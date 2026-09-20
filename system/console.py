"""Put the terminal back the way claude found it.

claude (ink) switches the console to raw input, enters the alternate screen,
turns on mouse reporting and hides the cursor. A claude that exits on its own
undoes all of that; a claude we killed with `taskkill /T` never runs its exit
handlers, and the console stays broken for whoever uses it next -- Ctrl-C
arrives as a keystroke instead of a signal, `input()` returns without echo,
and the next thing we print lands inside a screen nobody is drawing any more.

The pure part (mode bits, escape sequences) is what the tests cover on any
platform; the calls into the OS are guarded so that a redirected stream or an
unsupported platform degrades to doing nothing.
"""

from __future__ import annotations

import contextlib
import os
import sys
from dataclasses import dataclass
from typing import Any

IS_WINDOWS = os.name == "nt"

# Input mode bits, from wincon.h.
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_INSERT_MODE = 0x0020
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

# Output mode bits.
ENABLE_PROCESSED_OUTPUT = 0x0001
ENABLE_WRAP_AT_EOL_OUTPUT = 0x0002
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
INVALID_HANDLE_VALUE = -1

# What a killed ink never got to send. Each one is a no-op when the terminal is
# not in that state, so the whole set is safe to write blind.
RESET_SEQUENCES: tuple[str, ...] = (
    "\033[?1049l",  # leave the alternate screen
    "\033[?25h",  # show the cursor
    "\033[?1000l",  # mouse: click reporting
    "\033[?1002l",  # mouse: drag reporting
    "\033[?1003l",  # mouse: any-motion reporting
    "\033[?1006l",  # mouse: SGR encoding
    "\033[?1015l",  # mouse: urxvt encoding
    "\033[?2004l",  # bracketed paste
    "\033[?7h",  # line wrap
    "\033[0m",  # colours and attributes
)


def sane_input_mode() -> int:
    """A console you can type into: line editing, echo, Ctrl-C as a signal.

    Insert and quick-edit only count when ENABLE_EXTENDED_FLAGS is set too;
    without it the console ignores both and reports them as off.
    """
    return (
        ENABLE_PROCESSED_INPUT
        | ENABLE_LINE_INPUT
        | ENABLE_ECHO_INPUT
        | ENABLE_EXTENDED_FLAGS
        | ENABLE_INSERT_MODE
        | ENABLE_QUICK_EDIT_MODE
    )


def sane_output_mode() -> int:
    return (
        ENABLE_PROCESSED_OUTPUT
        | ENABLE_WRAP_AT_EOL_OUTPUT
        | ENABLE_VIRTUAL_TERMINAL_PROCESSING
    )


def is_broken(input_mode: int) -> bool:
    """Raw mode as ink leaves it: no line input, no Ctrl-C processing."""
    required = ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT
    return (input_mode & required) != required


@dataclass(frozen=True)
class State:
    input_mode: int | None
    output_mode: int | None
    termios: Any = None


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------


