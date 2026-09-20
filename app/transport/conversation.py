"""One claude, talking to one person in one chat.

A conversation owns a headless claude (`Driver`), renders what it does into
its chat and into the console window, and feeds it what that person types.
Questions and permission prompts go through `Prompter`. Who a message
belongs to is settled before it gets here -- the transport routes by profile
and by `(chat, thread, user)`, and simply calls `deliver()`.

A few lines are the conversation's own, not claude's: `/stop`, `/kill`,
`/status`, `/usage`, `/cd`, `/pwd`, `/switch`, `/save`, `/help`, and `!cmd`,
which the TUI would have run in its bash mode. Everything else is a prompt.
"""

from __future__ import annotations

import contextlib
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import autoswitch, wrapper
from app.transport import profiles as profiles_module
from app.transport.driver import Driver, DriverError, Event
from app.transport.profiles import Profile
from app.transport.prompter import Outcome, Prompt, Prompter
from core import hookbus, log, telegram
from core.sessions import update_session
from core.store import Accounts, Config, update_accounts
from ui import usage
from ui.i18n import t

TYPING_INTERVAL_SECONDS = 4.0
TICK_SECONDS = 1.0
SHELL_TIMEOUT_SECONDS = 120.0
OUTPUT_PREVIEW_LIMIT = 3000
TOOL_RESULT_PREVIEW = 400
# A second Ctrl-C this soon after the first ends the session instead of
# interrupting the turn again.
DOUBLE_INTERRUPT_SECONDS = 3.0
HELP_COMMANDS = ("/stop", "/kill", "/status", "/usage", "/pwd", "/cd", "/switch", "/save", "/help")

# Telegram rate-limits edits to the same message; one a second or so is what
# a person reads anyway.
EDIT_INTERVAL_SECONDS = 1.2

# How claude names its permission modes, and what each looks like at a glance.
MODE_MARKS = {
    "plan": "🟡",
    "acceptEdits": "🔵",
    "auto": "🟣",
    "dontAsk": "🟣",
    "bypassPermissions": "🔴",
    "manual": "⚪",
    "default": "⚪",
}
MODE_COMMANDS = {
    "/plan": "plan",
    "/bypass": "bypassPermissions",
    "/auto": "auto",
    "/edits": "acceptEdits",
    "/ask": "manual",
}


def mode_mark(mode: str) -> str:
    return MODE_MARKS.get(mode, "⚪")


def mode_line(mode: str) -> str:
    return f"{mode_mark(mode)} {mode or 'default'}"

_esc = telegram.markdown_to_html


def _plain(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass
class Relaunch:
    cwd: Path | None = None
    slot: int | None = None


@dataclass(frozen=True)
class ChatTarget:
    """Where a conversation writes: one chat, one topic, one person.

    `user` is 0 when the profile shares a session between everyone in the
    chat; it is only ever used to tell conversations apart, never to decide
    whether a message is allowed -- that is settled before `deliver()`.
    """

    bot: telegram.Bot
    chat: int
    thread: int = 0
    user: int = 0

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.chat, self.thread, self.user)


