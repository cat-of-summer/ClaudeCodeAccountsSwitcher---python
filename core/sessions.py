from __future__ import annotations

import contextlib
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from core.store import app_dir, read_json, write_json_atomic

LOCK_STALE_SECONDS = 12 * 60 * 60


def sessions_dir() -> Path:
    return app_dir() / "sessions"


def register_session(slot: int) -> None:
    write_json_atomic(
        sessions_dir() / f"{os.getpid()}.json",
        {"pid": os.getpid(), "slot": slot, "at": time.time()},
        harden=False,
    )


def unregister_session() -> None:
    with contextlib.suppress(OSError):
        (sessions_dir() / f"{os.getpid()}.json").unlink()


def other_live_sessions() -> list[dict[str, Any]]:
    """Wrapper processes other than this one that are still running.

    Tells apart the two reasons the shared config can name an unfamiliar
    account: a deliberate re-login on this slot (nobody else is running) versus
    a neighbouring terminal having just written its own (someone is).
    """
    directory = sessions_dir()
    if not directory.is_dir():
        return []

    mine = os.getpid()
    live: list[dict[str, Any]] = []
    for entry in directory.glob("*.json"):
        raw = read_json(entry)
        if not isinstance(raw, dict):
            continue
        pid = int(raw.get("pid", 0) or 0)
        if pid == mine:
            continue
        stale = time.time() - float(raw.get("at", 0) or 0) > LOCK_STALE_SECONDS
        if stale or not _pid_alive(pid):
            with contextlib.suppress(OSError):
                entry.unlink()
            continue
        live.append(raw)
    return live


def busy_slots() -> set[int]:
    """Slots a live wrapper is currently running claude on.

    Anything that rewrites a slot's credentials behind claude's back has to
    consult this first: the running process holds the refresh token in memory,
    and a rotation it never saw would log it out mid-session.
    """
    numbers: set[int] = set()
    for entry in other_live_sessions():
        with contextlib.suppress(TypeError, ValueError):
            numbers.add(int(entry.get("slot", 0) or 0))
    numbers.discard(0)
    return numbers


def _lock_path() -> Path:
    return app_dir() / ".lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock() -> dict[str, Any] | None:
    raw = read_json(_lock_path())
    if not isinstance(raw, dict):
        return None
    if time.time() - float(raw.get("at", 0)) > LOCK_STALE_SECONDS:
        return None
    if not _pid_alive(int(raw.get("pid", 0) or 0)):
        return None
    return raw


def acquire_lock(slot: int) -> None:
    write_json_atomic(_lock_path(), {"pid": os.getpid(), "slot": slot, "at": time.time()})


def release_lock() -> None:
    raw = read_json(_lock_path())
    if isinstance(raw, dict) and int(raw.get("pid", 0) or 0) != os.getpid():
        return
    with contextlib.suppress(OSError):
        _lock_path().unlink()
