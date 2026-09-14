from __future__ import annotations

import contextlib
import sys
from typing import Any

from core import log

# OSC 0 renames the window. The terminal consumes the sequence before it reaches
# the grid, so it is the one thing we can say while claude owns every cell --
# a line written to stderr instead lands inside ink's frame and the next redraw
# scrambles both.
_SET_TITLE = "\033]0;{text}\007"

PREFIX = "ccas"


def _tty(stream: Any) -> bool:
    try:
        return bool(stream and stream.isatty())
    except (AttributeError, ValueError):
        return False


def title(text: str) -> None:
    stream = sys.stdout
    if not _tty(stream):
        return
    body = f"{PREFIX}: {text}" if text else ""
    with contextlib.suppress(OSError, ValueError):
        stream.write(_SET_TITLE.format(text=body))
        stream.flush()


def clear_title() -> None:
    title("")


def notice(message: str, *, live: bool) -> None:
    """Say something without stepping on whoever owns the screen.

    `live` means claude is drawing right now: the journal gets the full line and
    the window title gets the short version, the terminal itself gets nothing.
    Between runs the terminal is ours again and the message is printed as before.
    """
    log.write(message)
    if live:
        title(message)
        return

    stream = sys.stderr
    if stream is None:
        return
    with contextlib.suppress(OSError, ValueError):
        if _tty(stream):
            stream.write(f"\033[33m{PREFIX}: {message}\033[0m\n")
        else:
            stream.write(f"{PREFIX}: {message}\n")
        stream.flush()
