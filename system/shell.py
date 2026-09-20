"""Run a `!cmd` line from the chat in a shell a person would expect.

`subprocess.run(shell=True)` on Windows means cmd.exe: `pwd` is "not
recognized", `ls -la` fails, and what does come back is in the OEM code page
rather than UTF-8, so a Russian error message arrives as a row of boxes.
The shell there is PowerShell (`pwsh` when installed, `powershell.exe`
otherwise): `pwd`, `ls`, `cat` work as aliases, and the console encoding is
switched to UTF-8 before the line runs so the output can be read as such.
On POSIX the line goes to bash, like claude's own bash mode.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# Cmdlet output is written in the console's encoding; without this line a
# Windows PowerShell 5.1 answers in the OEM code page.
POWERSHELL_PREAMBLE = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "


@dataclass(frozen=True)
class Completed:
    stdout: str
    stderr: str
    returncode: int


def shell_argv(command: str) -> list[str] | None:
    """The argv that runs `command`; None when only the OS default shell is left."""
    if IS_WINDOWS:
        exe = shutil.which("pwsh") or shutil.which("powershell")
        if not exe:
            return None
        return [exe, "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_PREAMBLE + command]
    bash = shutil.which("bash")
    return [bash, "-c", command] if bash else None


def oem_encoding() -> str:
    """The code page cmd.exe writes in; UTF-8 where there is no such thing."""
    if not IS_WINDOWS:
        return "utf-8"
    try:
        import ctypes

        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except (AttributeError, OSError, ValueError):
        return "utf-8"


def decode(data: bytes, *, fallback: str) -> str:
    """UTF-8 when the bytes are valid UTF-8, the fallback code page otherwise.

    Native tools called from PowerShell still write whatever they like, and
    cmd.exe's own messages come in the OEM code page.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode(fallback)
        except (UnicodeDecodeError, LookupError):
            return data.decode("utf-8", "replace")


def run(command: str, *, cwd: Path, timeout: float) -> Completed:
    """Run one line; `subprocess.TimeoutExpired` and `OSError` are the caller's."""
    argv = shell_argv(command)
    completed = subprocess.run(
        argv if argv else command,
        shell=argv is None,
        cwd=str(cwd),
        capture_output=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    fallback = oem_encoding()
    return Completed(
        decode(completed.stdout, fallback=fallback),
        decode(completed.stderr, fallback=fallback),
        completed.returncode,
    )
