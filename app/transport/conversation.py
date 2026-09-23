"""One claude, talking to one person in one chat.

A conversation owns a headless claude (`Driver`), renders what it does into
its chat and into the console window, and feeds it what that person types.
Questions and permission prompts go through `Prompter`. Who a message
belongs to is settled before it gets here -- the transport routes by profile
and by `(chat, thread, user)`, and simply calls `deliver()`.

A few lines are the conversation's own, not claude's: `/stop`, `/new`,
`/clear`, `/exit`, `/kill`, `/status`, `/usage`, `/cd`, `/pwd`, `/switch`,
`/save`, `/help`, the mode switches, and `!cmd`, which the TUI would have run
in its bash mode. Several may open one line -- `/new /plan Давай…` -- and they
run in order before what is left goes to claude as the prompt.

Files come and go as files. What the person attaches is saved into the
directory claude works in, and the paths are added under the prompt. What
the agent makes as an artifact follows its answer into the chat as the file
itself, with the claude.ai link when there was one.

Every message the session sends is remembered and labelled, because two of
those commands tidy the chat up afterwards: `/clear` takes the whole
conversation down and starts a fresh session in an empty chat, `/exit`
leaves the record -- the plan, the artifacts and the last thing the agent
said -- and closes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from app import autoswitch, wrapper
from app.transport import profiles as profiles_module
from app.transport import prompter as prompter_module
from app.transport.driver import Driver, DriverError, Event
from app.transport.profiles import Profile
from app.transport.prompter import Outcome, Prompt, Prompter
from app.transport.routing import split_commands
from core import hookbus, log, telegram
from core.sessions import update_session
from core.store import Accounts, Config, update_accounts
from system import shell
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
# How often a session parked on a limit reset asks again where to go. Each
# ask is a usage request per account, and the thing it is waiting for moves
# in hours, so once every few minutes is as often as it is worth asking.
LIMIT_RECHECK_SECONDS = 300.0
HELP_COMMANDS = (
    "/stop", "/new", "/clear", "/exit", "/kill", "/status", "/usage", "/pwd", "/cd", "/switch", "/save", "/help",
)

# What a message the session sent was, so that `/clear` and `/exit` can tell
# them apart long after they were sent.
MSG_SERVICE = "service"  # prompts, mode lines, limit notices
MSG_ANSWER = "answer"  # what the agent said
MSG_PLAN = "plan"  # the plan: the prompt, and the message it was written into
MSG_ARTIFACT = "artifact"  # an artifact the agent made, as a file or a link

# A published artifact's address, in whatever the Artifact tool answered.
ARTIFACT_URL_RE = re.compile(r"https://claude\.ai/(?:code/)?artifact/[\w-]+")
# Files are sent in albums no heavier than this, well inside what one upload
# to the Bot API may carry.
ARTIFACT_BATCH_BYTES = 45 * 1024 * 1024

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
# Prompts that are taken down once answered when the profile is not in
# debug: a permission asked and given is not worth a line in the chat.
EPHEMERAL_PROMPTS = frozenset({prompter_module.KIND_PERMISSION, prompter_module.KIND_ELICIT})


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
    # What to say to the resumed session first. A move to another slot cuts a
    # turn short; without a nudge the new claude sits there, resumed and
    # idle, until the person writes again.
    prompt: str = ""
    # Start over instead of resuming: a new session id in the same directory
    # and on the same slot, the way `/clear` does it in the TUI -- `/new`
    # here, since `/clear` also empties the chat.
    fresh: bool = False
    # The mode the new claude starts in, when a mode switch followed the
    # command that caused the relaunch (`/new /plan …`): it goes on the
    # command line, since the control channel is not there yet to ask.
    mode: str = ""
    # Own commands that came after the relaunching one and are better run
    # against the new claude than the one about to go.
    after: list[str] = field(default_factory=list)
    # The moment before which the relaunch does not start: every account is in
    # limit and we are sitting out the reset of the one that opens first.
    # Until then claude stays up, the chat keeps answering, and whatever the
    # person writes collects in `prompt`.
    not_before: float = 0.0

    def pending(self) -> bool:
        """Scheduled, but not due yet."""
        return self.not_before > time.time()


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
        # The profile's arguments and the launch's, already merged by the
        # transport; `defaultArgs` joins them in the driver.
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
        # Every prompt shown: the message it went into and the text in it,
        # so the answer can be written under the question.
        self._prompt_messages: dict[str, tuple[int, str]] = {}
        # Everything this session put in the chat, oldest first, with what it
        # was (MSG_*): `/clear` and `/exit` clean up by these labels.
        self._messages: list[tuple[int, str]] = []
        self._relaunch: Relaunch | None = None
        self._closing = False
        self._close_reason = ""
        self._last_typing = 0.0
        self._last_interrupt = 0.0
        self._reply_to = 0
        self.bypass_available = profile.bypass_available(config, self.args)
        self.mode = profile.resolve_mode(config, cwd, self.args)
        # The one message a turn is written into when the profile keeps its
        # messages collapsed: id, what is in it, and when it was last edited.
        self._live_id = 0
        self._live_text = ""
        self._live_edited = 0.0
        self._live_pending = ""
        self._mode_seen = False
        # Messages about a limit, taken down once the switch has happened.
        self._limit_messages: list[int] = []
        # When the target of a parked wait was last re-elected.
        self._limit_rechecked = 0.0
        self._opening_prompt = ""
        self._opening_lines: list[str] = []
        self._start_mode = ""
        # The turn's artifacts: Artifact calls not answered yet (call id ->
        # file), and what goes to the chat once the answer is in.
        self._artifact_calls: dict[str, Path] = {}
        self._turn_files: list[Path] = []
        self._turn_links: list[str] = []
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

    @property
    def waiting_out_limit(self) -> bool:
        """Parked on a quota reset: silent, but not the person's silence."""
        return self._relaunch is not None and self._relaunch.pending()

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
                    profile=self.profile.id,
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
                self._opening_prompt = relaunch.prompt
                self._opening_lines = list(relaunch.after)
                self._start_mode = relaunch.mode
                if relaunch.fresh:
                    self.session_id = ""
                self.resume = not relaunch.fresh
        finally:
            self._closing = True
            if self._close_reason:
                self._say(self._close_reason)
            if self._on_finished is not None:
                with contextlib.suppress(Exception):
                    self._on_finished(self)

    def close(self, reason: str = "") -> None:
        """Ask the conversation to wind down; the thread does the rest.

        `reason` is said in the chat once the claude is gone -- the person
        should know their next line starts a fresh session.
        """
        self._close_reason = reason
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

        # The mode is an argument only when someone asked for one: the
        # profile, or a switch that rode along with `/clear`. Left alone,
        # claude starts in whatever its own settings say.
        args = list(self.args)
        start_mode, self._start_mode = self._start_mode, ""
        wanted = start_mode or self.profile.mode
        if wanted and "--permission-mode" not in args:
            args = ["--permission-mode", wanted, *args]

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
        self.prompter = Prompter(driver, timeout_seconds=self.prompt_timeout, bypass_available=self.bypass_available)
        try:
            driver.start()
        except DriverError as exc:
            self._say(t("tg.launch_failed", error=exc))
            wrapper.close_hook_bus(self.bus)
            self.exit_code = 2
            return None
        self.session_id = driver.session_id
        update_session(session_id=self.session_id, cwd=str(self.cwd), slot=self.slot, key=self.tag)

        # The chat hears nothing on a start or a relaunch: what slot and
        # directory a conversation runs in is what the console window is
        # for, and the mode rides on every answer.
        self._console(self._banner() if not self.resume else t("tg.resumed", slot=self.slot, cwd=self.cwd))
        for line in self._opening_lines:
            self._on_line(line, source="relaunch")
        self._opening_lines = []
        if self._opening_prompt:
            # stdin is buffered until claude reads it, so this can go right
            # after the launch; the answer arrives like any other turn.
            with contextlib.suppress(DriverError):
                driver.send_user(self._opening_prompt)
            self._opening_prompt = ""

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
                if self._due():
                    return
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
            if self._due():
                return

    def _due(self) -> bool:
        """Time to leave `_loop`: we are closing, or a relaunch is ready.

        A relaunch parked on a limit reset is not ready -- claude stays up and
        the chat keeps working until the moment it was scheduled for.
        """
        if self._closing:
            return True
        return self._relaunch is not None and not self._relaunch.pending()

    def _tick(self) -> None:
        assert self.driver is not None and self.prompter is not None
        if self.driver.turn_active:
            self.last_seen = time.time()  # a long turn is not an idle one
        if self._relaunch is not None and self._relaunch.not_before:
            if self._relaunch.pending():
                self._wait_out_limit()
            else:
                self._limit_expired()
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
        # A subagent's hooks carry the subagent's mode (an Explore in plan
        # mode reports `dontAsk`); only the main thread's says where the
        # session is.
        mode = event.payload.get("permission_mode")
        if isinstance(mode, str) and mode and not event.payload.get("agent_id"):
            self._inbox.put(("mode", mode))
        if event.name == "StopFailure" and event.payload.get("error") == "rate_limit":
            wall = Event(
                "rate_limit",
                {"status": "rejected", "message": str(event.payload.get("last_assistant_message") or "")},
            )
            self._inbox.put(("event", (self.driver, wall)))
        return None

    def _on_incoming(self, incoming: telegram.Incoming) -> None:
        assert self.driver is not None and self.prompter is not None
        if incoming.is_callback:
            data = incoming.callback_data
            if not data.startswith(self.tag + ":"):
                return
            if self.target.user and incoming.user_id != self.target.user:
                # The buttons are in a shared chat, the dialog is not: what
                # somebody else presses is not this person's answer.
                self.target.bot.answer_callback(incoming.callback_id, t("tg.not_your_dialog"))
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
        if incoming.urgent and (self.driver.turn_active or self.prompter.open):
            # `/rik! ...`: a message during a turn would wait for the turn to
            # end; this one is meant to end it.
            self.driver.interrupt()
            self._console(t("tg.interrupted"))
            if self.profile.debug:
                self._say(t("tg.interrupted"))
        self._retire_live()
        text = incoming.text
        if incoming.files:
            saved = self._fetch(incoming.files)
            if saved:
                paths = "\n".join(str(path) for path in saved)
                text = f"{text.rstrip()}\n\n{paths}" if text.strip() else paths
        self._on_line(text, source="telegram", user=incoming.username or str(incoming.user_id))

    def _fetch(self, files: tuple[telegram.Attachment, ...]) -> list[Path]:
        """Save what the person attached where claude works; the paths.

        A name already taken gets a number, the way a browser saves a second
        download -- nothing the agent or the person put there is overwritten.
        A file that cannot be had is said so in the chat and left out; the
        rest of the message still goes through.
        """
        saved: list[Path] = []
        for item in files:
            if item.size > telegram.DOWNLOAD_LIMIT_BYTES:
                self._say(t("tg.file_too_big", name=_plain(item.name)))
                continue
            target: Path | None = None
            try:
                remote = self.target.bot.get_file(item.file_id)
                target, handle = claim_file(self.cwd, item.name)
                with handle:
                    self.target.bot.download(remote, handle)
            except (telegram.TelegramError, telegram.Unreachable, OSError) as exc:
                if target is not None:
                    with contextlib.suppress(OSError):
                        target.unlink()
                log.write(f"telegram: could not fetch {item.name}: {exc}")
                self._say(t("tg.file_failed", name=_plain(item.name), error=_plain(str(exc))))
                continue
            self._console(f"📎 {target}")
            saved.append(target)
        return saved

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

        if source == "telegram":
            self._console(f"[{user}] {line}")

        commands, rest = split_commands(line)
        for head, argument in commands:
            if (
                self._relaunch is not None
                and not self._relaunch.pending()
                and head not in RELAUNCH_COMMANDS
            ):
                # The claude this would run against is about to be replaced;
                # the new one gets the line. A relaunch parked on a limit
                # reset is hours away, and the claude it will replace is up
                # and able to answer `/status` or `/usage` right now.
                self._relaunch.after.append(f"{head} {argument}".strip())
                continue
            self._own_command(head, argument, then_prompt=bool(rest) and not rest.startswith("!"))
            if self._closing:
                return
        if not rest:
            return
        if self._relaunch is not None:
            if rest.startswith("!"):
                # A shell line has nothing to do with the claude being
                # replaced; during a parked wait it can simply run.
                if self._relaunch.pending():
                    self._shell(rest[1:].strip())
                else:
                    self._relaunch.after.append(rest)
                return
            # The last line typed wins: it becomes the first thing the
            # resumed session is asked.
            self._relaunch.prompt = rest
            if self._relaunch.pending():
                self._say(
                    t("tg.limit_wait_queued", opens=autoswitch.stamp(self._relaunch.not_before))
                )
            return
        if rest.startswith("!"):
            self._shell(rest[1:].strip())
            return
        try:
            self.driver.send_user(rest)
        except DriverError as exc:
            self._say(t("tg.not_running", error=exc))

    # -- the session's own commands ---------------------------------------

    def _own_command(self, head: str, tail: str, *, then_prompt: bool = False) -> None:
        assert self.driver is not None
        if head == "/stop":
            self.driver.interrupt()
            self._say(t("tg.interrupted"))
        elif head == "/kill":
            self._say(t("tg.bye", session=self.session_id[:8]))
            self._closing = True
        elif head in {"/exit", "/quit"}:
            # Done with this piece of work: the plan and the last thing the
            # agent said are what anyone would come back to read, the rest
            # was scaffolding.
            self._retire_live()
            self._purge(keep_plan=True, keep_last_answer=True)
            self._console(t("tg.bye", session=self.session_id[:8]))
            self._closing = True
        elif head == "/new":
            # Headless claude has no /clear of its own: the line would go in
            # as a prompt. A fresh session is the same thing done here.
            self._retire_live()
            self._say(t("tg.cleared"))
            self._plan_relaunch(fresh=True)
        elif head == "/clear":
            # Start over with nothing left of the old conversation, in the
            # chat as well as in claude's context.
            self._retire_live()
            self._purge()
            self._plan_relaunch(fresh=True)
        elif head == "/status":
            self._say(self._status())
        elif head == "/help":
            self._say(self._help())
        elif head == "/usage":
            self._say(self._usage())
        elif head == "/pwd":
            self._say(f"<code>{_plain(str(self.cwd))}</code>")
        elif head == "/cd":
            self._change_dir(tail)
        elif head == "/switch":
            self._switch(tail)
        elif head in MODE_COMMANDS:
            self._set_mode(MODE_COMMANDS[head], then_prompt=then_prompt)
        elif head == "/model":
            self._control(lambda: self.driver.set_model(tail) if self.driver else None, t("tg.model_set", model=tail or "default"))
        elif head == "/mode":
            self._set_mode(tail, then_prompt=then_prompt)
        elif head == "/save":
            self._save_profile()

    def _plan_relaunch(self, **fields: Any) -> Relaunch:
        """Add to the relaunch this line has already asked for, or start one:
        `/clear /cd X /plan` is one relaunch, not three.

        A relaunch parked on a limit reset loses its delay here unless the
        caller names one again: `/switch`, `/cd`, `/new` are asks for
        something to happen now, and an inherited `not_before` would silently
        hold them back for hours.
        """
        if self._relaunch is None:
            self._relaunch = Relaunch()
        fields.setdefault("not_before", 0.0)
        for name, value in fields.items():
            setattr(self._relaunch, name, value)
        return self._relaunch

    def _set_mode(self, mode: str, *, then_prompt: bool = False) -> None:
        """Ask claude to change mode, and only then believe it changed."""
        assert self.driver is not None
        wanted = mode.strip()
        if not wanted:
            self._say(t("tg.mode_now", mode=mode_line(self.mode)))
            return
        if self._relaunch is not None:
            # The switch belongs to the claude about to start; it goes on
            # its command line. A wait on a limit reset is kept: choosing a
            # mode is not a reason to stop waiting for the quota.
            self._plan_relaunch(mode=wanted, not_before=self._relaunch.not_before)
        else:
            try:
                self.driver.set_permission_mode(wanted)
            except DriverError as exc:
                self._say(t("tg.control_failed", error=exc))
                return
        self.mode = wanted
        self._mode_seen = True
        self._console(t("tg.mode_now", mode=mode_line(wanted)))
        text = t("tg.mode_chosen", mark=mode_mark(wanted), mode=wanted)
        if then_prompt:
            text += t("tg.mode_chosen_go")
        self._say(text)

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
            completed = shell.run(command, cwd=self.cwd, timeout=SHELL_TIMEOUT_SECONDS)
            stdout, stderr = completed.stdout, completed.stderr
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
        self._plan_relaunch(cwd=target)

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
            election = self._elect(accounts)
            if election.target is None or election.must_wait():
                self._say(t("tg.switch_none"))
                return
            number = election.target
        if number == self.slot:
            self._say(t("tg.switch_same", slot=number))
            return
        self._go_to_slot(number, accounts)

    def _go_to_slot(self, number: int, accounts: Accounts) -> None:
        """Move to `number` as soon as the loop comes round."""
        # The limit messages and their button have done their job: one line
        # saying what happened is all that stays in the chat.
        self._drop_limit_messages()
        self._say(
            t("tg.limit_over", slot=number)
            if number == self.slot
            else t(
                "tg.limit_switched",
                from_slot=self.slot,
                to_slot=number,
                label=_plain(accounts.ensure(number).label),
            )
        )
        # A line typed while the session was parked on a reset is already
        # waiting to be said; the configured nudge is only for a relaunch
        # nobody asked anything of.
        queued = self._relaunch.prompt if self._relaunch is not None else ""
        self._plan_relaunch(
            slot=number,
            prompt=queued or str(self.config.auto_switch.get("resumePrompt") or "").strip(),
        )

    def _drop_limit_messages(self) -> None:
        for message_id in self._limit_messages:
            self._drop(message_id)
        self._limit_messages = []

    def _elect(self, accounts: Accounts) -> autoswitch.Election:
        threshold = float(self.config.auto_switch.get("threshold") or 95)
        wrapper._refresh_candidates(accounts, skip=set())
        return autoswitch.elect_target(
            accounts,
            current=self.slot,
            tried=set(self.tried),
            threshold=threshold,
            strategy=str(self.config.auto_switch.get("strategy") or "limits"),
        )

    def _save_profile(self) -> None:
        """Pin what this conversation is doing to its profile."""
        chats = tuple({*self.profile.chats, (self.target.chat, self.target.thread)})
        saved = dataclasses.replace(self.profile, chats=chats, cwd=str(self.cwd), slot=self.slot)
        profiles_module.save(saved)
        self.profile = saved
        self._say(t("tg.saved", name=saved.label, cwd=self.cwd, slot=self.slot))

    # -- events from claude -----------------------------------------------

    def _on_event(self, event: Event) -> None:
        assert self.prompter is not None
        data = event.data
        debug = self.profile.debug

        if event.kind == "init":
            self._console(t("tg.console_ready", session=self.session_id[:8], model=data.get("model") or "?"))
            self._note_mode(str(data.get("permission_mode") or ""))
        elif event.kind == "text":
            if data.get("subagent") and not debug:
                return
            self._write(str(data["text"]))
        elif event.kind == "tool_use":
            self._note_artifact_call(data)
            line = f"🔧 {data['name']} {_tool_line(data)}".rstrip()
            if not debug:
                self._console(line)  # the window keeps the log the chat refused
                return
            self._say(f"🔧 <b>{_plain(str(data['name']))}</b> <code>{_plain(_tool_line(data))}</code>")
        elif event.kind == "tool_result":
            self._note_artifact_result(data)
            content = str(data.get("content") or "").strip()
            mark = "❌" if data.get("is_error") else "↩"
            if not debug:
                if content:
                    self._console(f"{mark} {content[:TOOL_RESULT_PREVIEW]}")
                return
            if content:
                self._say(f"{mark} <pre>{_plain(content[:TOOL_RESULT_PREVIEW])}</pre>")
        elif event.kind == "ask":
            if self._auto_allowed(event):
                return
            self._show_prompt(self.prompter.on_ask(event))
        elif event.kind == "elicit":
            self._show_prompt(self.prompter.on_elicit(event))
        elif event.kind == "cancel":
            key, kind = self.prompter.on_cancel(str(data.get("request_id") or ""))
            if key:
                self._close_prompt(key, t("tg.prompt_cancelled"), kind=kind)
        elif event.kind == "result":
            self._flush_live()
            self._note_links(str(data.get("text") or ""))
            self._send_artifacts()
            if data.get("api_error_status") == 429 or data.get("terminal_reason") == "rate_limit":
                self._rate_limited(str(data.get("text") or ""))
            elif data.get("is_error") and data.get("text"):
                self._say(f"⚠️ {_plain(str(data['text']))}")
            elif debug:
                self._say(t("tg.turn_done", seconds=int(data.get("duration_ms", 0) / 1000), cost=f"{data.get('cost', 0):.3f}"))
        elif event.kind == "rate_limit":
            if data.get("status") == "rejected":
                self._rate_limited(str(data.get("message") or ""), window=str(data.get("window") or ""))
        elif event.kind == "exit":
            self._flush_live()
            self._send_artifacts()
            if self._relaunch is None and not self._closing:
                code = int(data.get("code") or 0)
                self.exit_code = code
                tail = str(data.get("stderr") or "")
                self._say(t("tg.ended", code=code) + (f"\n<pre>{_plain(tail[:500])}</pre>" if tail and code else ""))

    # -- artifacts -----------------------------------------------------------

    def _note_artifact_call(self, data: dict[str, Any]) -> None:
        """Remember the file an Artifact publish is about.

        Taken from the call rather than its result: with publishing refused
        the result is only the refusal, and the file is on disk either way.
        """
        tool_input = data.get("input")
        if not hookbus.is_artifact_publish(str(data.get("name") or ""), tool_input):
            return
        assert isinstance(tool_input, dict)
        file_path = tool_input.get("file_path")
        if tool_input.get("asset") or not isinstance(file_path, str) or not file_path.strip():
            return
        path = Path(os.path.expanduser(file_path.strip()))
        if not path.is_absolute():
            path = self.cwd / path
        self._artifact_calls[str(data.get("id") or "")] = path

    def _note_artifact_result(self, data: dict[str, Any]) -> None:
        path = self._artifact_calls.pop(str(data.get("id") or ""), None)
        if path is None:
            return
        if path not in self._turn_files:
            self._turn_files.append(path)
        self._note_links(str(data.get("content") or ""))

    def _note_links(self, text: str) -> None:
        for link in ARTIFACT_URL_RE.findall(text):
            if link not in self._turn_links:
                self._turn_links.append(link)

    def _send_artifacts(self) -> None:
        """After the answer: the turn's artifacts as files, then their links.

        Files go in albums of up to ten and a few dozen megabytes; one that
        cannot be sent -- gone from disk, or over what a bot may upload -- is
        named by its path in the message with the links instead. Everything
        here is kept by `/exit`, the way the plan is.
        """
        files, links = self._turn_files, self._turn_links
        self._turn_files, self._turn_links = [], []
        self._artifact_calls = {}
        if not files and not links:
            return

        notes: list[str] = []
        batches: list[list[Path]] = []
        batch: list[Path] = []
        weight = 0
        for path in files:
            try:
                size = path.stat().st_size if path.is_file() else -1
            except OSError:
                size = -1
            if size < 0:
                notes.append(t("tg.artifact_missing", path=_plain(str(path))))
                continue
            if size > telegram.UPLOAD_LIMIT_BYTES:
                notes.append(t("tg.artifact_too_big", path=_plain(str(path))))
                continue
            if batch and (len(batch) >= telegram.MEDIA_GROUP_LIMIT or weight + size > ARTIFACT_BATCH_BYTES):
                batches.append(batch)
                batch, weight = [], 0
            batch.append(path)
            weight += size
        if batch:
            batches.append(batch)

        sent = False
        for group in batches:
            failed = self._send_files(group)
            notes.extend(failed)
            sent = sent or len(failed) < len(group)
        lines = [f"🔗 {_plain(link)}" for link in links] + notes
        if lines:
            sent = bool(self._say("\n".join(lines), kind=MSG_ARTIFACT)) or sent
        if sent:
            # Whatever the agent writes next starts below the files, not in
            # the answer above them.
            self._retire_live()

    def _send_files(self, paths: list[Path]) -> list[str]:
        """One album, or one document; the notes for what did not go."""
        loaded: list[tuple[Path, bytes]] = []
        notes: list[str] = []
        for path in paths:
            try:
                loaded.append((path, path.read_bytes()))
            except OSError:
                notes.append(t("tg.artifact_missing", path=_plain(str(path))))
        if not loaded:
            return notes
        bot = self.target.bot
        try:
            if len(loaded) == 1:
                path, content = loaded[0]
                ids = [bot.send_document(self.target.chat, path.name, content, thread_id=self.target.thread)]
            else:
                ids = bot.send_media_group(
                    self.target.chat,
                    [(path.name, content) for path, content in loaded],
                    thread_id=self.target.thread,
                )
        except (telegram.TelegramError, telegram.Unreachable) as exc:
            log.write(f"telegram: artifact upload failed: {exc}")
            return notes + [
                t("tg.artifact_send_failed", path=_plain(str(path)), error=_plain(str(exc)))
                for path, _ in loaded
            ]
        for message_id in ids:
            self._remember(message_id, MSG_ARTIFACT)
        for path, _ in loaded:
            self._console(f"📎 {path}")
        return notes

    def _auto_allowed(self, event: Event) -> bool:
        """Plan mode with bypass: run the tool instead of asking.

        In the TUI a session started with permissions bypassed keeps working
        through plan mode without prompts; headless claude turns each edit
        and command into a request instead. Answering yes here is what makes
        the chat behave like the terminal. Questions, the plan itself and
        MCP requests still reach the person.
        """
        assert self.prompter is not None
        if self.mode != "plan" or not self.bypass_available:
            return False
        if self.prompter.kind_of(event) != prompter_module.KIND_PERMISSION:
            return False
        self.prompter.allow(event)
        name = str(event.data.get("tool_name") or "")
        self._console(t("tg.auto_allowed", tool=name))
        if self.profile.debug:
            self._say(t("tg.auto_allowed", tool=_plain(name)))
        return True

    def _note_mode(self, mode: str) -> None:
        """Remember the mode claude is in, and say so when it changes.

        Three things know it: the session's own init, every hook payload, and
        our own switches -- so it stays right even when claude changes mode on
        its own, which is what leaving plan mode does. The console always
        hears about a change; the chat only in debug, since the switches a
        person asked for announce themselves.
        """
        if not mode or mode == self.mode:
            return
        known = self._mode_seen
        self._mode_seen = True
        self.mode = mode
        self._console(t("tg.mode_now", mode=mode_line(mode)))
        if known and self.profile.debug:
            self._say(mode_line(mode))

    _rate_limit_announced = 0.0

    def _rate_limited(self, message: str, *, window: str = "") -> None:
        if self._relaunch is not None and self._relaunch.pending():
            # Already parked on this wall; claude retries a 429 several times
            # and every retry comes back through here.
            return
        if time.time() - self._rate_limit_announced < 30:
            return
        self._rate_limit_announced = time.time()
        accounts = Accounts.load()
        election = self._elect(accounts)
        target = election.target
        lines = [t("tg.rate_limited", slot=self.slot, window=window or "?")]
        if message:
            lines.append(f"<i>{_plain(message)}</i>")
        markup = None
        if target is not None and election.must_wait():
            # Nobody is free. The slot that opens first may well be another
            # account still in limit, or this one -- either way the session
            # waits it out here instead of being answered "no slot left" and
            # left to the person to restart.
            self._park_until(target, election.available_at)
            lines.append(
                t(
                    "tg.limit_waiting",
                    slot=target,
                    label=_plain(accounts.ensure(target).label),
                    opens=autoswitch.stamp(election.available_at),
                )
            )
        elif target is not None:
            lines.append(t("tg.rate_limited_hint", slot=target, label=accounts.ensure(target).label))
            markup = telegram.keyboard([[(t("tg.switch_button", slot=target), f"{self.tag}:sw:{target}")]])
        else:
            lines.append(t("tg.switch_none"))
        sent = self._say("\n".join(lines), markup=markup)
        if sent:
            self._limit_messages.append(sent)

    def _park_until(self, target: int, opens: float) -> None:
        """Schedule the move to `target` for the moment it opens."""
        queued = self._relaunch.prompt if self._relaunch is not None else ""
        self._plan_relaunch(
            slot=target,
            not_before=opens + wrapper.RESET_MARGIN_SECONDS,
            prompt=queued or str(self.config.auto_switch.get("resumePrompt") or "").strip(),
        )
        self._limit_rechecked = time.time()
        log.write(
            f"transport: {self.tag} parked on slot {target}, opens {autoswitch.stamp(opens)}"
        )

    def _limit_expired(self) -> None:
        """The moment we were parked on has come: let the relaunch through."""
        assert self._relaunch is not None
        self._relaunch.not_before = 0.0
        target = self._relaunch.slot or self.slot
        self._drop_limit_messages()
        self._say(
            t("tg.limit_over", slot=target)
            if target == self.slot
            else t(
                "tg.limit_switched",
                from_slot=self.slot,
                to_slot=target,
                label=_plain(Accounts.load().ensure(target).label),
            )
        )

    def _wait_out_limit(self) -> None:
        """Sit out the reset this conversation is parked on.

        Two things happen here and nowhere else. The chat is not idle while
        the delay is ours: `Transport._retire_idle` reads `last_seen`, and a
        429 ends the turn, so without this line an hour of waiting would close
        the session the person never went quiet on.

        And the choice is made again now and then. A wait measured in hours
        outlives the snapshot it was decided from -- an account can come back
        early, or another one finish its own window first.
        """
        assert self._relaunch is not None
        self.last_seen = time.time()
        if time.time() - self._limit_rechecked < LIMIT_RECHECK_SECONDS:
            return
        self._limit_rechecked = time.time()
        accounts = Accounts.load()
        election = self._elect(accounts)
        if election.target is None:
            return
        if not election.must_wait():
            # Somebody came back early -- go now instead of sitting out a
            # reset that no longer decides anything.
            self._go_to_slot(election.target, accounts)
            return
        opens = election.available_at + wrapper.RESET_MARGIN_SECONDS
        if election.target == self._relaunch.slot and abs(opens - self._relaunch.not_before) < 1.0:
            return
        self._park_until(election.target, election.available_at)
        sent = self._say(
            t(
                "tg.limit_waiting",
                slot=election.target,
                label=_plain(accounts.ensure(election.target).label),
                opens=autoswitch.stamp(election.available_at),
            )
        )
        if sent:
            self._limit_messages.append(sent)

    # -- prompts -----------------------------------------------------------

    def _show_prompt(self, prompt: Prompt) -> None:
        is_plan = prompt.kind == prompter_module.KIND_PLAN
        if is_plan:
            self._keep_plan()
        markup = self._markup(prompt)
        message_id = self._say(prompt.text, markup=markup, kind=MSG_PLAN if is_plan else MSG_SERVICE)
        self._prompt_messages[prompt.key] = (message_id, prompt.text)
        numbered = [f"  {index + 1}) {choice.label}" for index, choice in enumerate(c for row in prompt.rows for c in row)]
        self._console("\n".join(numbered))

    def _keep_plan(self) -> None:
        """Close the message the plan was written into and leave it there.

        In a collapsed chat the turn owns one message that later text
        rewrites -- and the plan is the one thing worth rereading once the
        work has started. So it is closed here: the plan stays where it is,
        and what follows begins a new message.
        """
        if self.profile.expanded or not self._live_id:
            return
        self._flush_live()
        self._mark(self._live_id, MSG_PLAN)
        self._live_id = 0
        self._live_text = ""
        self._live_pending = ""

    def _markup(self, prompt: Prompt) -> dict[str, Any]:
        return telegram.keyboard(
            [[(choice.label, f"{self.tag}:{choice.data}") for choice in row] for row in prompt.rows]
        )

    def _apply(self, outcome: Outcome) -> None:
        if outcome.mode:
            # The answer carried the switch; claude applies it, we only stop
            # announcing it twice.
            self.mode = outcome.mode
            self._mode_seen = True
        kept = True
        if outcome.next_prompt is not None and outcome.next_prompt.key == outcome.close_prompt:
            # The same question with its buttons re-marked (a multi-select
            # toggle): redraw the keyboard, do not send the message again.
            shown = self._prompt_messages.get(outcome.close_prompt)
            if shown:
                self.target.bot.edit_markup(self.target.chat, shown[0], self._markup(outcome.next_prompt))
                return
        if outcome.close_prompt:
            kept = self._close_prompt(outcome.close_prompt, outcome.summary, kind=outcome.kind)
        if outcome.next_prompt is not None:
            self._show_prompt(outcome.next_prompt)
        elif outcome.ack and not outcome.close_prompt:
            self._console(outcome.ack)
        if outcome.finished_request and kept:
            self._relocate_live()

    def _close_prompt(self, key: str, summary: str, *, kind: str) -> bool:
        """Settle a prompt's message; True when it is still in the chat.

        A permission asked and given (or an MCP form) is noise once answered:
        outside debug it is taken down. A question and a plan are the record
        of what was decided, so they stay, with the answer written under the
        question in the same message.
        """
        message_id, text = self._prompt_messages.pop(key, (0, ""))
        if not message_id:
            if summary:
                self._say(summary)
            return True
        if kind in EPHEMERAL_PROMPTS and not self.profile.debug:
            self._drop(message_id)
            self._console(telegram.strip_html(summary))
            return False
        rewritten = f"{text}\n\n{summary}" if summary else text
        self._console(telegram.strip_html(summary))
        try:
            gone = not self.target.bot.edit_text(self.target.chat, message_id, rewritten)
        except (telegram.TelegramError, telegram.Unreachable) as exc:
            log.write(f"telegram: edit failed: {exc}")
            self.target.bot.edit_markup(self.target.chat, message_id, None)
            return True
        if gone and summary:
            self._say(summary)
        return True

    # -- the message a turn is written into ---------------------------------

    def _answer(self, text: str) -> str:
        """What the agent said, marked with the mode it was said in.

        The mark is on every answer rather than on a line of its own: the
        session has no greeting to carry it, and the mode is exactly the
        thing a person wants to know before reading what the agent did.
        Prompts and questions keep their own marks.
        """
        return f"{mode_mark(self.mode)} {_esc(text)}"

    def _write(self, text: str) -> None:
        """A block of the agent's text, as this profile wants it shown.

        Expanded: one message per block, as before. Collapsed: the turn owns
        one message and the newest block replaces what is in it -- what the
        agent says last is almost always the part worth reading.
        """
        if self.profile.expanded:
            self._say(self._answer(text), plain=text, kind=MSG_ANSWER)
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
        rendered = self._answer(text)

        if not self._live_id:
            self._live_id = self._say(rendered, plain=text, kind=MSG_ANSWER)
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
            self._forget(self._live_id)
            self._live_id = self._say(rendered, plain=text, kind=MSG_ANSWER)

    def _relocate_live(self) -> None:
        """Move the turn's message below whatever was just answered.

        A question lands under the working message and stays there once
        answered -- that is fine, it is the record of what was asked. But the
        work that follows should read at the bottom, so the message is taken
        down and put back with the same text. A prompt that was deleted
        instead leaves the working message where it was, and this is not
        called.
        """
        if self.profile.expanded or not self._live_id:
            return
        self._drop(self._live_id)
        self._live_id = 0
        if self._live_text:
            self._live_id = self._say(
                self._answer(self._live_text), plain=None, mirror=False, kind=MSG_ANSWER
            )

    def _retire_live(self) -> None:
        """A new turn starts: close the old message and drop its buttons."""
        self._flush_live()
        if self._live_id:
            self.target.bot.edit_markup(self.target.chat, self._live_id, None)
        self._live_id = 0
        self._live_text = ""
        self._live_pending = ""

    # -- what is in the chat -------------------------------------------------

    def _remember(self, message_id: int, kind: str) -> None:
        if message_id:
            self._messages.append((message_id, kind))

    def _forget(self, message_id: int) -> None:
        self._messages = [entry for entry in self._messages if entry[0] != message_id]

    def _mark(self, message_id: int, kind: str) -> None:
        self._messages = [
            (found, kind if found == message_id else was) for found, was in self._messages
        ]

    def _drop(self, message_id: int) -> None:
        """Take one of our messages out of the chat and off the books."""
        self.target.bot.delete_message(self.target.chat, message_id)
        self._forget(message_id)

    def _purge(self, *, keep_plan: bool = False, keep_last_answer: bool = False) -> None:
        """Take this session's messages out of the chat.

        `/clear` keeps nothing: the chat ends up as it was before the
        conversation started. `/exit` keeps what someone would come back to
        read -- every plan, every artifact, and the last thing the agent
        said. A turn cut short has no last answer, and then only the plans
        and the artifacts stay.
        """
        kept: list[tuple[int, str]] = []
        doomed = list(self._messages)
        if keep_last_answer:
            last = next((entry for entry in reversed(doomed) if entry[1] == MSG_ANSWER), None)
            if last is not None:
                doomed.remove(last)
                kept.append(last)
        if keep_plan:
            for entry in [entry for entry in doomed if entry[1] in (MSG_PLAN, MSG_ARTIFACT)]:
                doomed.remove(entry)
                kept.append(entry)
        for message_id, _ in doomed:
            self.target.bot.delete_message(self.target.chat, message_id)
        self._messages = sorted(kept)
        surviving = {message_id for message_id, _ in kept}
        self._prompt_messages = {
            key: shown for key, shown in self._prompt_messages.items() if shown[0] in surviving
        }
        self._limit_messages = [found for found in self._limit_messages if found in surviving]
        self._live_id = 0
        self._live_text = ""
        self._live_pending = ""

    # -- output ------------------------------------------------------------

    def _say(
        self,
        html_text: str,
        *,
        markup: dict[str, Any] | None = None,
        plain: str | None = None,
        mirror: bool = True,
        kind: str = MSG_SERVICE,
    ) -> int:
        if mirror:
            self._console(plain if plain is not None else telegram.strip_html(html_text))
        reply_to, self._reply_to = self._reply_to, 0
        try:
            sent = self.target.bot.send_message(
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
        self._remember(sent, kind)
        return sent

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
            alias=self.profile.command,
            session=self.session_id[:8],
        ))

    def _status(self) -> str:
        assert self.driver is not None
        state = t("tg.state_busy") if self.driver.turn_active else t("tg.state_idle")
        if self.prompter is not None and self.prompter.open:
            state = t("tg.state_waiting")
        if self._relaunch is not None and self._relaunch.pending():
            state = t(
                "tg.state_limited",
                slot=self._relaunch.slot or self.slot,
                opens=autoswitch.stamp(self._relaunch.not_before),
            )
        return t(
            "tg.status",
            mode=mode_line(self.mode),
            alias=self.profile.command,
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
        # The marks go with the commands here, because they are what the
        # chat sees on every answer and nothing else explains them.
        return t(
            "tg.help",
            alias=self.profile.command,
            commands="  ".join(HELP_COMMANDS),
            modes="  ".join(f"{mode_mark(mode)} {command}" for command, mode in MODE_COMMANDS.items()),
            mode=mode_line(self.mode),
        )


# Own commands that end in a relaunch, and so may pile onto one another in
# a single line (`/clear /cd X /plan`); anything else typed after them waits
# for the new claude.
RELAUNCH_COMMANDS = frozenset({"/clear", "/new", "/cd", "/switch", "/mode", *MODE_COMMANDS})


def _tool_line(data: dict[str, Any]) -> str:
    tool_input = data.get("input") or {}
    for key in ("command", "file_path", "path", "pattern", "url", "description", "prompt", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value.replace("\n", " ")[:160]
    return ""


# What Windows will not have in a file name, and the names it keeps for
# devices whatever the extension.
_UNSAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
)


def safe_filename(name: str) -> str:
    """A sent file's name, fit to be created on any of our platforms."""
    base = re.split(r"[\\/]", name)[-1]
    clean = _UNSAFE_NAME_RE.sub("_", base).strip().rstrip(". ")
    if not clean:
        return "file"
    if clean.split(".", 1)[0].upper() in _RESERVED_NAMES:
        clean = f"_{clean}"
    return clean


def claim_file(directory: Path, name: str) -> tuple[Path, BinaryIO]:
    """Create `name` in `directory`, or `name (1)`, `name (2)`... when taken.

    The file is opened exclusively, so two downloads racing for one name end
    up in two files instead of one overwriting the other.
    """
    clean = safe_filename(name)
    stem, suffix = Path(clean).stem, Path(clean).suffix
    for index in itertools.count():
        candidate = directory / (clean if not index else f"{stem} ({index}){suffix}")
        try:
            return candidate, candidate.open("xb")
        except FileExistsError:
            continue
    raise AssertionError("unreachable")


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


__all__ = ["ChatTarget", "Conversation", "Relaunch", "claim_file", "resolve_dir", "safe_filename"]
