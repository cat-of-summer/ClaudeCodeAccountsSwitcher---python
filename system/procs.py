"""Who is running our own binaries, and how to stop them.

An upgrade replaces `claude` and `ccas` in the shim directory, and Windows
refuses to overwrite a file some process still has open. Until now `install`
worked around that silently: it left the new build as `.new` beside the old
one and the next `ccas` swapped it in -- so the version just installed was
not the version that ran, and the feature the upgrade was for stayed missing.

Knowing *which* processes hold the files turns that into something a person
can act on: a window with a Telegram session in it, a daemon, a claude
started from another terminal.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

IS_WINDOWS = os.name == "nt"

MAX_PATH_CHARS = 32768
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INVALID_HANDLE_VALUE = -1


@dataclass(frozen=True)
class Process:
    pid: int
    path: Path

    @property
    def name(self) -> str:
        return self.path.name


def _windows_processes() -> list[tuple[int, Path]]:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    # Handles are pointer-sized; left as the default `int` return they come
    # back truncated on 64-bit and every call after them fails.
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(INVALID_HANDLE_VALUE).value:
        return []
    found: list[tuple[int, Path]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            pid = int(entry.th32ProcessID)
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                try:
                    size = wintypes.DWORD(MAX_PATH_CHARS)
                    buffer = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                        found.append((pid, Path(buffer.value)))
                finally:
                    kernel32.CloseHandle(handle)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def _posix_processes() -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        with contextlib.suppress(OSError):
            found.append((int(entry.name), Path(os.readlink(entry / "exe"))))
    return found


def running() -> list[tuple[int, Path]]:
    """Every process we can see, as (pid, executable)."""
    try:
        return _windows_processes() if IS_WINDOWS else _posix_processes()
    except (OSError, AttributeError, ValueError):
        return []


def _same(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def using(directory: Path, *, skip: Iterable[int] = ()) -> list[Process]:
    """Processes running a binary out of `directory`, ours excluded.

    The current process is always skipped: it cannot free its own file, and
    the `.new` hand-off exists for exactly that case.
    """
    skipped = {os.getpid(), *skip}
    directory = Path(directory)
    found: list[Process] = []
    for pid, path in running():
        if pid in skipped:
            continue
        if _same(path.parent, directory):
            found.append(Process(pid, path))
    return sorted(found, key=lambda process: process.pid)


def kill(pid: int, *, timeout: float = 15.0) -> bool:
    """Stop a process and its children; True when it is gone afterwards."""
    if IS_WINDOWS:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    else:
        with contextlib.suppress(OSError):
            os.kill(pid, 9)
    return not alive(pid)


def alive(pid: int) -> bool:
    return any(known == pid for known, _ in running())
