from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Any

from core.store import app_dir, read_json, write_json_atomic

LOCK_STALE_SECONDS = 12 * 60 * 60


def sessions_dir() -> Path:
    return app_dir() / "sessions"


def session_file() -> Path:
    return sessions_dir() / f"{os.getpid()}.json"


def register_session(slot: int, **extra: Any) -> None:
    write_json_atomic(
        session_file(),
        {"pid": os.getpid(), "slot": slot, "at": time.time(), **extra},
        harden=False,
    )


def update_session(**fields: Any) -> None:
    """Add to this process's record: the hook-bus port, the transport, the
    session id -- facts that are only known once claude is on its way."""
    raw = read_json(session_file())
    if not isinstance(raw, dict):
        return
    raw.update(fields)
    write_json_atomic(session_file(), raw, harden=False)


def unregister_session() -> None:
    with contextlib.suppress(OSError):
        session_file().unlink()


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


# OpenProcess rights, and the two answers WaitForSingleObject gives for a
# handle to a process.
_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_TIMEOUT = 0x00000102
_ERROR_INVALID_PARAMETER = 87


def _pid_alive_windows(pid: int) -> bool:
    """Ask the kernel directly instead of spawning `tasklist`.

    The old implementation cost a console process per call -- several hundred
    milliseconds each, on every `ccas` start and every usage refresh -- and it
    attached that process to the console claude was drawing in. Nothing here
    touches the console, and the answer arrives in microseconds.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.OpenProcess(
        _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        # "No such process" is the only error that means dead. Access denied is
        # a process we may not touch, which is still very much running.
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER

    try:
        return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            return _pid_alive_windows(pid)
        except (AttributeError, OSError, ValueError):
            # Unreadable is not the same as gone: dropping the session record
            # here would let a rotation run against a live claude.
            return True
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
