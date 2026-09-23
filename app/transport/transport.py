"""One profile, running: the chats it listens to and the claudes it holds.

A transport process *is* a profile. It gets its share of the bot's updates
-- from the daemon, which owns the token, or straight from Telegram when it
is the only thing running -- and hands each one to a conversation.

Which conversation depends on the profile: with `multi` on, everyone in a
chat talks to the same claude; with it off (the default) each person gets
their own, keyed by `(chat, thread, user)`. That is the difference between
a shared workroom and a bot that answers each person privately, and it is
the one thing a group chat gets wrong by default.

The console window is a journal and nothing else: every conversation's lines
are mirrored there with a short tag, and nothing is read back from it -- a
stray keystroke in that window used to reach the agent as a prompt.
"""

from __future__ import annotations

import contextlib
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

from app.transport import daemonlink
from app.transport import poller as poller_module
from app.transport import profiles as profiles_module
from app.transport.conversation import ChatTarget, Conversation
from app.transport.profiles import Profile
from app.transport.routing import parse_address
from core import log, telegram
from core.store import Config
from system import childjob, console
from ui.i18n import t

FEED_WAIT_SECONDS = 25
FEED_RETRY_SECONDS = 2.0
FEED_FAILURES_BEFORE_TAKEOVER = 3
LOCK_REFRESH_SECONDS = 60.0
TICK_SECONDS = 1.0
DEFAULT_MAX_SESSIONS = 8
DEFAULT_IDLE_HOURS = 1.0
# Separates this transport's tag from the conversation's within it: "t1.2".
CONVERSATION_SEPARATOR = "."


