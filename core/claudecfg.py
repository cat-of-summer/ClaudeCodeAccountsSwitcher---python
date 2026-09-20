from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Any

from core.store import (
    CREDENTIALS_FILENAME,
    backup_file,
    home,
    read_json,
    write_json_atomic,
)

IDENTITY_KEYS = ("oauthAccount", "userID")


def config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return home() / ".claude"


def shared_credentials_path() -> Path:
    return config_dir() / CREDENTIALS_FILENAME


def active_config_target() -> Path:
    nested = config_dir() / ".config.json"
    if nested.exists():
        return nested
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base) if base else home()
    return root / ".claude.json"


def candidate_config_targets() -> list[Path]:
    seen: list[Path] = []
    for candidate in (
        active_config_target(),
        config_dir() / ".config.json",
        home() / ".claude.json",
        config_dir() / ".claude.json",
    ):
        if candidate not in seen:
            seen.append(candidate)
    return seen


def existing_config_targets() -> list[Path]:
    return [candidate for candidate in candidate_config_targets() if candidate.exists()]


def freshest_config_target() -> Path | None:
    targets = existing_config_targets()
    if not targets:
        return None
    return max(targets, key=lambda path: path.stat().st_mtime_ns)


def read_identity() -> dict[str, Any] | None:
    target = freshest_config_target()
    if target is None:
        return None
    raw = read_json(target)
    if not isinstance(raw, dict):
        return None
    if not any(key in raw for key in IDENTITY_KEYS):
        return None
    return {"oauthAccount": raw.get("oauthAccount"), "userID": raw.get("userID")}


def _identity_matches(raw: dict[str, Any], identity: dict[str, Any]) -> bool:
    return all(raw.get(key) == identity.get(key) for key in IDENTITY_KEYS)


def patch_identity(identity: dict[str, Any] | None) -> list[Path]:
    if not identity:
        return []

    written: list[Path] = []
    for target in existing_config_targets():
        raw = read_json(target)
        if not isinstance(raw, dict):
            continue
        if _identity_matches(raw, identity):
            continue
        backup_file(target)
        for key in IDENTITY_KEYS:
            raw[key] = identity.get(key)
        write_json_atomic(target, raw, harden=False)
        written.append(target)
    return written


def clear_identity() -> list[Path]:
    written: list[Path] = []
    for target in existing_config_targets():
        raw = read_json(target)
        if not isinstance(raw, dict) or raw.get("oauthAccount") is None:
            continue
        backup_file(target)
        raw["oauthAccount"] = None
        write_json_atomic(target, raw, harden=False)
        written.append(target)
    return written


def read_credentials(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    raw = read_json(path)
    if not isinstance(raw, dict):
        return None
    oauth = raw.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else None


def access_token(path: str | os.PathLike[str]) -> str | None:
    oauth = read_credentials(path)
    if not oauth:
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def token_state(path: str | os.PathLike[str], *, now_ms: float | None = None) -> str:
    oauth = read_credentials(path)
    if not oauth:
        return "missing"

    current = now_ms if now_ms is not None else time.time() * 1000

    refresh_expiry = oauth.get("refreshTokenExpiresAt")
    if isinstance(refresh_expiry, (int, float)) and current >= refresh_expiry:
        return "stale"

    expiry = oauth.get("expiresAt")
    if isinstance(expiry, (int, float)) and current >= expiry:
        return "expired"

    return "ok"


def read_usage_cache() -> dict[str, Any] | None:
    target = freshest_config_target()
    if target is None:
        return None
    raw = read_json(target)
    if not isinstance(raw, dict):
        return None
    cached = raw.get("cachedUsageUtilization")
    return cached if isinstance(cached, dict) else None


def known_projects() -> list[str]:
    """Directories claude has been run in, newest first.

    `~/.claude.json` keeps them under `projects`, keyed by the real path (not
    the slug the transcript directory uses), appended as they are first seen.
    Paths that no longer exist are dropped; the same directory spelt two
    ways -- forward and backward slashes -- counts once.
    """
    target = freshest_config_target()
    if target is None:
        return []
    raw = read_json(target)
    projects = raw.get("projects") if isinstance(raw, dict) else None
    if not isinstance(projects, dict):
        return []

    seen: set[str] = set()
    found: list[str] = []
    for key in reversed(list(projects)):
        if not isinstance(key, str) or not key:
            continue
        path = Path(key)
        try:
            if not path.is_dir():
                continue
            marker = os.path.normcase(str(path.resolve()))
        except OSError:
            continue
        if marker in seen:
            continue
        seen.add(marker)
        found.append(str(path))
    return found


def settings_files(cwd: Path | None = None) -> list[Path]:
    """The settings claude reads, weakest first.

    Managed policy settings are deliberately left out: ccas reports what the
    user can change, and a policy file would only make the answer wrong in
    the other direction if it were missing.
    """
    root = Path(cwd) if cwd is not None else Path.cwd()
    return [
        config_dir() / "settings.json",
        root / ".claude" / "settings.json",
        root / ".claude" / "settings.local.json",
    ]


def default_permission_mode(cwd: Path | None = None) -> str:
    """The mode claude would start in, without starting it.

    `permissions.defaultMode` in the settings chain, later files winning, the
    way claude resolves it. Empty when nothing says -- which means claude's
    own default, the one that asks.
    """
    mode = ""
    for path in settings_files(cwd):
        raw = None
        with contextlib.suppress(OSError, ValueError):
            raw = read_json(path)
        if not isinstance(raw, dict):
            continue
        permissions = raw.get("permissions")
        if not isinstance(permissions, dict):
            continue
        found = permissions.get("defaultMode")
        if isinstance(found, str) and found.strip():
            mode = found.strip()
    return mode
