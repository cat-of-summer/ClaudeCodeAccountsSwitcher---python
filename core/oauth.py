from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from core import claudecfg, log, sessions
from core.detect import CRED_MODE_COPY
from core.store import (
    Accounts,
    Config,
    backup_file,
    creds_dir,
    creds_file,
    file_lock,
    read_json,
    update_accounts,
    write_json_atomic,
)
from system import secure

# Lifted from the installed claude binary rather than guessed: it posts JSON
# (not form-encoded) to this URL with exactly these fields.
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
DEFAULT_SCOPES = (
    "user:file_upload",
    "user:inference",
    "user:mcp_servers",
    "user:profile",
    "user:sessions:claude_code",
)

REFRESH_TIMEOUT = 30.0
REFRESH_SKEW_MS = 120_000.0
REFRESH_COOLDOWN = 300.0
LOCK_TIMEOUT = 2.0
REFRESH_LOCK_NAME = ".refresh.lock"

REFRESHED = "refreshed"
SKIPPED = "skipped"
BUSY = "busy"
DEAD = "dead"
FAILED = "failed"


class OAuthError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind


def refresh_lock_path(number: int) -> Path:
    return creds_dir(number) / REFRESH_LOCK_NAME


def _post_refresh(
    refresh_token: str, scopes: list[str], *, timeout: float
) -> dict[str, Any]:
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "scope": " ".join(scopes or DEFAULT_SCOPES),
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "ccas"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # A revoked or superseded refresh token answers 400/401 with
        # "invalid_grant". That is terminal -- retrying it only wastes the next
        # five minutes -- while a 5xx is worth trying again later.
        detail = _http_error_code(exc)
        kind = "dead" if detail == "invalid_grant" or exc.code in (400, 401) else "transient"
        raise OAuthError(kind, f"HTTP {exc.code} {detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise OAuthError("transient", repr(exc)) from exc

    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise OAuthError("transient", "malformed token response")
    return payload


def _http_error_code(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read().decode("utf-8"))
    except (ValueError, OSError, AttributeError):
        return ""
    return body.get("error", "") if isinstance(body, dict) else ""


def needs_refresh(oauth: dict[str, Any], *, now_ms: float | None = None) -> bool:
    current = now_ms if now_ms is not None else time.time() * 1000
    if not oauth.get("refreshToken"):
        return False

    refresh_expiry = oauth.get("refreshTokenExpiresAt")
    if isinstance(refresh_expiry, (int, float)) and current >= refresh_expiry:
        return False

    expiry = oauth.get("expiresAt")
    if not isinstance(expiry, (int, float)):
        return True
    return current >= expiry - REFRESH_SKEW_MS


def merge_tokens(
    existing: dict[str, Any], payload: dict[str, Any], *, now_ms: float | None = None
) -> dict[str, Any]:
    """Fold a token response into the credentials block already on disk.

    Everything unrecognised is carried over untouched: the block also holds
    `subscriptionType` and whatever else claude decided to keep there, and
    dropping a key we did not think about would be a silent downgrade.
    """
    current = now_ms if now_ms is not None else time.time() * 1000
    merged = dict(existing)
    merged["accessToken"] = payload["access_token"]

    # Rotation is optional. When the server keeps the old refresh token it
    # simply omits it, and overwriting with None would end the slot.
    rotated = payload.get("refresh_token")
    if isinstance(rotated, str) and rotated:
        merged["refreshToken"] = rotated

    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)):
        merged["expiresAt"] = current + expires_in * 1000

    refresh_expires_in = payload.get("refresh_token_expires_in")
    if isinstance(refresh_expires_in, (int, float)):
        merged["refreshTokenExpiresAt"] = current + refresh_expires_in * 1000

    scope = payload.get("scope")
    if isinstance(scope, str) and scope:
        merged["scopes"] = scope.split()

    return merged


def _cooldown_active(number: int, *, now: float) -> bool:
    slot = Accounts.load().get(number)
    if slot is None:
        return False
    attempted = slot.token.get("attemptedAtMs")
    if not isinstance(attempted, (int, float)) or attempted <= 0:
        return False
    if slot.token.get("error") in (None, ""):
        return False
    return now - attempted / 1000 < REFRESH_COOLDOWN