class Conversation:
    def __init__(
        self,
        config: Config,
        *,
        slot: int,
        target: ChatTarget,
        profile: Profile,
        cwd: Path,
        args: list[str],
        session_id: str = "",
        resume: bool = False,
        tag: str = "s",
        on_console: Callable[[str], None] | None = None,
        on_finished: Callable[["Conversation"], None] | None = None,
    ) -> None:
        self.config = config
        self.slot = slot
        self.target = target
        self.profile = profile
        self.cwd = cwd
        self.args = list(args)
        self.session_id = session_id
        self.resume = resume
        # Prefixed to every button this conversation sends, so that a press
        # comes back to the claude that asked and not to its neighbour.
        self.tag = tag
        self._on_console = on_console
        self._on_finished = on_finished

        timeout_minutes = config.telegram.get("promptTimeoutMinutes")
        self.prompt_timeout = float(timeout_minutes) * 60 if isinstance(timeout_minutes, (int, float)) else 0.0

        self.driver: Driver | None = None
        self.prompter: Prompter | None = None
        self.bus: hookbus.HookBus | None = None
        self._inbox: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._prompt_messages: dict[str, int] = {}
        self._relaunch: Relaunch | None = None
        self._closing = False
        self._last_typing = 0.0
        self._last_interrupt = 0.0
        self._reply_to = 0
        self.mode = profile.resolve_mode(config, cwd)
        # The one message a turn is written into when the profile keeps its
        # messages collapsed: id, what is in it, and when it was last edited.
        self._live_id = 0
        self._live_text = ""
        self._live_edited = 0.0
        self._live_pending = ""
        self._mode_seen = False
        self.last_seen = time.time()
        self.exit_code = 0
        self.started_at = time.time()
        self.tried: set[int] = set()
        self._thread: threading.Thread | None = None

    @property
    def alias(self) -> str:
        return self.profile.alias

    @property
    def key(self) -> tuple[int, int, int]:
        return self.target.key

    # -- entry -------------------------------------------------------------

    def start(self) -> None:
        """Run the conversation on its own thread; one process holds many."""
        self._thread = threading.Thread(target=self.run, daemon=True, name=f"conv-{self.tag}")
        self._thread.start()

    def run(self) -> int:
        """Drive this claude until it ends or the chat asks it to stop."""
        try:
            while True:
                with wrapper.slot_session(
                    self.config,
                    self.slot,
                    key=self.tag,
                    transport="telegram",
                    profile=self.profile.name,
                    chat=self.target.chat,
                    thread=self.target.thread,
                    user=self.target.user,
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
            self._closing = True
            if self._on_finished is not None:
                with contextlib.suppress(Exception):
                    self._on_finished(self)

    def close(self) -> None:
        """Ask the conversation to wind down; the thread does the rest."""
        self._closing = True
        self._inbox.put(("wake", None))

    def join(self, timeout: float = 10.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run_on_slot(self) -> Relaunch | None:
        self.tried.add(self.slot)
        self._relaunch = None
        self.bus = None
        if wrapper.wants_hook_bus(self.config, self.args):
            self.bus = wrapper.open_hook_bus(self.config, self.slot, key=self.tag)
            self.bus.subscribe(self._on_hook)

        # The mode is an argument only when the profile asks for one; left
        # alone, claude starts in whatever its own settings say.
        args = list(self.args)
        if self.profile.mode and "--permission-mode" not in args:
            args = ["--permission-mode", self.profile.mode, *args]

        driver = Driver(
            self.config,
            self.slot,
            cwd=self.cwd,
            args=args,
            name=self.alias,
            session_id=self.session_id,
            resume=self.resume,
            listener=lambda event: self._inbox.put(("event", (driver, event))),
            bus=self.bus,
            settings_key=self.tag,
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
        update_session(session_id=self.session_id, cwd=str(self.cwd), slot=self.slot, key=self.tag)

        # The chat hears one greeting from the transport, not a header per
        # relaunch: what slot and directory a conversation runs in is what
        # the console window is for.
        self._console(self._banner() if not self.resume else t("tg.resumed", slot=self.slot, cwd=self.cwd))

        try:
            self._loop()
        finally:
            driver.close()
            wrapper.close_hook_bus(self.bus, key=self.tag)
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
            elif kind == "mode":
                self._note_mode(item)
            if self._relaunch is not None or self._closing:
                return

    def _tick(self) -> None:
        assert self.driver is not None and self.prompter is not None
        if self.driver.turn_active:
            self.last_seen = time.time()  # a long turn is not an idle one
        if self.driver.turn_active and time.time() - self._last_typing > TYPING_INTERVAL_SECONDS:
            self._last_typing = time.time()
            self.target.bot.typing(self.target.chat, thread_id=self.target.thread)
        for outcome in self.prompter.tick():
            self._apply(outcome)
        if self._live_pending and time.time() - self._live_edited >= EDIT_INTERVAL_SECONDS:
            self._flush_live()

    # -- inputs ------------------------------------------------------------

    def deliver(self, incoming: telegram.Incoming) -> None:
        """A message the transport has already decided is ours."""
        self.last_seen = time.time()
        self._inbox.put(("incoming", incoming))

    def _on_hook(self, event: hookbus.HookEvent) -> dict[str, Any] | None:
        mode = event.payload.get("permission_mode")
        if isinstance(mode, str) and mode:
            self._inbox.put(("mode", mode))
        if event.name == "StopFailure" and event.payload.get("error") == "rate_limit":
            wall = Event(
                "rate_limit",
                {"status": "rejected", "message": str(event.payload.get("last_assistant_message") or "")},
            )
            self._inbox.put(("event", (self.driver, wall)))
        return None

    def _on_incoming(self, incoming: telegram.Incoming) -> None:
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
        # Answering the message that triggered the turn is what tells people
        # apart when several of them share a chat.
        self._reply_to = incoming.message_id
        self._retire_live()
        self._on_line(incoming.text, source="telegram", user=incoming.username or str(incoming.user_id))

    def _on_line(self, text: str, *, source: str = "telegram", user: str = "") -> None:
        assert self.driver is not None and self.prompter is not None
        line = text.strip()
        if not line:
            self._say(self._status())
            return

        outcome = self.prompter.on_text(line)
        if outcome.consumed:
            self._apply(outcome)
            return

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
        if head in MODE_COMMANDS:
            self._set_mode(MODE_COMMANDS[head])
            return True
        if head == "/model":
            self._control(lambda: self.driver.set_model(tail) if self.driver else None, t("tg.model_set", model=tail or "default"))
            return True
        if head == "/mode":
            self._set_mode(tail)
            return True
        if head == "/save":
            self._save_profile()
            return True
        return False

    def _set_mode(self, mode: str) -> None:
        """Ask claude to change mode, and only then believe it changed."""
        assert self.driver is not None
        wanted = mode.strip()
        if not wanted:
            self._say(t("tg.mode_now", mode=mode_line(self.mode)))
            return
        try:
            self.driver.set_permission_mode(wanted)
        except DriverError as exc:
            self._say(t("tg.control_failed", error=exc))
            return
        self.mode = ""  # so the change is announced even back to a known mode
        self._mode_seen = True
        self._note_mode(wanted)

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
        """Pin what this conversation is doing to its profile."""
        chats = tuple({*self.profile.chats, (self.target.chat, self.target.thread)})
        saved = profiles_module.Profile(
            name=self.profile.name,
            chats=chats,
            cwd=str(self.cwd),
            slot=self.slot,
            daemon=self.profile.daemon,
            multi=self.profile.multi,
            users=self.profile.users,
            args=self.profile.args,
        )
        profiles_module.save(saved)
        self.profile = saved
        self._say(t("tg.saved", name=saved.name, cwd=self.cwd, slot=self.slot))

    # -- events from claude -----------------------------------------------

    def _on_event(self, event: Event) -> None:
        assert self.prompter is not None
        data = event.data
        tech = self.profile.tech

        if event.kind == "init":
            self._console(t("tg.console_ready", session=self.session_id[:8], model=data.get("model") or "?"))
            self._note_mode(str(data.get("permission_mode") or ""))
        elif event.kind == "text":
            if data.get("subagent") and not tech:
                return
            self._write(str(data["text"]))
        elif event.kind == "tool_use":
            line = f"🔧 {data['name']} {_tool_line(data)}".rstrip()
            if not tech:
                self._console(line)  # the window keeps the log the chat refused
                return
            self._say(f"🔧 <b>{_plain(str(data['name']))}</b> <code>{_plain(_tool_line(data))}</code>")
        elif event.kind == "tool_result":
            content = str(data.get("content") or "").strip()
            mark = "❌" if data.get("is_error") else "↩"
            if not tech:
                if content:
                    self._console(f"{mark} {content[:TOOL_RESULT_PREVIEW]}")
                return
            if content:
                self._say(f"{mark} <pre>{_plain(content[:TOOL_RESULT_PREVIEW])}</pre>")
        elif event.kind == "ask":
            self._show_prompt(self.prompter.on_ask(event))
        elif event.kind == "cancel":
            key = self.prompter.on_cancel(str(data.get("request_id") or ""))
            if key:
                self._close_prompt(key, t("tg.prompt_cancelled"))
        elif event.kind == "result":
            self._flush_live()
            if data.get("api_error_status") == 429 or data.get("terminal_reason") == "rate_limit":
                self._rate_limited(str(data.get("text") or ""))
            elif data.get("is_error") and data.get("text"):
                self._say(f"⚠️ {_plain(str(data['text']))}")
            elif tech:
                self._say(t("tg.turn_done", seconds=int(data.get("duration_ms", 0) / 1000), cost=f"{data.get('cost', 0):.3f}"))
        elif event.kind == "rate_limit":
            if data.get("status") == "rejected":
                self._rate_limited(str(data.get("message") or ""), window=str(data.get("window") or ""))
        elif event.kind == "exit":
            self._flush_live()
            if self._relaunch is None and not self._closing:
                code = int(data.get("code") or 0)
                self.exit_code = code
                tail = str(data.get("stderr") or "")
                self._say(t("tg.ended", code=code) + (f"\n<pre>{_plain(tail[:500])}</pre>" if tail and code else ""))

    def _note_mode(self, mode: str) -> None:
        """Remember the mode claude is in, and say so when it changes.

        Three things know it: the session's own init, every hook payload, and
        our own switches -- so it stays right even when claude changes mode on
        its own, which is what leaving plan mode does. The starting value came
        from the settings, so the first report only corrects the console.
        """
        if not mode or mode == self.mode:
            return
        known = self._mode_seen
        self._mode_seen = True
        self.mode = mode
        self._console(t("tg.mode_now", mode=mode_line(mode)))
        if known:
            self._say(mode_line(mode))

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
        if outcome.mode:
            # The answer carried the switch; claude applies it, we only stop
            # announcing it twice.
            self.mode = outcome.mode
            self._mode_seen = True
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

    # -- the message a turn is written into ---------------------------------

    def _write(self, text: str) -> None:
        """A block of the agent's text, as this profile wants it shown.

        Expanded: one message per block, as before. Collapsed: the turn owns
        one message and the newest block replaces what is in it -- what the
        agent says last is almost always the part worth reading.
        """
        if self.profile.expanded:
            self._say(_esc(text), plain=text)
            return

        self._live_pending = text
        if time.time() - self._live_edited < EDIT_INTERVAL_SECONDS:
            return  # Telegram throttles edits; the tick flushes what is left
        self._flush_live()

    def _flush_live(self) -> None:
        """Put the newest text into the turn's message, or start one."""
        text = self._live_pending
        if not text or self.profile.expanded:
            return
        self._live_pending = ""
        self._live_edited = time.time()
        self._live_text = text
        rendered = _esc(text)

        if not self._live_id:
            self._live_id = self._say(rendered, plain=text)
            return

        self._console(text)
        try:
            gone = not self.target.bot.edit_text(self.target.chat, self._live_id, rendered)
        except (telegram.TelegramError, telegram.Unreachable) as exc:
            log.write(f"telegram: edit failed: {exc}")
            return
        if gone:
            # Deleted in the chat while we were writing into it; start again
            # rather than lose the rest of the turn.
            self._live_id = self._say(rendered, plain=text)

    def _retire_live(self) -> None:
        """A new turn starts: close the old message and drop its buttons."""
        self._flush_live()
        if self._live_id:
            self.target.bot.edit_markup(self.target.chat, self._live_id, None)
        self._live_id = 0
        self._live_text = ""
        self._live_pending = ""

    # -- output ------------------------------------------------------------

    def _say(self, html_text: str, *, markup: dict[str, Any] | None = None, plain: str | None = None) -> int:
        self._console(plain if plain is not None else telegram.strip_html(html_text))
        reply_to, self._reply_to = self._reply_to, 0
        try:
            return self.target.bot.send_message(
                self.target.chat,
                html_text,
                thread_id=self.target.thread,
                reply_markup=markup,
                reply_to=reply_to,
            )
        except (telegram.TelegramError, telegram.Unreachable) as exc:
            log.write(f"telegram: send failed: {exc}")
            self._console(t("tg.send_failed", error=exc))
            return 0

    def _console(self, text: str) -> None:
        if self._on_console is not None:
            self._on_console(text)

    def _banner(self) -> str:
        label = Accounts.load().ensure(self.slot).label
        return telegram.strip_html(t(
            "tg.started",
            slot=self.slot,
            label=_plain(label),
            cwd=_plain(str(self.cwd)),
            alias=self.profile.name,
            session=self.session_id[:8],
        ))

    def _status(self) -> str:
        assert self.driver is not None
        state = t("tg.state_busy") if self.driver.turn_active else t("tg.state_idle")
        if self.prompter is not None and self.prompter.open:
            state = t("tg.state_waiting")
        return t(
            "tg.status",
            mode=mode_line(self.mode),
            alias=self.profile.name,
            slot=self.slot,
            cwd=_plain(str(self.cwd)),
            session=self.session_id,
            model=self.driver.model or "?",
            state=state,
            uptime=int(time.time() - self.started_at) // 60,
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
        return t(
            "tg.help",
            commands="  ".join(HELP_COMMANDS),
            modes="  ".join(MODE_COMMANDS),
            mode=mode_line(self.mode),
        )


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


__all__ = ["ChatTarget", "Conversation", "Relaunch", "resolve_dir"]