def _kernel32() -> Any:
    """kernel32 with handle-typed signatures: a HANDLE is 64 bits wide and the
    ctypes default of c_int would truncate it."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.SetConsoleMode.restype = wintypes.BOOL
    kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    return kernel32


def _console_handle(std: int, device: str) -> int:
    """The std handle when it is a console, the device itself otherwise.

    Under a redirection the std handle is a pipe and GetConsoleMode fails on
    it; the console the user is looking at is still reachable by name.
    """
    import ctypes

    kernel32 = _kernel32()
    handle = kernel32.GetStdHandle(std & 0xFFFFFFFF)
    mode = ctypes.c_ulong()
    if handle and handle != _invalid_handle() and kernel32.GetConsoleMode(
        handle, ctypes.byref(mode)
    ):
        return int(handle)

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3
    handle = kernel32.CreateFileW(
        device,
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if not handle or handle == _invalid_handle():
        return 0
    return int(handle)


def _invalid_handle() -> int:
    import ctypes

    return ctypes.c_void_p(INVALID_HANDLE_VALUE).value or 0


def _get_mode(handle: int) -> int | None:
    import ctypes

    if not handle:
        return None
    mode = ctypes.c_ulong()
    if not _kernel32().GetConsoleMode(handle, ctypes.byref(mode)):
        return None
    return int(mode.value)


def _set_mode(handle: int, mode: int | None) -> None:
    if handle and mode is not None:
        _kernel32().SetConsoleMode(handle, mode)


def _snapshot_windows() -> State | None:
    stdin = _console_handle(STD_INPUT_HANDLE, "CONIN$")
    stdout = _console_handle(STD_OUTPUT_HANDLE, "CONOUT$")
    input_mode = _get_mode(stdin)
    output_mode = _get_mode(stdout)
    if input_mode is None and output_mode is None:
        return None
    return State(input_mode, output_mode)


def _restore_windows(state: State | None) -> None:
    stdin = _console_handle(STD_INPUT_HANDLE, "CONIN$")
    stdout = _console_handle(STD_OUTPUT_HANDLE, "CONOUT$")
    if state is None:
        _set_mode(stdin, sane_input_mode())
        # Keep whatever else the host terminal had on the output side; only
        # the three bits a readable console needs are forced.
        current = _get_mode(stdout)
        _set_mode(stdout, (current or 0) | sane_output_mode())
        return
    _set_mode(stdin, state.input_mode)
    _set_mode(stdout, state.output_mode)


# --------------------------------------------------------------------------
# POSIX
# --------------------------------------------------------------------------


def _snapshot_posix() -> State | None:
    import termios

    if not _tty(sys.stdin):
        return None
    return State(None, None, termios=termios.tcgetattr(sys.stdin.fileno()))


def _restore_posix(state: State | None) -> None:
    import termios

    if not _tty(sys.stdin):
        return
    descriptor = sys.stdin.fileno()
    if state is None or state.termios is None:
        attrs = termios.tcgetattr(descriptor)
        attrs[3] |= termios.ICANON | termios.ECHO | termios.ISIG
        termios.tcsetattr(descriptor, termios.TCSADRAIN, attrs)
        return
    termios.tcsetattr(descriptor, termios.TCSADRAIN, state.termios)


def _posix_broken() -> bool:
    import termios

    if not _tty(sys.stdin):
        return False
    attrs = termios.tcgetattr(sys.stdin.fileno())
    wanted = termios.ICANON | termios.ISIG
    return (attrs[3] & wanted) != wanted


# --------------------------------------------------------------------------
# public surface
# --------------------------------------------------------------------------


def _tty(stream: Any) -> bool:
    try:
        return bool(stream and stream.isatty())
    except (AttributeError, ValueError):
        return False


_GUARD = (OSError, AttributeError, ValueError, ImportError)


def snapshot() -> State | None:
    with contextlib.suppress(*_GUARD):
        return _snapshot_windows() if IS_WINDOWS else _snapshot_posix()
    return None


def restore(state: State | None) -> None:
    with contextlib.suppress(*_GUARD):
        if IS_WINDOWS:
            _restore_windows(state)
        else:
            _restore_posix(state)


def sanitize() -> None:
    """Undo the screen-level state a killed claude left: alt screen, mouse, cursor."""
    stream = sys.stdout
    if not _tty(stream):
        return
    with contextlib.suppress(*_GUARD):
        stream.write("".join(RESET_SEQUENCES))
        stream.flush()


def repair() -> bool:
    """Fix a console a previous run left in raw mode. True when something was done.

    Cheap on a healthy console: one GetConsoleMode and nothing else, so it is
    safe to run at every entry point.
    """
    with contextlib.suppress(*_GUARD):
        if IS_WINDOWS:
            state = _snapshot_windows()
            if state is None or state.input_mode is None:
                return False
            if not is_broken(state.input_mode):
                return False
        elif not _posix_broken():
            return False
        restore(None)
        sanitize()
        return True
    return False


def poll_key(timeout: float) -> str:
    """One keypress if it arrives within `timeout` seconds, else "".

    Reads the console directly, so it works whether or not line input is on:
    the wait screen must answer to a key in the very state we are repairing.
    Windows returns arrow keys as two reads; they are folded into "".
    """
    if IS_WINDOWS:
        return _poll_key_windows(timeout)
    return _poll_key_posix(timeout)


def _poll_key_windows(timeout: float) -> str:
    import msvcrt
    import time

    deadline = time.monotonic() + timeout
    while True:
        if msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                msvcrt.getwch()
                return ""
            return char
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        time.sleep(min(0.05, remaining))


def _poll_key_posix(timeout: float) -> str:
    import select
    import termios
    import tty

    if not _tty(sys.stdin):
        import time

        time.sleep(timeout)
        return ""

    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor)
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return ""
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)
