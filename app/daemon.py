"""The process that polls the bot and hands each chat line to its session.

One daemon per bot token, on the machine that owns the token. It is the only
`getUpdates` caller -- Telegram enforces that, the lock file mirrors it
locally -- and everything else follows from that fact: sessions started with
`claude -t telegram` register a *route* (chat, topic, alias, a tag for their
buttons) and long-poll the daemon for their share of the updates, and lines
addressed to nobody are the daemon's own commands. `/claude ...` is the one
that matters: it opens a console window on this PC running exactly the
command line that would have been typed there.
"""

from __future__ import annotations

import contextlib
import ctypes
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.transport import daemonlink
from app.transport import poller as poller_module
from app.transport import profiles as profiles_module
from app.transport.profiles import DEFAULT_PROFILE, Profile
from app.transport.routing import (
    CLAUDE_COMMAND,
    ClaudeCommand,
    parse_claude_command,
    route as route_line,
    valid_alias,
)
from app.transport.conversation import resolve_dir
from core import claudecfg, log, telegram
from core.sessions import _pid_alive
from core.store import Config, bin_dir, manager_name, read_json, shim_name, write_json_atomic
from ui.i18n import t

POLL_WAIT_SECONDS = 25
ROUTE_STALE_SECONDS = 90.0
ONE_SHOT_TIMEOUT_SECONDS = 180.0
PROJECTS_PER_PAGE = 8
LIST_LIMIT = 40
DAEMON_TAG = "d"
# Separates a transport's tag from the conversation's within it: "t1.2".
CONVERSATION_SEPARATOR = "."

# `claude <subcommand>` and print-mode runs finish on their own: they are
# run to completion and their output posted, not given a window.
ONE_SHOT_FLAGS = frozenset({"-p", "--print", "-v", "--version", "-h", "--help"})


daemon_file = daemonlink.daemon_file
read_daemon = daemonlink.read_daemon


def chats_file() -> Path:
    return poller_module.telegram_dir() / "chats.json"


def configured(config: Config) -> bool:
    """A token is all the daemon needs; what it listens to is up to profiles."""
    return telegram.looks_like_token(str(config.telegram.get("token") or ""))


def ensure_running(config: Config) -> bool:
    """Start the daemon in the background unless it is already up.

    Called both by a transport that wants the token multiplexed and by
    `reconcile_autostart`; cheap when the daemon is up, since that is one
    file read and one pid probe.
    """
    if not configured(config):
        return False
    if read_daemon() is not None:
        return False
    if os.environ.get("CCAS_DAEMON_CHILD"):
        return False
    return spawn_detached()


def reconcile_autostart(config: Config) -> bool:
    """Keep the OS autostart entry in step with the profiles.

    There is no switch of its own: the daemon has to be up before anyone
    types anything exactly when some profile may be raised on its own, and
    that is the whole condition. Returns whether it is registered now.
    """
    from system import autostart

    wanted = configured(config) and profiles_module.daemon_wanted(config)
    if wanted:
        if not autostart.is_registered():
            autostart.register()
        ensure_running(config)
    else:
        if autostart.is_registered():
            autostart.unregister()
    return wanted


