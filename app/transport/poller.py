"""The one process per bot token that calls getUpdates.

Telegram enforces a single poller per token; ccas mirrors that locally with a
lock file so that the loser learns it from a file rather than from a 409
after it has already opened a session. The lock names the holder's pid and
the port it answers on, which is how a second session on the same machine
finds the poller to register with instead of competing with it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core import log, telegram
from core.sessions import _pid_alive
from core.store import app_dir, read_json, write_json_atomic

LOCK_STALE_SECONDS = 6 * 60 * 60
# How long an album waits for its next file before it is handed on. Telegram
# sends the parts back to back, but not always in one getUpdates answer.
ALBUM_SETTLE_SECONDS = 1.5
# The poll timeout while an album is still gathering: short enough to notice
# that it has settled.
ALBUM_POLL_SECONDS = 1


def telegram_dir() -> Path:
    return app_dir() / "telegram"


def lock_path(bot_id: int) -> Path:
    return telegram_dir() / f"{bot_id}.lock"


def read_holder(bot_id: int) -> dict[str, Any] | None:
    """Who polls this bot right now, or None when nobody alive does."""
    raw = read_json(lock_path(bot_id))
    if not isinstance(raw, dict):
        return None
    pid = int(raw.get("pid", 0) or 0)
    if pid == os.getpid():
        return raw
    if time.time() - float(raw.get("at", 0) or 0) > LOCK_STALE_SECONDS:
        return None
    if not _pid_alive(pid):
        return None
    return raw


def acquire(bot_id: int, *, port: int = 0) -> bool:
    holder = read_holder(bot_id)
    if holder is not None and int(holder.get("pid", 0) or 0) != os.getpid():
        return False
    write_json_atomic(
        lock_path(bot_id), {"pid": os.getpid(), "at": time.time(), "port": port}, harden=False
    )
    return True


def refresh(bot_id: int, *, port: int = 0) -> None:
    write_json_atomic(
        lock_path(bot_id), {"pid": os.getpid(), "at": time.time(), "port": port}, harden=False
    )


def release(bot_id: int) -> None:
    raw = read_json(lock_path(bot_id))
    if isinstance(raw, dict) and int(raw.get("pid", 0) or 0) != os.getpid():
        return
    with contextlib.suppress(OSError):
        lock_path(bot_id).unlink()


class Albums:
    """Files sent as one album, put back together into one message.

    Telegram delivers an album as a message per file and puts the caption on
    one of them. Routing reads the caption -- `/rik look at these` -- so a
    file without it would be dropped as a line that names nobody. Held here
    until the album stops growing, the parts leave as one update: the
    captioned part's text and id, every part's files in the order sent.
    """

    def __init__(self, settle: float = ALBUM_SETTLE_SECONDS) -> None:
        self.settle = settle
        self._parts: dict[tuple[int, str], list[telegram.Incoming]] = {}
        self._touched: dict[tuple[int, str], float] = {}

    def __bool__(self) -> bool:
        return bool(self._parts)

    def add(self, incoming: telegram.Incoming, *, now: float | None = None) -> bool:
        """Keep `incoming` if it is part of an album; False when it is not."""
        if not incoming.media_group or incoming.is_callback:
            return False
        key = (incoming.chat_id, incoming.media_group)
        self._parts.setdefault(key, []).append(incoming)
        self._touched[key] = time.time() if now is None else now
        return True

    def ready(self, *, now: float | None = None, everything: bool = False) -> list[telegram.Incoming]:
        """The albums that have stopped growing, each as one update."""
        moment = time.time() if now is None else now
        done = [
            key for key, touched in self._touched.items() if everything or moment - touched >= self.settle
        ]
        merged: list[telegram.Incoming] = []
        for key in done:
            parts = self._parts.pop(key)
            self._touched.pop(key, None)
            merged.append(_merge(parts))
        return merged


def _merge(parts: list[telegram.Incoming]) -> telegram.Incoming:
    parts = sorted(parts, key=lambda part: part.message_id)
    head = next((part for part in parts if part.text.strip()), parts[0])
    files = tuple(item for part in parts for item in part.files)
    return dataclasses.replace(head, files=files)


class Poller(threading.Thread):
    """getUpdates in a loop, every update handed to `deliver`.

    Stops on its own when Telegram says the token is taken (`on_busy` gets
    the reason); everything else is retried with backoff inside poll_once.
    """

    def __init__(
        self,
        bot: telegram.Bot,
        deliver: Callable[[telegram.Incoming], None],
        *,
        on_busy: Callable[[str], None] | None = None,
        timeout: int = telegram.POLL_TIMEOUT_SECONDS,
        skip_backlog: bool = True,
    ) -> None:
        super().__init__(daemon=True, name="telegram-poller")
        self.bot = bot
        self.deliver = deliver
        self.on_busy = on_busy
        self.timeout = timeout
        self.skip_backlog = skip_backlog
        self.state = telegram.PollState()
        self._stop = threading.Event()
        self.busy_reason = ""
        self.albums = Albums()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if self.skip_backlog:
            try:
                telegram.skip_pending(self.bot, self.state)
            except telegram.TokenBusy as exc:
                self.busy_reason = exc.description
                if self.on_busy is not None:
                    self.on_busy(exc.description)
                return
        while not self._stop.is_set():
            try:
                incoming = telegram.poll_once(
                    self.bot, self.state, timeout=ALBUM_POLL_SECONDS if self.albums else self.timeout
                )
            except telegram.TokenBusy as exc:
                self.busy_reason = exc.description
                log.write(f"telegram: token {self.bot.id} is polled elsewhere: {exc.description}")
                if self.on_busy is not None:
                    self.on_busy(exc.description)
                return
            ready = [item for item in incoming if not self.albums.add(item)]
            ready.extend(self.albums.ready())
            for item in ready:
                if self._stop.is_set():
                    return
                try:
                    self.deliver(item)
                except Exception as exc:  # one bad update must not stop the poll
                    log.write(f"telegram: delivery failed: {exc!r}")
