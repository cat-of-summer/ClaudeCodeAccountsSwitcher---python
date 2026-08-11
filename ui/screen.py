from __future__ import annotations

import os
import sys

IS_WINDOWS = os.name == "nt"

ESC = "\033["
RESET = f"{ESC}0m"
DIM = f"{ESC}2m"
BOLD = f"{ESC}1m"
GREEN = f"{ESC}32m"
YELLOW = f"{ESC}33m"
CYAN = f"{ESC}36m"

ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
STD_OUTPUT_HANDLE = -11

ESCAPE_TIMEOUT = 0.05


def enable_ansi() -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(
                handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            )
    except (AttributeError, OSError):
        pass


def _read_key_windows() -> str:
    import msvcrt

    char = msvcrt.getwch()
    if char in ("\x00", "\xe0"):
        second = msvcrt.getwch()
        return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(second, "")
    if char == "\r":
        return "enter"
    if char == "\x1b":
        return "esc"
    if char == " ":
        return "space"
    if char == "\x03":
        raise KeyboardInterrupt
    return char.lower()


def _read_key_posix() -> str:
    import select
    import termios
    import tty

    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        char = sys.stdin.read(1)
        if char == "\x1b":
            # A lone Escape arrives as "\x1b" and nothing else, an arrow as
            # "\x1b[A". Only a short wait tells them apart.
            ready, _, _ = select.select([sys.stdin], [], [], ESCAPE_TIMEOUT)
            if not ready:
                return "esc"
            if sys.stdin.read(1) != "[":
                return "esc"
            return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(
                sys.stdin.read(1), ""
            )
        if char in ("\r", "\n"):
            return "enter"
        if char == " ":
            return "space"
        if char == "\x03":
            raise KeyboardInterrupt
        return char.lower()
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def read_key() -> str:
    return _read_key_windows() if IS_WINDOWS else _read_key_posix()


def draw(lines: list[str], previous_height: int) -> int:
    if previous_height:
        sys.stdout.write(f"{ESC}{previous_height}A")
    for line in lines:
        sys.stdout.write(f"\r{ESC}K{line}\n")
    sys.stdout.flush()
    return len(lines)


class Surface:
    """A block of lines redrawn in place, plus the cursor bookkeeping it needs.

    Keeping the height here rather than in a local of every screen loop is what
    lets one screen hand control to another: the caller that opens a nested
    screen calls `reset()` afterwards, and the next paint writes a fresh block
    instead of scrolling over whatever the nested screen left behind.
    """

    def __init__(self) -> None:
        self._height = 0

    def paint(self, lines: list[str]) -> None:
        self._height = draw(lines, self._height)

    def reset(self) -> None:
        self._height = 0