def spawn_detached() -> bool:
    command = [str(bin_dir() / manager_name()), "daemon", "run", "--hidden"]
    if not Path(command[0]).exists():
        # A development checkout: no frozen binary, run this interpreter.
        command = [sys.executable, str(Path(__file__).resolve().parent.parent / "main.py"), "daemon", "run", "--hidden"]
    env = dict(os.environ)
    env["CCAS_DAEMON_CHILD"] = "1"
    try:
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(command, env=env, creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(command, env=env, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.write(f"daemon: could not start: {exc}")
        return False
    log.write("daemon: started in the background")
    return True


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------


@dataclass
class Route:
    pid: int
    tag: str
    chat: int
    thread: int
    alias: str
    profile: str = DEFAULT_PROFILE
    cwd: str = ""
    slot: int = 0
    seen_at: float = field(default_factory=time.time)
    pending: list[dict[str, Any]] = field(default_factory=list)
    wakeup: threading.Condition = field(default_factory=threading.Condition)

    def push(self, incoming: telegram.Incoming) -> None:
        with self.wakeup:
            self.pending.append(dataclasses.asdict(incoming))
            self.wakeup.notify_all()

    def take(self, wait: float) -> list[dict[str, Any]]:
        with self.wakeup:
            if not self.pending:
                self.wakeup.wait(wait)
            batch, self.pending = self.pending, []
            return batch

    def serves(self, chat: int, thread: int) -> bool:
        """A transport with no chat of its own takes whatever its profile is
        routed; one started with --tg-chat is pinned to that chat."""
        if self.chat and self.chat != chat:
            return False
        return not self.thread or self.thread == thread

    def describe(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "tag": self.tag,
            "chat": self.chat,
            "thread": self.thread,
            "alias": self.alias,
            "profile": self.profile,
            "cwd": self.cwd,
            "slot": self.slot,
        }


class Daemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        settings = config.telegram
        self.bot = telegram.Bot(str(settings.get("token") or ""))
        self.prefix = str(settings.get("prefix") or "")
        self.routes: dict[int, Route] = {}
        # Lines that arrived while a profile's window was still coming up.
        self._pending: dict[str, list[telegram.Incoming]] = {}
        self._routes_lock = threading.Lock()
        self._stop = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._tags = 0
        self._projects: list[str] = []
        self.poller: poller_module.Poller | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else 0

    def run(self, *, hidden: bool = False) -> int:
        if not configured(self.config):
            _say_local(t("daemon.not_configured"), error=True)
            return 2
        holder = poller_module.read_holder(self.bot.id)
        if holder is not None and int(holder.get("pid", 0) or 0) != os.getpid():
            _say_local(t("daemon.already_running", pid=holder.get("pid")), error=True)
            return 1
        if hidden:
            hide_console_window()

        self._serve()
        poller_module.acquire(self.bot.id, port=self.port)
        write_json_atomic(
            daemon_file(),
            {"pid": os.getpid(), "port": self.port, "botId": self.bot.id, "at": time.time()},
            harden=False,
        )
        watched = [name for name, profile in self.profiles().items() if profile.daemon]
        log.write(f"daemon: up, bot {self.bot.id}, port {self.port}, watching {watched or '-'}")
        _say_local(
            t("daemon.listening", bot=self.bot.id, port=self.port, profiles=", ".join(watched) or "—")
        )

        self.poller = poller_module.Poller(self.bot, self.deliver, on_busy=lambda reason: self._stop.set())
        self.poller.start()
        self._install_signals()
        try:
            while not self._stop.is_set():
                self._stop.wait(5.0)
                poller_module.refresh(self.bot.id, port=self.port)
                self._prune()
        finally:
            self._shutdown()
        if self.poller.busy_reason:
            _say_local(t("daemon.token_busy", reason=self.poller.busy_reason), error=True)
            return 1
        return 0

    def _install_signals(self) -> None:
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            number = getattr(signal, name, None)
            if number is None:
                continue
            with contextlib.suppress(ValueError, OSError):
                signal.signal(number, lambda *_: self._stop.set())

    def _shutdown(self) -> None:
        if self.poller is not None:
            self.poller.stop()
        if self._server is not None:
            with contextlib.suppress(Exception):
                self._server.shutdown()
                self._server.server_close()
        poller_module.release(self.bot.id)
        raw = read_json(daemon_file())
        if isinstance(raw, dict) and int(raw.get("pid", 0) or 0) == os.getpid():
            with contextlib.suppress(OSError):
                daemon_file().unlink()
        log.write("daemon: down")

    def stop(self) -> None:
        self._stop.set()

    # -- the local API sessions talk to -----------------------------------

    def _serve(self) -> None:
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    parsed = json.loads(raw.decode("utf-8", "replace")) if raw else {}
                except ValueError:
                    return {}
                return parsed if isinstance(parsed, dict) else {}

            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path == "/route":
                    route = daemon.register(self._body())
                    self._reply(200, {"ok": route is not None, "tag": route.tag if route else ""})
                elif path == "/stop":
                    daemon.stop()
                    self._reply(200, {"ok": True})
                else:
                    self._reply(404, {"ok": False})

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                if parts.path == "/poll":
                    pid = int((query.get("pid") or ["0"])[0] or 0)
                    wait = min(float((query.get("wait") or [str(POLL_WAIT_SECONDS)])[0]), 60.0)
                    route = daemon.route_for(pid)
                    if route is None:
                        self._reply(404, {"ok": False, "error": "unknown route"})
                        return
                    route.seen_at = time.time()
                    self._reply(200, {"ok": True, "updates": route.take(wait)})
                elif parts.path == "/status":
                    self._reply(200, daemon.status())
                else:
                    self._reply(404, {"ok": False})

            def do_DELETE(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                pid = int((parse_qs(parts.query).get("pid") or ["0"])[0] or 0)
                daemon.unregister(pid)
                self._reply(200, {"ok": True})

            def log_message(self, *args: Any) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()

    def register(self, body: dict[str, Any]) -> Route | None:
        pid = int(body.get("pid") or 0)
        if pid <= 0:
            return None
        tag = str(body.get("tag") or "") or self.new_tag()
        route = Route(
            pid=pid,
            tag=tag,
            chat=int(body.get("chat") or 0),
            thread=int(body.get("thread") or 0),
            alias=str(body.get("alias") or ""),
            profile=str(body.get("profile") or "") or DEFAULT_PROFILE,
            cwd=str(body.get("cwd") or ""),
            slot=int(body.get("slot") or 0),
        )
        with self._routes_lock:
            self.routes[pid] = route
        log.write(f"daemon: route {tag} pid={pid} profile={route.profile} chat={route.chat or '*'}")
        for queued in self._pending.pop(route.profile, []):
            route.push(queued)
        return route

    def unregister(self, pid: int) -> None:
        with self._routes_lock:
            self.routes.pop(pid, None)

    def route_for(self, pid: int) -> Route | None:
        with self._routes_lock:
            return self.routes.get(pid)

    def new_tag(self) -> str:
        self._tags += 1
        return f"t{self._tags}"

    def _prune(self) -> None:
        now = time.time()
        with self._routes_lock:
            for pid, route in list(self.routes.items()):
                if not _pid_alive(pid) or now - route.seen_at > ROUTE_STALE_SECONDS:
                    del self.routes[pid]
                    log.write(f"daemon: route {route.tag} pid={pid} gone")

    def status(self) -> dict[str, Any]:
        with self._routes_lock:
            routes = [route.describe() for route in self.routes.values()]
        return {
            "ok": True,
            "pid": os.getpid(),
            "port": self.port,
            "botId": self.bot.id,
            "routes": routes,
            "updates": self.poller.state.updates if self.poller else 0,
        }

    def _routes_in(self, chat: int, thread: int) -> list[Route]:
        with self._routes_lock:
            return [route for route in self.routes.values() if route.serves(chat, thread)]

    # -- routing -----------------------------------------------------------

    def deliver(self, incoming: telegram.Incoming) -> None:
        """Hand one update to the profile it belongs to, or drop it.

        Nothing is listened to by default: a chat no profile claims and that
        the default profile is not open to gets silence, not a hint.
        """
        here = self._routes_in(incoming.chat_id, incoming.thread_id)

        if incoming.is_callback:
            # A button carries the conversation's tag ("t1.2"); the route is
            # the transport that drew it ("t1"). Comparing the two whole was
            # what made every press answer "that session is gone".
            tag = incoming.callback_data.split(":", 1)[0]
            if tag == DAEMON_TAG:
                self._on_button(incoming)
                return
            owner = tag.split(CONVERSATION_SEPARATOR, 1)[0]
            for route in here:
                if route.tag == owner:
                    route.push(incoming)
                    return
            self.bot.answer_callback(incoming.callback_id, t("daemon.button_expired"))
            return

        known = self.profiles()
        routed = route_line(
            incoming.text,
            prefix=self.prefix,
            profiles=known,
            chat=incoming.chat_id,
            thread=incoming.thread_id,
        )
        if routed is None:
            return
        profile = known[routed.profile]
        if not profile.open_to(incoming.chat_id, incoming.thread_id):
            # Named from a chat this profile does not work in: saying the
            # name somewhere else must not reach it.
            return
        if not profile.allows(incoming.user_id, self._users()):
            return

        command = parse_claude_command(routed.body)
        if command is not None:
            # Checked before the running transport is handed the line: to a
            # conversation `/claude ...` is just text, and the one thing it
            # certainly means is "start something".
            # An explicit request from someone allowed here is honoured even
            # when the profile is not one the daemon may raise on its own.
            self._launch(command, incoming, profile)
            return

        for existing in here:
            if existing.profile == profile.name:
                existing.push(incoming)
                return

        if routed.body.startswith("/") and self._own_command(routed.body, incoming):
            return

        if profile.daemon:
            self._pending.setdefault(profile.name, []).append(incoming)
            bare = parse_claude_command(CLAUDE_COMMAND)
            assert bare is not None
            self._launch(bare, incoming, profile, quiet=True)
            return

        if routed.addressed or routed.body.startswith("/"):
            self._say(incoming, t("daemon.not_running", name=profile.alias or DEFAULT_PROFILE))

    def _say(self, incoming: telegram.Incoming, text: str, *, markup: dict[str, Any] | None = None) -> None:
        with contextlib.suppress(telegram.TelegramError, telegram.Unreachable):
            self.bot.send_message(incoming.chat_id, text, thread_id=incoming.thread_id, reply_markup=markup)

    # -- the daemon's own commands -----------------------------------------

    def _own_command(self, body: str, incoming: telegram.Incoming) -> bool:
        head, _, tail = body.partition(" ")
        head = head.lower().split("@", 1)[0]
        tail = tail.strip()
        if head in {"/help", "/start"}:
            self._say(incoming, t("daemon.help", prefix=self.prefix or "—"))
            return True
        if head == "/pwd":
            self._say(incoming, f"<code>{_plain(str(self.chat_dir(incoming)))}</code>")
            return True
        if head == "/cd":
            if not tail:
                self._say(incoming, t("tg.cd_usage"))
                return True
            target = resolve_dir(tail, self.chat_dir(incoming), roots=self.config.telegram.get("roots") or [])
            if target is None:
                self._say(incoming, t("tg.cd_bad", path=tail))
                return True
            self.set_chat_dir(incoming, target)
            self._say(incoming, t("daemon.cd_done", path=_plain(str(target))))
            return True
        if head == "/ls":
            self._say(incoming, self._listing(tail, incoming))
            return True
        if head == "/projects":
            text, markup = self._projects_page(0)
            self._say(incoming, text, markup=markup)
            return True
        if head == "/sessions":
            self._say(incoming, self._sessions_text(incoming))
            return True
        return False

    def _on_button(self, incoming: telegram.Incoming) -> None:
        parts = incoming.callback_data.split(":")
        self.bot.answer_callback(incoming.callback_id)
        if len(parts) < 3:
            return
        action, value = parts[1], parts[2]
        if action == "pg":
            text, markup = self._projects_page(int(value or 0))
            self.bot.edit_text(incoming.chat_id, incoming.message_id, text)
            self.bot.edit_markup(incoming.chat_id, incoming.message_id, markup)
            return
        if action in {"cd", "go"}:
            try:
                path = self._projects[int(value)]
            except (ValueError, IndexError):
                self._say(incoming, t("daemon.button_expired"))
                return
            target = resolve_dir(path, Path.home(), roots=self.config.telegram.get("roots") or [])
            if target is None:
                self._say(incoming, t("tg.cd_bad", path=path))
                return
            self.set_chat_dir(incoming, target)
            self.bot.edit_markup(incoming.chat_id, incoming.message_id, None)
            if action == "cd":
                self._say(incoming, t("daemon.cd_done", path=_plain(str(target))))
            else:
                command = parse_claude_command(CLAUDE_COMMAND)
                assert command is not None
                self._launch(command, incoming)

    def _projects_page(self, page: int) -> tuple[str, dict[str, Any]]:
        if page == 0 or not self._projects:
            self._projects = claudecfg.known_projects()
        total = len(self._projects)
        if not total:
            return t("daemon.no_projects"), {"inline_keyboard": []}
        pages = (total + PROJECTS_PER_PAGE - 1) // PROJECTS_PER_PAGE
        page = max(0, min(page, pages - 1))
        start = page * PROJECTS_PER_PAGE
        chunk = list(enumerate(self._projects))[start : start + PROJECTS_PER_PAGE]
        rows: list[list[tuple[str, str]]] = []
        lines = [t("daemon.projects_title", page=page + 1, pages=pages)]
        for index, path in chunk:
            label = Path(path).name or path
            lines.append(f"• <code>{_plain(path)}</code>")
            rows.append([(f"📁 {label}"[:40], f"d:cd:{index}"), ("▶", f"d:go:{index}")])
        nav: list[tuple[str, str]] = []
        if page > 0:
            nav.append(("⬅", f"d:pg:{page - 1}"))
        if page < pages - 1:
            nav.append(("➡", f"d:pg:{page + 1}"))
        if nav:
            rows.append(nav)
        return "\n".join(lines), telegram.keyboard(rows)

    def _listing(self, raw: str, incoming: telegram.Incoming) -> str:
        base = self.chat_dir(incoming)
        target = resolve_dir(raw, base, roots=self.config.telegram.get("roots") or []) if raw else base
        if target is None:
            return t("tg.cd_bad", path=raw)
        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            return t("tg.cd_bad", path=exc)
        lines = [f"<b>{_plain(str(target))}</b>"]
        for entry in entries[:LIST_LIMIT]:
            lines.append(f"{'📁' if entry.is_dir() else '📄'} {_plain(entry.name)}")
        if len(entries) > LIST_LIMIT:
            lines.append(f"… +{len(entries) - LIST_LIMIT}")
        return "\n".join(lines)

    def _sessions_text(self, incoming: telegram.Incoming) -> str:
        here = self._routes_in(incoming.chat_id, incoming.thread_id)
        if not here:
            return t("daemon.nothing_running")
        lines = []
        for route in here:
            lines.append(
                f"• <b>{_plain(route.profile)}</b> · slot {route.slot} · "
                f"<code>{_plain(route.cwd)}</code> · pid {route.pid}"
            )
        return "\n".join(lines)

    # -- per-chat directory -----------------------------------------------

    def _chat_key(self, incoming: telegram.Incoming) -> str:
        return f"{incoming.chat_id}:{incoming.thread_id}"

    def chat_dir(self, incoming: telegram.Incoming) -> Path:
        raw = read_json(chats_file())
        stored = raw.get(self._chat_key(incoming)) if isinstance(raw, dict) else None
        if isinstance(stored, str) and stored and Path(stored).is_dir():
            return Path(stored)
        default = str(self.config.telegram.get("workdir") or "")
        if default and Path(default).is_dir():
            return Path(default)
        return Path.home()

    def set_chat_dir(self, incoming: telegram.Incoming, path: Path) -> None:
        raw = read_json(chats_file())
        table = raw if isinstance(raw, dict) else {}
        table[self._chat_key(incoming)] = str(path)
        write_json_atomic(chats_file(), table, harden=False)

    # -- /claude -----------------------------------------------------------

    def _launch(
        self,
        command: ClaudeCommand,
        incoming: telegram.Incoming,
        profile: Profile | None = None,
        *,
        quiet: bool = False,
    ) -> None:
        options, args = command.options, list(command.args)
        known = self.profiles()

        if options.name:
            if not valid_alias(options.name):
                self._say(incoming, t("daemon.bad_alias", name=options.name))
                return
            wanted = known.get(options.name)
            if wanted is None:
                self._say(
                    incoming,
                    t("daemon.no_profile", name=options.name, names=", ".join(sorted(known))),
                )
                return
            profile = wanted
        elif profile is None:
            routed = route_line(
                CLAUDE_COMMAND,
                prefix="",
                profiles=known,
                chat=incoming.chat_id,
                thread=incoming.thread_id,
            )
            profile = known[routed.profile] if routed is not None else known[DEFAULT_PROFILE]

        if not profile.open_to(incoming.chat_id, incoming.thread_id):
            self._say(
                incoming,
                t(
                    "daemon.profile_elsewhere",
                    name=profile.name,
                    chats=", ".join(profiles_module.format_chat(ref) for ref in profile.chats),
                ),
            )
            return
        if not profile.allows(incoming.user_id, self._users()):
            return

        if args and (args[0] in _subcommands() or set(args) & ONE_SHOT_FLAGS):
            self._one_shot(args, incoming)
            return

        here = self._routes_in(incoming.chat_id, incoming.thread_id)
        if any(existing.profile == profile.name for existing in here):
            self._say(incoming, t("daemon.already_up", name=profile.alias or DEFAULT_PROFILE))
            return

        cwd = self._launch_dir(options.cwd, profile, incoming)
        if cwd is None:
            self._say(incoming, t("tg.cd_bad", path=options.cwd))
            return

        slot_token = ""
        if args and (args[0].isdigit() or args[0].startswith("@")):
            slot_token = args.pop(0)
        elif profile.slot:
            slot_token = str(profile.slot)
        args = [*profile.args, *args]
        if profile.alias and "-n" not in args and "--name" not in args:
            args = ["-n", profile.alias, *args]

        tag = self.new_tag()
        line = [str(bin_dir() / shim_name())]
        if slot_token:
            line.append(slot_token)
        line += ["-t", "telegram", *args]
        env = dict(os.environ)
        env["CCAS_TELEGRAM_FEED"] = "1"
        env["CCAS_TELEGRAM_TAG"] = tag
        env["CCAS_TELEGRAM_PROFILE"] = profile.name
        env["CCAS_TELEGRAM_DAEMON_PORT"] = str(self.port)

        try:
            open_window(line, cwd=cwd, env=env, console=bool(self.config.telegram.get("console", True)))
        except OSError as exc:
            self._say(incoming, t("tg.launch_failed", error=exc))
            return
        log.write(f"daemon: launched {line[1:]} in {cwd} as {tag} for {profile.name}")
        if not quiet:
            self._say(
                incoming,
                t("daemon.launching", cwd=_plain(str(cwd)), name=profile.alias or DEFAULT_PROFILE),
            )

    def profiles(self) -> dict[str, Profile]:
        """Read from disk every time: `ccas profile set` must take effect in
        a daemon that has been running for days."""
        return profiles_module.load(Config.load())

    def _users(self) -> tuple[int, ...]:
        return profiles_module.global_users(Config.load())

    def _launch_dir(self, explicit: str, profile: Profile | None, incoming: telegram.Incoming) -> Path | None:
        roots = self.config.telegram.get("roots") or []
        if explicit:
            return resolve_dir(explicit, self.chat_dir(incoming), roots=roots)
        if profile is not None and profile.cwd:
            found = resolve_dir(profile.cwd, Path.home(), roots=roots)
            if found is not None:
                return found
        return self.chat_dir(incoming)

    def _one_shot(self, args: list[str], incoming: telegram.Incoming) -> None:
        line = [str(bin_dir() / shim_name()), *args]
        try:
            completed = subprocess.run(
                line,
                cwd=str(self.chat_dir(incoming)),
                capture_output=True,
                timeout=ONE_SHOT_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = (completed.stdout + completed.stderr).decode("utf-8", "replace").strip()
        except subprocess.TimeoutExpired:
            output = t("tg.shell_timeout", seconds=int(ONE_SHOT_TIMEOUT_SECONDS))
        except OSError as exc:
            output = str(exc)
        self._say(incoming, f"<b>claude {_plain(' '.join(args))}</b>\n<pre>{_plain(output[:3500] or t('tg.shell_no_output'))}</pre>")


def _subcommands() -> frozenset[str]:
    from app.wrapper import CLAUDE_SUBCOMMANDS

    return CLAUDE_SUBCOMMANDS


def _plain(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _say_local(text: str, *, error: bool = False) -> None:
    """Print if there is a console; a detached daemon has none (the frozen
    build then has sys.stdout set to None), and the journal has it anyway."""
    log.write(f"daemon: {text}")
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        with contextlib.suppress(OSError, ValueError):
            stream.write(text + "\n")
            stream.flush()


# --------------------------------------------------------------------------
# windows and terminals
# --------------------------------------------------------------------------


def open_window(command: list[str], *, cwd: Path, env: dict[str, str], console: bool) -> None:
    """Start `command` in a fresh console on this PC, or quietly if told to."""
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_CONSOLE if console else getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(command, cwd=str(cwd), env=env, creationflags=flags)
        return
    if console:
        terminal = _terminal_command(command)
        if terminal is not None:
            subprocess.Popen(terminal, cwd=str(cwd), env=env, start_new_session=True)
            return
    subprocess.Popen(
        command, cwd=str(cwd), env=env, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _terminal_command(command: list[str]) -> list[str] | None:
    if sys.platform == "darwin":
        script = " ".join(_sh_quote(part) for part in command)
        return ["osascript", "-e", f'tell application "Terminal" to do script "{script}"']
    for launcher in (
        ["x-terminal-emulator", "-e"],
        ["gnome-terminal", "--"],
        ["konsole", "-e"],
        ["xfce4-terminal", "-x"],
        ["xterm", "-e"],
    ):
        if shutil.which(launcher[0]):
            return [*launcher, *command]
    return None


def _sh_quote(part: str) -> str:
    return "'" + part.replace("'", "'\\''") + "'"


def hide_console_window() -> None:
    if os.name != "nt":
        return
    with contextlib.suppress(AttributeError, OSError):
        handle = ctypes.windll.kernel32.GetConsoleWindow()
        if handle:
            ctypes.windll.user32.ShowWindow(handle, 0)


# --------------------------------------------------------------------------
# CLI helpers
# --------------------------------------------------------------------------


def status_text(config: Config) -> list[str]:
    lines: list[str] = []
    record = read_daemon()
    if record is None:
        lines.append(t("daemon.status_down"))
    else:
        lines.append(t("daemon.status_up", pid=record.get("pid"), port=record.get("port"), bot=record.get("botId")))
        try:
            payload = daemonlink.status(int(record.get("port") or 0))
            routes = payload.get("routes") or []
            lines.append(t("daemon.status_routes", count=len(routes), updates=payload.get("updates", 0)))
            for route in routes:
                lines.append(
                    f"  {route.get('profile') or '-':<12} chat {route.get('chat') or '*'} "
                    f"slot {route.get('slot')} pid {route.get('pid')} {route.get('cwd')}"
                )
        except daemonlink.LinkError:
            lines.append(t("daemon.status_unreachable"))
    holder = poller_module.read_holder(telegram.bot_id(str(config.telegram.get("token") or "")))
    if holder is not None and (record is None or int(holder.get("pid", 0) or 0) != int(record.get("pid", 0) or 0)):
        lines.append(t("daemon.status_lock_elsewhere", pid=holder.get("pid")))
    return lines


def request_stop() -> bool:
    record = read_daemon()
    if record is None:
        return False
    try:
        daemonlink.stop(int(record.get("port") or 0))
    except daemonlink.LinkError:
        return False
    deadline = time.time() + 10
    while time.time() < deadline:
        if read_daemon() is None:
            return True
        time.sleep(0.2)
    return False


__all__ = ["Daemon", "ensure_running", "read_daemon", "request_stop", "spawn_detached", "status_text"]

