from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from core.store import bin_dir, home, shim_name

SECURESTORAGE_VAR = "CLAUDE_SECURESTORAGE_CONFIG_DIR"

_PROBE_PATTERN = SECURESTORAGE_VAR.encode("ascii")
_CHUNK = 1 << 20
_TEXT_SHIM_LIMIT = 2 << 20
_JS_REFERENCE = re.compile(rb"[\w./\\:-]+\.(?:js|mjs|cjs)")

CRED_MODE_ENV = "env"
CRED_MODE_COPY = "copy"


class DetectionError(Exception):
    pass


def _path_entries(exclude: set[Path]) -> list[Path]:
    entries: list[Path] = []
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if not raw.strip():
            continue
        try:
            candidate = Path(raw).expanduser()
        except (OSError, ValueError):
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in exclude:
            continue
        entries.append(candidate)
    return entries


def _npm_global_bin() -> Path | None:
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        completed = subprocess.run(
            [npm, "prefix", "-g"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None

    prefix = completed.stdout.strip()
    if not prefix:
        return None

    base = Path(prefix)
    return base / "bin" if (base / "bin").is_dir() else base


def find_real_claude(*, extra_exclude: list[Path] | None = None) -> Path | None:
    exclude: set[Path] = set()
    for directory in [bin_dir(), *(extra_exclude or [])]:
        try:
            exclude.add(directory.resolve())
        except OSError:
            exclude.add(directory)

    names = ["claude.exe", "claude.cmd", "claude"] if os.name == "nt" else ["claude"]

    for directory in _path_entries(exclude):
        for name in names:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate

    search_dirs = [home() / ".local" / "bin"]
    npm_bin = _npm_global_bin()
    if npm_bin:
        search_dirs.append(npm_bin)

    for directory in search_dirs:
        try:
            if directory.resolve() in exclude:
                continue
        except OSError:
            pass
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate

    return None


def is_our_shim(path: str | os.PathLike[str]) -> bool:
    candidate = Path(path)
    if candidate.name.lower() != shim_name().lower():
        return False
    try:
        return candidate.resolve().parent == bin_dir().resolve()
    except OSError:
        return False


def _contains_pattern(path: Path) -> bool:
    overlap = len(_PROBE_PATTERN) - 1
    try:
        with path.open("rb") as handle:
            tail = b""
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    return False
                if _PROBE_PATTERN in tail + chunk:
                    return True
                tail = chunk[-overlap:] if overlap else b""
    except OSError:
        return False


def _js_targets(path: Path) -> list[Path]:
    try:
        if path.stat().st_size > _TEXT_SHIM_LIMIT:
            return []
        blob = path.read_bytes()
    except OSError:
        return []

    targets: list[Path] = []
    for match in _JS_REFERENCE.findall(blob):
        try:
            raw = match.decode("utf-8", "ignore").replace("\\", "/")
        except UnicodeDecodeError:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (path.parent / candidate).resolve(strict=False)
        if candidate.is_file() and candidate not in targets:
            targets.append(candidate)
    return targets


def supports_securestorage(claude_path: str | os.PathLike[str]) -> bool:
    path = Path(claude_path)
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path

    for candidate in (resolved, path):
        if candidate.is_file() and _contains_pattern(candidate):
            return True

    for target in _js_targets(resolved):
        if _contains_pattern(target):
            return True

    return False


def _identity(path: Path) -> dict[str, Any]:
    try:
        info = path.stat()
    except OSError:
        return {}
    return {"path": str(path), "size": info.st_size, "mtimeNs": info.st_mtime_ns}


def probe_cred_mode(
    claude_path: str | os.PathLike[str], cache: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]]:
    path = Path(claude_path)
    identity = _identity(path)
    if not identity:
        raise DetectionError(str(path))

    if cache and all(cache.get(key) == value for key, value in identity.items()):
        supported = bool(cache.get("supported"))
        return (CRED_MODE_ENV if supported else CRED_MODE_COPY), dict(cache)

    supported = supports_securestorage(path)
    fresh = {**identity, "supported": supported}
    return (CRED_MODE_ENV if supported else CRED_MODE_COPY), fresh


def claude_version(claude_path: str | os.PathLike[str]) -> str:
    try:
        completed = subprocess.run(
            [str(claude_path), "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() or completed.stderr.strip()
