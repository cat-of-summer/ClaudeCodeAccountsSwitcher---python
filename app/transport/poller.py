"""The one process per bot token that calls getUpdates.

Telegram enforces a single poller per token; ccas mirrors that locally with a
lock file so that the loser learns it from a file rather than from a 409
after it has already opened a session. The lock names the holder's pid and
the port it answers on, which is how a second session on the same machine
finds the poller to register with instead of competing with it.
"""

from __future__ import annotations

import contextlib
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
    ) -> None:
        super().__init__(daemon=True, name="telegram-poller")
        self.bot = bot
        self.deliver = deliver
        self.on_busy = on_busy
        self.timeout = timeout
        self.state = telegram.PollState()
        self._stop = threading.Event()
        self.busy_reason = ""

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                incoming = telegram.poll_once(self.bot, self.state, timeout=self.timeout)
            except telegram.TokenBusy as exc:
                self.busy_reason = exc.description
                log.write(f"telegram: token {self.bot.id} is polled elsewhere: {exc.description}")
                if self.on_busy is not None:
                    self.on_busy(exc.description)
                return
            for item in incoming:
                if self._stop.is_set():
                    return
                try:
                    self.deliver(item)
                except Exception as exc:  # one bad update must not stop the poll
                    log.write(f"telegram: delivery failed: {exc!r}")
