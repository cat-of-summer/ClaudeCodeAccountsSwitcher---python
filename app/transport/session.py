"""One claude session living in one Telegram chat.

The session owns a headless claude (`Driver`), renders what it does into the
chat and into the console window it runs in, and feeds it what the chat and
the console type. Questions and permission prompts go through `Prompter`.
Updates arrive from whoever polls the bot -- the session's own poller when it
is alone, the daemon otherwise -- via `deliver()`, so the two cases look the
same from in here.

A few lines are the session's own, not claude's: `/stop`, `/kill`, `/status`,
`/usage`, `/cd`, `/pwd`, `/switch`, `/save`, `/help`, and `!cmd`, which the
TUI would have run in its bash mode. Everything else is a prompt.
"""

from __future__ import annotations

import contextlib
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import autoswitch, wrapper
from app.transport import daemonlink
from app.transport import poller as poller_module
from app.transport.driver import Driver, DriverError, Event
from app.transport.prompter import Outcome, Prompt, Prompter
from app.transport.routing import address
from core import hookbus, log, telegram
from core.sessions import update_session
from core.store import Accounts, Config, TELEGRAM_VERBOSITIES, update_accounts
from ui import usage
from ui.i18n import t

TYPING_INTERVAL_SECONDS = 4.0
TICK_SECONDS = 1.0
SHELL_TIMEOUT_SECONDS = 120.0
OUTPUT_PREVIEW_LIMIT = 3000
TOOL_RESULT_PREVIEW = 400
FEED_WAIT_SECONDS = 25
FEED_RETRY_SECONDS = 2.0
FEED_FAILURES_BEFORE_TAKEOVER = 3
LOCK_REFRESH_SECONDS = 60.0
# A second Ctrl-C this soon after the first ends the session instead of
# interrupting the turn again.
DOUBLE_INTERRUPT_SECONDS = 3.0
HELP_COMMANDS = ("/stop", "/kill", "/status", "/usage", "/pwd", "/cd", "/switch", "/model", "/mode", "/save", "/help")

_esc = telegram.markdown_to_html


