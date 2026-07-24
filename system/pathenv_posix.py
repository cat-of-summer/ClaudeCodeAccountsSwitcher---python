from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BEGIN_MARKER = "# >>> ccas >>>"
END_MARKER = "# <<< ccas <<<"


@dataclass(frozen=True)
class ShellTarget:
    name: str
    rc_path: Path
    posix_syntax: bool


def shell_targets() -> list[ShellTarget]:
    home = Path.home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return [
        ShellTarget("bash", home / ".bashrc", True),
        ShellTarget("zsh", home / ".zshrc", True),
        ShellTarget("fish", config_home / "fish" / "config.fish", False),
    ]


def fallback_target() -> ShellTarget:
    return ShellTarget("profile", Path.home() / ".profile", True)


def scan_targets() -> list[ShellTarget]:
    return [*shell_targets(), fallback_target()]


def _block(directory: Path, posix_syntax: bool) -> str:
    if posix_syntax:
        body = f'export PATH="{directory}:$PATH"'
    else:
        body = f'fish_add_path -m "{directory}"'
    return f"{BEGIN_MARKER}\n{body}\n{END_MARKER}\n"


def _strip_block(text: str) -> str:
    start = text.find(BEGIN_MARKER)
    if start == -1:
        return text
    end = text.find(END_MARKER, start)
    if end == -1:
        return text[:start].rstrip("\n") + "\n"
    end += len(END_MARKER)
    return text[:start] + text[end:].lstrip("\n")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _apply(target: ShellTarget, directory: Path) -> bool:
    existing = (
        target.rc_path.read_text(encoding="utf-8", errors="replace")
        if target.rc_path.exists()
        else ""
    )
    stripped = _strip_block(existing)
    if stripped and not stripped.endswith("\n"):
        stripped += "\n"
    updated = stripped + _block(directory, target.posix_syntax)
    if updated == existing:
        return False
    _write(target.rc_path, updated)
    return True


def ensure_first(directory: Path) -> list[str]:
    current_shell = Path(os.environ.get("SHELL", "")).name
    changed: list[str] = []
    considered = False

    for target in shell_targets():
        if not target.rc_path.exists() and target.name != current_shell:
            continue
        considered = True
        if _apply(target, directory):
            changed.append(target.name)

    if not considered:
        fallback = fallback_target()
        if _apply(fallback, directory):
            changed.append(fallback.name)

    return changed


def remove(directory: Path) -> list[str]:
    del directory
    changed: list[str] = []
    for target in scan_targets():
        if not target.rc_path.exists():
            continue
        existing = target.rc_path.read_text(encoding="utf-8", errors="replace")
        stripped = _strip_block(existing)
        if stripped != existing:
            _write(target.rc_path, stripped)
            changed.append(target.name)
    return changed


def is_on_path(directory: Path) -> bool:
    del directory
    for target in scan_targets():
        if not target.rc_path.exists():
            continue
        if BEGIN_MARKER in target.rc_path.read_text(encoding="utf-8", errors="replace"):
            return True
    return False