class Transport:
    def __init__(
        self,
        config: Config,
        profile: Profile,
        *,
        bot: telegram.Bot,
        slot: int,
        cwd: Path,
        args: list[str],
        chat: int = 0,
        thread: int = 0,
        tag: str = "",
    ) -> None:
        self.config = config
        self.profile = profile
        self.bot = bot
        self.slot = slot
        self.cwd = cwd
        self.args = list(args)
        # A transport started with --tg-chat is pinned to that chat; without
        # it, it serves every chat its profile is open to.
        self.chat = chat
        self.thread = thread
        self.tag = tag or "s"

        limit = config.telegram.get("maxSessions")
        self.max_sessions = int(limit) if isinstance(limit, (int, float)) and limit else DEFAULT_MAX_SESSIONS
        hours = config.telegram.get("idleHours")
        hours = float(hours) if isinstance(hours, (int, float)) else DEFAULT_IDLE_HOURS
        self.idle_seconds = hours * 3600.0
        self.started_at = time.time()

        self.conversations: dict[tuple[int, int, int], Conversation] = {}
        self._lock = threading.Lock()
        self._inbox: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._stop = threading.Event()
        self._counter = 0
        self._refused: set[tuple[int, int, int]] = set()
        self.poller: poller_module.Poller | None = None
        self._console_state: Any = None
        self._last_seen = time.time()
        self._feed_port = 0
        self._last_lock_refresh = 0.0
        self.exit_code = 0

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> int:
        """Listen until every conversation is done and the chat goes quiet."""
        if not self._attach():
            return 2
        # Nothing is read from the window: a stray keystroke used to reach
        # the agent as a prompt. QuickEdit is the other half of that story --
        # a click in the window blocks every write until it is dismissed.
        self._console_state = console.quiet()
        childjob.on_close(self._detach)
        self._console(
            t(
                "tg.transport_ready",
                profile=self.profile.label,
                chats=self._chats_text(),
                cwd=self.cwd,
            )
        )
        try:
            self._loop()
        finally:
            self._detach()
        return self.exit_code

    def _attach(self) -> bool:
        """Get a feed: the daemon if it has the token, otherwise poll it."""
        from app import daemon as daemon_module

        holder = poller_module.read_holder(self.bot.id)
        if holder is None:
            # Nothing owns the token yet. The daemon is the better owner --
            # it outlives us and can serve the next profile too.
            daemon_module.ensure_running(self.config)
            holder = _wait_for_holder(self.bot.id)

        if holder is not None and int(holder.get("pid", 0) or 0) != os.getpid():
            port = int(holder.get("port") or 0)
            if port:
                self._feed_port = port
                threading.Thread(target=self._feed_loop, daemon=True, name="daemon-feed").start()
                return True
            self._console(t("tg.token_held_locally", pid=holder.get("pid")))
            return False

        if not poller_module.acquire(self.bot.id):
            self._console(t("tg.token_held_locally", pid=(holder or {}).get("pid")))
            return False
        self._start_poller()
        return True

    def _start_poller(self) -> None:
        self.poller = poller_module.Poller(self.bot, self.deliver, on_busy=self._on_busy)
        self.poller.start()

    def _detach(self) -> None:
        self._stop.set()
        with self._lock:
            conversations = list(self.conversations.values())
        for conversation in conversations:
            conversation.close()
        for conversation in conversations:
            conversation.join()
        if self._feed_port:
            daemonlink.unregister(self._feed_port, os.getpid())
        if self.poller is not None:
            self.poller.stop()
            poller_module.release(self.bot.id)
        childjob.kill_all()
        console.restore(self._console_state)

    # -- the feed ----------------------------------------------------------

    def _route_record(self) -> dict[str, Any]:
        return {
            "pid": os.getpid(),
            "tag": self.tag,
            "profile": self.profile.id,
            "alias": self.profile.alias,
            "chat": self.chat,
            "thread": self.thread,
            "cwd": str(self.cwd),
            "slot": self.slot,
        }

    def _feed_loop(self) -> None:
        """Long-poll the daemon for this profile's updates.

        If the daemon goes away, the transport takes the token over and polls
        Telegram itself: the chat keeps working, only the daemon's own
        commands are missing until it is back.
        """
        failures = 0
        registered = False
        while not self._stop.is_set():
            try:
                if not registered:
                    tag = daemonlink.register(self._feed_port, self._route_record())
                    if tag:
                        self.tag = tag
                    registered = True
                for raw in daemonlink.poll(self._feed_port, os.getpid(), wait=FEED_WAIT_SECONDS):
                    with contextlib.suppress(TypeError):
                        self.deliver(telegram.Incoming.from_dict(raw))
                failures = 0
            except daemonlink.LinkError as exc:
                failures += 1
                registered = False
                if failures >= FEED_FAILURES_BEFORE_TAKEOVER and daemonlink.read_daemon() is None:
                    if poller_module.acquire(self.bot.id):
                        log.write(f"transport: daemon gone ({exc}); polling the bot myself")
                        self._feed_port = 0
                        self._start_poller()
                        return
                self._stop.wait(FEED_RETRY_SECONDS)
                record = daemonlink.read_daemon()
                if record is not None:
                    self._feed_port = int(record.get("port") or self._feed_port)

    def deliver(self, incoming: telegram.Incoming) -> None:
        self._last_seen = time.time()
        self._inbox.put(("incoming", incoming))

    def _on_busy(self, reason: str) -> None:
        self._inbox.put(("busy", reason))

    # -- the loop ----------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                kind, item = self._inbox.get(timeout=TICK_SECONDS)
            except queue.Empty:
                self._tick()
                continue
            except KeyboardInterrupt:
                self._console(t("tg.transport_closing"))
                return
            if kind == "incoming":
                self._on_incoming(item)
            elif kind == "busy":
                self._console(t("tg.token_busy", reason=item))
                return

    def _tick(self) -> None:
        if self.poller is not None and time.time() - self._last_lock_refresh > LOCK_REFRESH_SECONDS:
            self._last_lock_refresh = time.time()
            poller_module.refresh(self.bot.id)
        self._retire_idle()

    def _retire_idle(self) -> None:
        """Let go of what nobody has talked to in a long while.

        A conversation is a claude holding a slot and a few hundred megabytes;
        after hours of silence it is only costing memory, and the next message
        starts a fresh one anyway. The window itself closes too, but only when
        the daemon can raise this profile again -- otherwise the chat would go
        deaf with nobody to wake it.

        A session sitting out a quota reset is exempt. The chat is quiet there
        because the agent cannot work, not because the person walked away, and
        closing it would throw away the wait and the line queued for after it.
        """
        if not self.idle_seconds:
            return
        now = time.time()
        with self._lock:
            stale = [
                c
                for c in self.conversations.values()
                if now - c.last_seen > self.idle_seconds and not c.waiting_out_limit
            ]
        for conversation in stale:
            log.write(f"transport: conversation {conversation.tag} idle, closing")
            conversation.close(reason=t("tg.idle_closed", hours=_hours_text(self.idle_seconds)))
        if stale:
            return

        with self._lock:
            empty = not self.conversations
        if not empty or now - self._last_activity() <= self.idle_seconds:
            return
        if self.profile.daemon and daemonlink.read_daemon() is not None:
            log.write("transport: idle with nothing running; the daemon can raise us again")
            self._stop.set()

    def _last_activity(self) -> float:
        with self._lock:
            seen = [c.last_seen for c in self.conversations.values()]
        return max([self.started_at, *seen, self._last_seen])

    # -- routing inside the profile ----------------------------------------

    def serves(self, chat: int, thread: int) -> bool:
        if self.chat and self.chat != chat:
            return False
        if self.chat and self.thread and self.thread != thread:
            return False
        return self.profile.open_to(chat, thread)

    def _key(self, incoming: telegram.Incoming) -> tuple[int, int, int]:
        user = 0 if self.profile.multi else incoming.user_id
        return (incoming.chat_id, incoming.thread_id, user)

    def _on_incoming(self, incoming: telegram.Incoming) -> None:
        if not self.serves(incoming.chat_id, incoming.thread_id):
            return
        if not self.profile.allows(incoming.user_id, profiles_module.global_users(self.config)):
            return

        if incoming.is_callback:
            # By tag, not by who pressed: in a shared chat the button may be
            # answered by someone whose own conversation is a different one.
            conversation = self._by_tag(incoming.callback_data.split(":", 1)[0])
            if conversation is not None:
                conversation.deliver(incoming)
            return

        key = self._key(incoming)

        stripped = self._strip(incoming)
        if stripped is None:
            return

        conversation = self._find(key)
        if conversation is None:
            conversation = self._open(incoming, key)
            if conversation is None:
                return
        conversation.deliver(stripped)

    def _strip(self, incoming: telegram.Incoming) -> telegram.Incoming | None:
        """Take our own name off a line, or refuse it.

        A profile answers only what says its name -- every time, not just
        the first. The daemon applies the same rule; doing it here too is
        what keeps a transport polling on its own behaving alike.
        """
        address = parse_address(incoming.text)
        if address is None or not self.profile.answers_to(address.alias):
            return None
        return incoming.with_text(address.body, urgent=address.urgent)

    def _find(self, key: tuple[int, int, int]) -> Conversation | None:
        with self._lock:
            return self.conversations.get(key)

    def _by_tag(self, tag: str) -> Conversation | None:
        with self._lock:
            return next((c for c in self.conversations.values() if c.tag == tag), None)

    def _open(self, incoming: telegram.Incoming, key: tuple[int, int, int]) -> Conversation | None:
        with self._lock:
            if len(self.conversations) >= self.max_sessions:
                if key in self._refused:
                    return None
                self._refused.add(key)
                self._say(incoming, t("tg.too_many", count=self.max_sessions))
                return None
            self._counter += 1
            tag = f"{self.tag}{CONVERSATION_SEPARATOR}{self._counter}"
            conversation = Conversation(
                self.config,
                slot=self.slot,
                target=ChatTarget(
                    bot=self.bot,
                    chat=incoming.chat_id,
                    thread=incoming.thread_id,
                    user=key[2],
                ),
                profile=self.profile,
                cwd=self.cwd,
                args=self.args,
                tag=tag,
                on_console=lambda text, mark=tag: self._console(f"[{mark}] {text}"),
                on_finished=self._finished,
            )
            self.conversations[key] = conversation
        log.write(
            f"transport: conversation {tag} for {self.profile.label} "
            f"chat={incoming.chat_id} user={key[2] or '*'}"
        )
        conversation.start()
        return conversation

    def _finished(self, conversation: Conversation) -> None:
        with self._lock:
            self.conversations.pop(conversation.key, None)
            self._refused.discard(conversation.key)
            remaining = len(self.conversations)
        log.write(f"transport: conversation {conversation.tag} ended, {remaining} left")
        # A transport started by hand for one chat is done when its only
        # conversation is; one serving a profile keeps listening.
        if not remaining and self.chat:
            self.exit_code = conversation.exit_code
            self._stop.set()

    # -- the console window -------------------------------------------------

    def _console(self, text: str) -> None:
        if sys.stdout is None:
            return  # started without a window: nothing to mirror into
        with contextlib.suppress(OSError, ValueError):
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def _say(self, incoming: telegram.Incoming, text: str) -> None:
        self._console(telegram.strip_html(text))
        with contextlib.suppress(telegram.TelegramError, telegram.Unreachable):
            self.bot.send_message(
                incoming.chat_id, text, thread_id=incoming.thread_id, reply_to=incoming.message_id
            )

    def _chats_text(self) -> str:
        if self.chat:
            return str(self.chat)
        if self.profile.chats:
            return ", ".join(profiles_module.format_chat(ref) for ref in self.profile.chats)
        return "*"


def _hours_text(seconds: float) -> str:
    hours = seconds / 3600.0
    return str(int(hours)) if hours == int(hours) else f"{hours:g}"


def _wait_for_holder(bot_id: int, timeout: float = 10.0) -> dict[str, Any] | None:
    """Give a daemon we just started a moment to take the token."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        holder = poller_module.read_holder(bot_id)
        if holder is not None and int(holder.get("port") or 0):
            return holder
        time.sleep(0.2)
    return poller_module.read_holder(bot_id)


__all__ = ["Transport"]