def _plain(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class Relaunch:
    cwd: Path | None = None
    slot: int | None = None


@dataclass
class ChatTarget:
    bot: telegram.Bot
    chat: int
    thread: int = 0
    users: list[int] = field(default_factory=list)
    prefix: str = ""

    def accepts(self, incoming: telegram.Incoming) -> bool:
        if incoming.chat_id != self.chat:
            return False
        if self.thread and incoming.thread_id != self.thread:
            return False
        if self.users and incoming.user_id not in self.users:
            return False
        return True


class Session:
    def __init__(
        self,
        config: Config,
        *,
        slot: int,
        target: ChatTarget,
        cwd: Path,
        args: list[str],
        alias: str = "",
        session_id: str = "",
        resume: bool = False,
        own_poller: bool = True,
        tag: str = "s",
    ) -> None:
        self.config = config
        self.slot = slot
        self.target = target
        self.cwd = cwd
        self.args = list(args)
        self.alias = alias
        self.session_id = session_id
        self.resume = resume
        self.own_poller = own_poller
        # Prefixed to every button this session sends, so that the daemon can
        # hand the press back to the right session when several share a chat.
        self.tag = tag

        self.verbosity = str(config.telegram.get("verbosity") or "tools")
        if self.verbosity not in TELEGRAM_VERBOSITIES:
            self.verbosity = "tools"
        timeout_minutes = config.telegram.get("promptTimeoutMinutes")
        self.prompt_timeout = float(timeout_minutes) * 60 if isinstance(timeout_minutes, (int, float)) else 0.0

        self.driver: Driver | None = None
        self.prompter: Prompter | None = None
        self.bus: hookbus.HookBus | None = None
        self.poller: poller_module.Poller | None = None
        self._inbox: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._prompt_messages: dict[str, int] = {}
        self._relaunch: Relaunch | None = None
        self._closing = False
        self._last_typing = 0.0
        self._last_interrupt = 0.0
        self._last_lock_refresh = 0.0
        self.exit_code = 0
        self.tried: set[int] = set()
        self._feed_port = 0
        self._feed_stop = threading.Event()

    # -- entry -------------------------------------------------------------

    def run(self) -> int:
        """Drive the session until claude ends it or the chat asks it to."""
        if self.own_poller:
            bot_id = self.target.bot.id
            holder = poller_module.read_holder(bot_id)
            if holder is not None and not poller_module.acquire(bot_id):
                port = int(holder.get("port") or 0)
                if not port:
                    self._console(t("tg.token_held_locally", pid=holder.get("pid")))
                    return 2
                # A daemon has the token: let it feed us instead of fighting.
                self.own_poller = False
                self._feed_port = port
            else:
                poller_module.acquire(bot_id)
                self._start_poller()
        if not self.own_poller:
            self._start_feed()

        if sys.stdin and sys.stdin.isatty():
            threading.Thread(target=self._console_loop, daemon=True).start()

        try:
            while True:
                with wrapper.slot_session(
                    self.config,
                    self.slot,
                    transport="telegram",
                    chat=self.target.chat,
                    thread=self.target.thread,
                    alias=self.alias,
                    cwd=str(self.cwd),
                ) as fresh_login:
                    if fresh_login:
                        self._say(t("tg.login_needed", slot=self.slot))
                        return 2
                    relaunch = self._run_on_slot()
                if relaunch is None:
                    return self.exit_code
                if relaunch.cwd is not None:
                    self.cwd = relaunch.cwd
                if relaunch.slot is not None:
                    self.slot = relaunch.slot
                self.resume = True
        finally:
            self._feed_stop.set()
            if self._feed_port:
                daemonlink.unregister(self._feed_port, os.getpid())
            if self.poller is not None:
                self.poller.stop()
                poller_module.release(self.target.bot.id)

    def _start_poller(self) -> None:
        self.poller = poller_module.Poller(self.target.bot, self.deliver, on_busy=self._on_busy)
        self.poller.start()

    # -- fed by the daemon -------------------------------------------------

    def _route_record(self) -> dict[str, Any]:
        return {
            "pid": os.getpid(),
            "tag": self.tag,
            "chat": self.target.chat,
            "thread": self.target.thread,
            "alias": self.alias,
            "cwd": str(self.cwd),
            "slot": self.slot,
        }

    def _start_feed(self) -> None:
        if not self._feed_port:
            env_port = os.environ.get("CCAS_TELEGRAM_DAEMON_PORT") or ""
            record = daemonlink.read_daemon()
            self._feed_port = int(env_port) if env_port.isdigit() else int((record or {}).get("port") or 0)
        threading.Thread(target=self._feed_loop, daemon=True, name="daemon-feed").start()

    def _feed_loop(self) -> None:
        """Long-poll the daemon for this route's updates.

        When the daemon goes away the session tries to take the token over
        and poll on its own: the chat keeps working, only the daemon's own
        commands are gone until it is back.
        """
        failures = 0
        registered = False
        while not self._feed_stop.is_set():
            try:
                if not registered:
                    tag = daemonlink.register(self._feed_port, self._route_record())
                    if tag:
                        self.tag = tag
                    registered = True
                for raw in daemonlink.poll(self._feed_port, os.getpid(), wait=FEED_WAIT_SECONDS):
                    with contextlib.suppress(TypeError):
                        self.deliver(telegram.Incoming(**raw))
                failures = 0
            except daemonlink.LinkError as exc:
                failures += 1
                registered = False
                if failures >= FEED_FAILURES_BEFORE_TAKEOVER and daemonlink.read_daemon() is None:
                    if poller_module.acquire(self.target.bot.id):
                        log.write(f"transport: daemon gone ({exc}); polling the bot myself")
                        self.own_poller = True
                        self._feed_port = 0
                        self._start_poller()
                        return
                self._feed_stop.wait(FEED_RETRY_SECONDS)
                record = daemonlink.read_daemon()
                if record is not None:
                    self._feed_port = int(record.get("port") or self._feed_port)

    def _run_on_slot(self) -> Relaunch | None:
        self.tried.add(self.slot)
        self._relaunch = None
        self.bus = None
        if wrapper.wants_hook_bus(self.config, self.args):
            self.bus = wrapper.open_hook_bus(self.config, self.slot)
            self.bus.subscribe(self._on_hook)

        driver = Driver(
            self.config,
            self.slot,
            cwd=self.cwd,
            args=self.args,
            name=self.alias,
            session_id=self.session_id,
            resume=self.resume,
            listener=lambda event: self._inbox.put(("event", (driver, event))),
            bus=self.bus,
        )
        self.driver = driver
        self.prompter = Prompter(driver, timeout_seconds=self.prompt_timeout)
        try:
            driver.start()
        except DriverError as exc:
            self._say(t("tg.launch_failed", error=exc))
            wrapper.close_hook_bus(self.bus)
            self.exit_code = 2
            return None
        self.session_id = driver.session_id
        update_session(session_id=self.session_id, cwd=str(self.cwd), slot=self.slot)
        if self._feed_port and self.resume:
            # The daemon shows cwd and slot in /sessions; keep it current.
            with contextlib.suppress(daemonlink.LinkError):
                daemonlink.register(self._feed_port, self._route_record())

        if not self.resume:
            self._say(self._banner())
        else:
            self._say(t("tg.resumed", slot=self.slot, cwd=self.cwd))

        try:
            self._loop()
        finally:
            driver.close()
            wrapper.close_hook_bus(self.bus)
            self.bus = None
        return self._relaunch

    def _loop(self) -> None:
        assert self.driver is not None and self.prompter is not None
        while True:
            try:
                kind, item = self._inbox.get(timeout=TICK_SECONDS)
            except queue.Empty:
                self._tick()
                continue
            except KeyboardInterrupt:
                # Ctrl-C in the console window: the TUI would stop the turn,
                # so do that; a second one right after closes the session.
                if time.time() - self._last_interrupt < DOUBLE_INTERRUPT_SECONDS:
                    self._say(t("tg.bye", session=self.session_id[:8]))
                    self._closing = True
                    return
                self._last_interrupt = time.time()
                self.driver.interrupt()
                self._console(t("tg.interrupted"))
                continue
            if kind == "event":
                owner, event = item
                if owner is not self.driver:
                    continue  # a driver we already replaced, still winding down
                self._on_event(event)
                if event.kind == "exit":
                    return
            elif kind == "incoming":
                self._on_incoming(item)
            elif kind == "line":
                self._on_line(item, source="console")
            elif kind == "busy":
                self._say(t("tg.token_busy", reason=item))
                self._closing = True
            if self._relaunch is not None or self._closing:
                return

    def _tick(self) -> None:
        assert self.driver is not None and self.prompter is not None
        if self.driver.turn_active and time.time() - self._last_typing > TYPING_INTERVAL_SECONDS:
            self._last_typing = time.time()
            self.target.bot.typing(self.target.chat, thread_id=self.target.thread)
        for outcome in self.prompter.tick():
            self._apply(outcome)
        if self.poller is not None and time.time() - self._last_lock_refresh > LOCK_REFRESH_SECONDS:
            self._last_lock_refresh = time.time()
            poller_module.refresh(self.target.bot.id)

    # -- inputs ------------------------------------------------------------

    def deliver(self, incoming: telegram.Incoming) -> None:
        """An update from whoever polls the bot."""
        self._inbox.put(("incoming", incoming))

    def _on_busy(self, reason: str) -> None:
        self._inbox.put(("busy", reason))

    def _on_hook(self, event: hookbus.HookEvent) -> dict[str, Any] | None:
        if event.name == "StopFailure" and event.payload.get("error") == "rate_limit":
            wall = Event(
                "rate_limit",
                {"status": "rejected", "message": str(event.payload.get("last_assistant_message") or "")},
            )
            self._inbox.put(("event", (self.driver, wall)))
        return None

    def _console_loop(self) -> None:
        while not self._closing:
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                return
            if not line:
                return
            self._inbox.put(("line", line.rstrip("\r\n")))

    def _on_incoming(self, incoming: telegram.Incoming) -> None:
        if not self.target.accepts(incoming):
            return
        assert self.prompter is not None
        if incoming.is_callback:
            data = incoming.callback_data
            if not data.startswith(self.tag + ":"):
                return
            data = data[len(self.tag) + 1 :]
            if data.startswith("sw:"):
                self.target.bot.answer_callback(incoming.callback_id)
                self.target.bot.edit_markup(self.target.chat, incoming.message_id, None)
                self._switch(data[3:])
                return
            outcome = self.prompter.on_callback(data)
            self.target.bot.answer_callback(incoming.callback_id, outcome.ack)
            self._apply(outcome)
            return
        parsed = address(incoming.text, prefix=self.target.prefix, aliases=[self.alias] if self.alias else [])
        if parsed is None:
            return
        if self.alias and parsed.alias != self.alias and not parsed.body.startswith("/claude"):
            # Addressed to nobody in particular: only a nameless session
            # takes those, and this one has a name.
            return
        if parsed.body.startswith("/claude"):
            return  # the daemon's business, not ours
        self._on_line(parsed.body, source="telegram", user=incoming.username or str(incoming.user_id))

    def _on_line(self, text: str, *, source: str, user: str = "") -> None:
        assert self.driver is not None and self.prompter is not None
        line = text.strip()
        if source == "console" and self.alias and line.startswith(self.alias + " "):
            line = line[len(self.alias) :].strip()

        if not line:
            if source == "telegram":
                self._say(self._status())
            return

        outcome = self.prompter.on_text(line)
        if outcome.consumed:
            self._apply(outcome)
            return

        if source == "telegram":
            self._console(f"[{user}] {line}")

        if line.startswith("!"):
            self._shell(line[1:].strip())
            return
        if line.startswith("/") and self._own_command(line):
            return
        try:
            self.driver.send_user(line)
        except DriverError as exc:
            self._say(t("tg.not_running", error=exc))

    # -- the session's own commands ---------------------------------------

    def _own_command(self, line: str) -> bool:
        assert self.driver is not None
        head, _, tail = line.partition(" ")
        head = head.lower().split("@", 1)[0]
        tail = tail.strip()

        if head == "/stop":
            self.driver.interrupt()
            self._say(t("tg.interrupted"))
            return True
        if head in {"/kill", "/exit", "/quit"}:
            self._say(t("tg.bye", session=self.session_id[:8]))
            self._closing = True
            return True
        if head == "/status":
            self._say(self._status())
            return True
        if head == "/help":
            self._say(self._help())
            return True
        if head == "/usage":
            self._say(self._usage())
            return True
        if head == "/pwd":
            self._say(f"<code>{_plain(str(self.cwd))}</code>")
            return True
        if head == "/cd":
            self._change_dir(tail)
            return True
        if head == "/switch":
            self._switch(tail)
            return True
        if head == "/model":
            self._control(lambda: self.driver.set_model(tail) if self.driver else None, t("tg.model_set", model=tail or "default"))
            return True
        if head == "/mode":
            self._control(lambda: self.driver.set_permission_mode(tail) if self.driver else None, t("tg.mode_set", mode=tail))
            return True
        if head == "/save":
            self._save_profile()
            return True
        return False

    def _control(self, action: Any, done: str) -> None:
        try:
            action()
        except DriverError as exc:
            self._say(t("tg.control_failed", error=exc))
            return
        self._say(done)

    def _shell(self, command: str) -> None:
        assert self.driver is not None
        if not command:
            return
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(self.cwd),
                capture_output=True,
                timeout=SHELL_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            stdout = completed.stdout.decode("utf-8", "replace")
            stderr = completed.stderr.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            stdout, stderr = "", t("tg.shell_timeout", seconds=int(SHELL_TIMEOUT_SECONDS))
        except OSError as exc:
            stdout, stderr = "", str(exc)

        shown = (stdout + ("\n" + stderr if stderr else "")).strip() or t("tg.shell_no_output")
        self._say(f"<b>! {_plain(command)}</b>\n<pre>{_plain(shown[:OUTPUT_PREVIEW_LIMIT])}</pre>")
        # The same shape the TUI uses for its bash mode, so the exchange
        # lands in claude's context the way it would have locally.
        payload = (
            f"<bash-input>{command}</bash-input>"
            f"<bash-stdout>{stdout}</bash-stdout><bash-stderr>{stderr}</bash-stderr>"
        )
        with contextlib.suppress(DriverError):
            self.driver.send_user(payload)

    def _change_dir(self, raw: str) -> None:
        if not raw:
            self._say(t("tg.cd_usage"))
            return
        target = resolve_dir(raw, self.cwd, roots=self.config.telegram.get("roots") or [])
        if target is None:
            self._say(t("tg.cd_bad", path=raw))
            return
        self._say(t("tg.cd_moving", path=target))
        self._relaunch = Relaunch(cwd=target)

    def _switch(self, raw: str) -> None:
        accounts = Accounts.load()
        if raw:
            probe = raw.strip()
            slot = accounts.by_alias(probe[1:] if probe.startswith("@") else probe)
            number = slot.number if slot is not None else (int(probe) if probe.isdigit() else 0)
            if number <= 0 or accounts.get(number) is None or not accounts.get(number).has_credentials():  # type: ignore[union-attr]
                self._say(t("tg.switch_bad", target=raw))
                return
        else:
            number = self._elect(accounts)
            if number is None:
                self._say(t("tg.switch_none"))
                return
        if number == self.slot:
            self._say(t("tg.switch_same", slot=number))
            return
        self._say(t("tg.switching", slot=number, label=accounts.ensure(number).label))
        self._relaunch = Relaunch(slot=number)

    def _elect(self, accounts: Accounts) -> int | None:
        threshold = float(self.config.auto_switch.get("threshold") or 95)
        wrapper._refresh_candidates(accounts, skip=set())
        election = autoswitch.elect_target(
            accounts,
            current=self.slot,
            tried=set(self.tried),
            threshold=threshold,
            strategy=str(self.config.auto_switch.get("strategy") or "limits"),
        )
        if election.target is None or election.must_wait():
            return None
        return election.target

    def _save_profile(self) -> None:
        if not self.alias:
            self._say(t("tg.save_needs_name"))
            return
        profile = {
            "chat": self.target.chat,
            "thread": self.target.thread,
            "cwd": str(self.cwd),
            "slot": self.slot,
            "args": list(self.args),
        }
        config = Config.load()
        sessions = dict(config.telegram.get("sessions") or {})
        sessions[self.alias] = profile
        config.telegram = {**config.telegram, "sessions": sessions}
        config.save()
        self._say(t("tg.saved", name=self.alias, cwd=self.cwd, slot=self.slot))

    # -- events from claude -----------------------------------------------

    def _on_event(self, event: Event) -> None:
        assert self.prompter is not None
        data = event.data
        if event.kind == "init":
            self._console(t("tg.console_ready", session=self.session_id[:8], model=data.get("model") or "?"))
        elif event.kind == "text":
            if data.get("subagent") and self.verbosity != "all":
                return
            self._say(_esc(str(data["text"])), plain=str(data["text"]))
        elif event.kind == "tool_use":
            if self.verbosity == "text" or (data.get("subagent") and self.verbosity != "all"):
                return
            self._say(f"🔧 <b>{_plain(str(data['name']))}</b> <code>{_plain(_tool_line(data))}</code>")
        elif event.kind == "tool_result":
            if self.verbosity != "all":
                return
            content = str(data.get("content") or "").strip()
            if content:
                mark = "❌" if data.get("is_error") else "↩"
                self._say(f"{mark} <pre>{_plain(content[:TOOL_RESULT_PREVIEW])}</pre>")
        elif event.kind == "ask":
            self._show_prompt(self.prompter.on_ask(event))
        elif event.kind == "cancel":
            key = self.prompter.on_cancel(str(data.get("request_id") or ""))
            if key:
                self._close_prompt(key, t("tg.prompt_cancelled"))
        elif event.kind == "result":
            if data.get("api_error_status") == 429 or data.get("terminal_reason") == "rate_limit":
                self._rate_limited(str(data.get("text") or ""))
            elif data.get("is_error") and data.get("text"):
                self._say(f"⚠️ {_plain(str(data['text']))}")
            elif self.verbosity == "all":
                self._say(t("tg.turn_done", seconds=int(data.get("duration_ms", 0) / 1000), cost=f"{data.get('cost', 0):.3f}"))
        elif event.kind == "rate_limit":
            if data.get("status") == "rejected":
                self._rate_limited(str(data.get("message") or ""), window=str(data.get("window") or ""))
        elif event.kind == "exit":
            if self._relaunch is None and not self._closing:
                code = int(data.get("code") or 0)
                self.exit_code = code
                tail = str(data.get("stderr") or "")
                self._say(t("tg.ended", code=code) + (f"\n<pre>{_plain(tail[:500])}</pre>" if tail and code else ""))

    _rate_limit_announced = 0.0

    def _rate_limited(self, message: str, *, window: str = "") -> None:
        if time.time() - self._rate_limit_announced < 30:
            return
        self._rate_limit_announced = time.time()
        accounts = Accounts.load()
        target = self._elect(accounts)
        lines = [t("tg.rate_limited", slot=self.slot, window=window or "?")]
        if message:
            lines.append(f"<i>{_plain(message)}</i>")
        if target is not None:
            lines.append(t("tg.rate_limited_hint", slot=target, label=accounts.ensure(target).label))
            markup = telegram.keyboard([[(t("tg.switch_button", slot=target), f"{self.tag}:sw:{target}")]])
        else:
            lines.append(t("tg.switch_none"))
            markup = None
        self._say("\n".join(lines), markup=markup)

    # -- prompts -----------------------------------------------------------

    def _show_prompt(self, prompt: Prompt) -> None:
        markup = telegram.keyboard(
            [[(choice.label, f"{self.tag}:{choice.data}") for choice in row] for row in prompt.rows]
        )
        message_id = self._say(prompt.text, markup=markup)
        self._prompt_messages[prompt.key] = message_id
        numbered = [f"  {index + 1}) {choice.label}" for index, choice in enumerate(c for row in prompt.rows for c in row)]
        self._console("\n".join(numbered) + "\n" + t("tg.console_answer_hint"))

    def _apply(self, outcome: Outcome) -> None:
        if outcome.close_prompt:
            self._close_prompt(outcome.close_prompt, outcome.summary)
        if outcome.next_prompt is not None:
            self._show_prompt(outcome.next_prompt)
        elif outcome.ack and not outcome.close_prompt:
            self._console(outcome.ack)

    def _close_prompt(self, key: str, summary: str) -> None:
        message_id = self._prompt_messages.pop(key, 0)
        if message_id:
            self.target.bot.edit_markup(self.target.chat, message_id, None)
        if summary:
            self._say(summary)

    # -- output ------------------------------------------------------------

    def _say(self, html_text: str, *, markup: dict[str, Any] | None = None, plain: str | None = None) -> int:
        self._console(plain if plain is not None else telegram.strip_html(html_text))
        try:
            return self.target.bot.send_message(
                self.target.chat, html_text, thread_id=self.target.thread, reply_markup=markup
            )
        except (telegram.TelegramError, telegram.Unreachable) as exc:
            log.write(f"telegram: send failed: {exc}")
            self._console(t("tg.send_failed", error=exc))
            return 0

    def _console(self, text: str) -> None:
        if sys.stdout is None:
            return  # spawned without a window: nothing to mirror into
        with contextlib.suppress(OSError, ValueError):
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def _banner(self) -> str:
        label = Accounts.load().ensure(self.slot).label
        return t(
            "tg.started",
            slot=self.slot,
            label=_plain(label),
            cwd=_plain(str(self.cwd)),
            alias=self.alias or "—",
            session=self.session_id[:8],
        )

    def _status(self) -> str:
        assert self.driver is not None
        state = t("tg.state_busy") if self.driver.turn_active else t("tg.state_idle")
        if self.prompter is not None and self.prompter.open:
            state = t("tg.state_waiting")
        return t(
            "tg.status",
            alias=self.alias or "—",
            slot=self.slot,
            cwd=_plain(str(self.cwd)),
            session=self.session_id,
            model=self.driver.model or "?",
            state=state,
            uptime=int(time.time() - self.driver.started_at) // 60,
        )

    def _usage(self) -> str:
        accounts = Accounts.load()
        slot = accounts.ensure(self.slot)
        fresh = usage.refresh_slots([slot], refresh_timeout=usage.INTERACTIVE_REFRESH_TIMEOUT)
        if fresh:
            slot.usage = fresh[self.slot]
            update_accounts(lambda current: setattr(current.ensure(self.slot), "usage", fresh[self.slot]))
        return f"<b>[{self.slot}] {_plain(slot.label)}</b>\n<code>{_plain(usage.describe(slot))}</code>"

    def _help(self) -> str:
        return t("tg.help", commands="  ".join(HELP_COMMANDS), alias=self.alias or "")


def _tool_line(data: dict[str, Any]) -> str:
    tool_input = data.get("input") or {}
    for key in ("command", "file_path", "path", "pattern", "url", "description", "prompt", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value.replace("\n", " ")[:160]
    return ""


def resolve_dir(raw: str, base: Path, *, roots: list[Any]) -> Path | None:
    candidate = Path(os.path.expanduser(raw.strip().strip('"')))
    if not candidate.is_absolute():
        candidate = base / candidate
    with contextlib.suppress(OSError):
        candidate = candidate.resolve()
    if not candidate.is_dir():
        return None
    allowed = [Path(str(root)).expanduser() for root in roots if str(root).strip()]
    if allowed:
        for root in allowed:
            with contextlib.suppress(OSError, ValueError):
                candidate.relative_to(root.resolve())
                return candidate
        return None
    return candidate


__all__ = ["ChatTarget", "Session", "resolve_dir"]
