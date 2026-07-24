from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

MAX_BYTES = 1 << 20


def _rotate(path: Path) -> None:
    with contextlib.suppress(OSError):
        if path.exists() and path.stat().st_size > MAX_BYTES:
            backup = path.with_suffix(path.suffix + ".1")
            with contextlib.suppress(OSError):
                backup.unlink()
            os.replace(path, backup)


def write(message: str) -> None:
    from core.store import log_file

    try:
        path = log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{stamp} [{os.getpid()}] {message}\n")
    except OSError:
        pass
