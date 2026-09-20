"""Children that die when we do.

A transport window holds several claude processes, and the window has an X
button. Closing it sends CTRL_CLOSE_EVENT and kills the transport a few
seconds later -- but the claudes it started are processes of their own, and
nothing makes them go. They would sit in memory, holding a slot and a
credentials directory, until someone noticed them in the task manager.

On Windows a Job Object with KILL_ON_JOB_CLOSE settles it in the kernel:
whatever happens to this process, even a hard kill, the children go with it.
On POSIX the process group does the same job, so all this module has to do
there is remember who to signal.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
from typing import Any

from core import log

IS_WINDOWS = os.name == "nt"

# JOBOBJECT_EXTENDED_LIMIT_INFORMATION, and the one flag that matters.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

_lock = threading.Lock()
_handle: int | None = None
_tracked: list[subprocess.Popen[bytes]] = []


def _create_job() -> int | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        wintypes.HANDLE(job),
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        kernel32.CloseHandle(wintypes.HANDLE(job))
        return None
    return int(job)


def _job() -> int | None:
    global _handle
    if not IS_WINDOWS:
        return None
    if _handle is None:
        with contextlib.suppress(Exception):
            _handle = _create_job() or 0
        if not _handle:
            log.write("childjob: no job object; children may outlive the window")
            _handle = 0
    return _handle or None


def adopt(process: subprocess.Popen[bytes]) -> None:
    """Make this child part of our fate."""
    with _lock:
        _tracked.append(process)
    job = _job()
    if job is None:
        return
    with contextlib.suppress(Exception):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, process.pid)
        if not handle:
            return
        try:
            kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(handle))
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(handle))


def forget(process: subprocess.Popen[bytes]) -> None:
    with _lock:
        with contextlib.suppress(ValueError):
            _tracked.remove(process)


def kill_all() -> None:
    """Stop every child we started; the last resort before going away."""
    with _lock:
        children = list(_tracked)
        _tracked.clear()
    for process in children:
        if process.poll() is not None:
            continue
        with contextlib.suppress(Exception):
            from app import wrapper

            wrapper.terminate(process)


def on_close(callback: Any) -> None:
    """Run `callback` when the window is closed or the session ends.

    Windows delivers CTRL_CLOSE_EVENT and gives a few seconds before killing
    the process; Python raises nothing for it, so the handler has to be
    installed by hand. POSIX gets the same treatment through SIGHUP/SIGTERM.
    """
    if IS_WINDOWS:
        with contextlib.suppress(Exception):
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

            def _handler(event: int) -> bool:
                # CTRL_C(0) and CTRL_BREAK(1) are the user interrupting a
                # turn; close, logoff and shutdown are the end of us.
                if event in (2, 5, 6):  # CLOSE, LOGOFF, SHUTDOWN
                    with contextlib.suppress(Exception):
                        callback()
                    kill_all()
                return False

            global _console_handler
            _console_handler = handler_type(_handler)
            kernel32.SetConsoleCtrlHandler(_console_handler, True)
        return

    for name in ("SIGHUP", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            signal.signal(number, lambda *_: (callback(), kill_all()))


_console_handler: Any = None


__all__ = ["adopt", "forget", "kill_all", "on_close"]
