from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ui.i18n import t

IS_WINDOWS = os.name == "nt"

DIR_MODE = 0o700
FILE_MODE = 0o600


class PermissionWarning(Exception):
    pass


def _current_user() -> str:
    for key in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(key)
        if value:
            domain = os.environ.get("USERDOMAIN")
            if domain and key == "USERNAME":
                return f"{domain}\\{value}"
            return value
    try:
        return os.getlogin()
    except OSError as exc:
        raise PermissionWarning(t("error.no_user")) from exc


def harden_dir(path: str | os.PathLike[str]) -> None:
    target = Path(path)
    if not target.exists():
        return
    if not IS_WINDOWS:
        os.chmod(target, DIR_MODE)
        return

    user = _current_user()
    try:
        completed = subprocess.run(
            [
                "icacls",
                str(target),
                "/inheritance:r",
                "/grant:r",
                f"{user}:(OI)(CI)F",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PermissionWarning(t("error.icacls_failed", path=target, error=exc)) from exc

    if completed.returncode != 0:
        raise PermissionWarning(
            t(
                "error.icacls_returned",
                code=completed.returncode,
                path=target,
                output=completed.stderr.strip() or completed.stdout.strip(),
            )
        )


def harden_file(path: str | os.PathLike[str]) -> None:
    target = Path(path)
    if not target.exists() or IS_WINDOWS:
        return
    os.chmod(target, FILE_MODE)


def is_world_readable(path: str | os.PathLike[str]) -> bool:
    if IS_WINDOWS:
        return False
    target = Path(path)
    if not target.exists():
        return False
    return bool(target.stat().st_mode & 0o077)