def _record(number: int, outcome: str, *, now: float) -> None:
    def _write(current: Accounts) -> None:
        token = dict(current.ensure(number).token)
        token["attemptedAtMs"] = now * 1000
        if outcome == REFRESHED:
            token["refreshedAtMs"] = now * 1000
            token["error"] = ""
        else:
            token["error"] = outcome
        current.ensure(number).token = token

    update_accounts(_write)


def _backup_credentials(path: Path) -> None:
    """Keep one generation of the previous tokens, at the same permissions.

    `backup_file` drops its copy into `backups/`, which is not hardened the way
    `creds/` is -- and a credentials file is the one thing in this tree that
    must never widen its permissions on the way to a backup.
    """
    copied = backup_file(path)
    if copied is None:
        return
    with contextlib.suppress(secure.PermissionWarning, OSError):
        secure.harden_file(copied)


def _copy_mode_blocked(number: int, config: Config | None) -> bool:
    """In copy mode `creds/<n>/` is a stale copy, not the live credentials.

    The running slot keeps its real tokens in the shared `~/.claude/.credentials.json`,
    and whatever we write into its copy is either overwritten on exit or, worse,
    restored over a token claude has since rotated.
    """
    if config is None:
        config = Config.load()
    if config.cred_mode != CRED_MODE_COPY:
        return False

    held = sessions.read_lock()
    if held is None:
        return False
    return int(held.get("slot", 0) or 0) == number


def refresh_slot(
    number: int,
    *,
    busy: set[int] | None = None,
    force: bool = False,
    timeout: float = REFRESH_TIMEOUT,
    config: Config | None = None,
) -> str:
    """Mint a fresh access token for one slot from its refresh token.

    Only ever useful for a slot nobody is running: claude refreshes its own
    token while it works. That is also what makes it safe -- the hazard here is
    rotating a refresh token out from under a live process that still holds the
    old one in memory, and a slot with no live process has no such holder.
    """
    now = time.time()
    if busy is None:
        busy = sessions.busy_slots()
    if number in busy:
        return BUSY
    if _copy_mode_blocked(number, config):
        return SKIPPED
    if not force and _cooldown_active(number, now=now):
        return SKIPPED

    path = creds_file(number)
    with file_lock(refresh_lock_path(number), timeout=LOCK_TIMEOUT) as taken:
        if not taken:
            # file_lock lets a timed-out caller proceed unlocked, which is the
            # right trade for accounts.json and the wrong one here: a lost
            # rotation costs a re-login.
            return BUSY

        oauth = claudecfg.read_credentials(path)
        if not oauth or not oauth.get("refreshToken"):
            return SKIPPED
        if not force and not needs_refresh(oauth, now_ms=now * 1000):
            return SKIPPED

        sent = oauth["refreshToken"]
        scopes = [s for s in (oauth.get("scopes") or []) if isinstance(s, str)]

        try:
            payload = _post_refresh(sent, scopes, timeout=timeout)
        except OAuthError as exc:
            outcome = DEAD if exc.kind == "dead" else FAILED
            log.write(f"slot {number}: token refresh failed ({exc})")
            _record(number, outcome, now=time.time())
            return outcome

        # The POST took up to half a minute. If claude started on this slot
        # meanwhile and rotated the token itself, our answer is stale and
        # writing it would revoke the one now in use.
        current = claudecfg.read_credentials(path)
        if not current or current.get("refreshToken") != sent:
            log.write(f"slot {number}: refresh discarded, credentials changed under us")
            return SKIPPED

        raw = read_json(path)
        if not isinstance(raw, dict):
            raw = {}
        _backup_credentials(path)
        raw["claudeAiOauth"] = merge_tokens(current, payload)
        write_json_atomic(path, raw)

    log.write(f"slot {number}: access token refreshed")
    _record(number, REFRESHED, now=time.time())
    return REFRESHED
