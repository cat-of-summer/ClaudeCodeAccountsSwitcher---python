from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from core import claudecfg, log
from core.store import Slot, creds_file
from ui.i18n import t

API_BASE = "https://api.anthropic.com"
USAGE_PATH = "/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "ccas"
DEFAULT_TIMEOUT = 5.0
MAX_PARALLEL = 8
USAGE_TTL_SECONDS = 600.0

TOKEN_LABEL_KEYS = {
    "ok": "usage.token_ok",
    "expired": "usage.token_expired",
    "stale": "usage.token_stale",
    "missing": "usage.token_missing",
}


def fetch_live(token: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any] | None:
    request = urllib.request.Request(
        f"{API_BASE}{USAGE_PATH}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-beta": BETA_HEADER,
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # A dead access token answers 401 here. Without this branch it is
        # indistinguishable from "no network" and the limits stay silently blank.
        log.write(f"usage request rejected: HTTP {exc.code} {exc.reason}")
        return None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log.write(f"usage request failed: {exc!r}")
        return None
    return payload if isinstance(payload, dict) else None


def _normalise(payload: dict[str, Any]) -> dict[str, Any]:
    # The endpoint returns the windows at the top level; older payloads nested
    # them under "utilization". Accept both.
    utilization = payload.get("utilization")
    source = utilization if isinstance(utilization, dict) else payload
    return {
        "fetchedAtMs": datetime.now(timezone.utc).timestamp() * 1000,
        "five_hour": source.get("five_hour"),
        "seven_day": source.get("seven_day"),
    }


def is_stale(usage: dict[str, Any] | None, *, ttl: float = USAGE_TTL_SECONDS) -> bool:
    if not usage:
        return True
    fetched_at_ms = usage.get("fetchedAtMs")
    if not isinstance(fetched_at_ms, (int, float)) or fetched_at_ms <= 0:
        return True
    return datetime.now(timezone.utc).timestamp() - fetched_at_ms / 1000 >= ttl


def refreshable(slots: list[Slot]) -> list[Slot]:
    """Slots holding a token we can actually ask the API with."""
    return [slot for slot in slots if claudecfg.access_token(creds_file(slot.number))]


def refresh_slots(
    slots: list[Slot], *, timeout: float = DEFAULT_TIMEOUT
) -> dict[int, dict[str, Any]]:
    targets: list[tuple[int, str]] = []
    for slot in slots:
        token = claudecfg.access_token(creds_file(slot.number))
        if token:
            targets.append((slot.number, token))
    if not targets:
        return {}

    results: dict[int, dict[str, Any]] = {}
    workers = min(MAX_PARALLEL, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_live, token, timeout=timeout): number
            for number, token in targets
        }
        for future in futures:
            number = futures[future]
            try:
                payload = future.result()
            except Exception:
                payload = None
            if payload:
                results[number] = _normalise(payload)
    return results


def _parse_reset(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone()


def age_text(fetched_at_ms: Any) -> str:
    if not isinstance(fetched_at_ms, (int, float)) or fetched_at_ms <= 0:
        return ""

    seconds = datetime.now(timezone.utc).timestamp() - fetched_at_ms / 1000
    if seconds < 90:
        return t("usage.age_now")

    minutes = seconds / 60
    if minutes < 90:
        return t("usage.age_minutes", value=int(minutes))

    hours = minutes / 60
    if hours < 36:
        return t("usage.age_hours", value=int(hours))

    return t("usage.age_days", value=int(hours / 24))


def format_usage(usage: dict[str, Any] | None) -> str:
    if not usage:
        return t("usage.unknown")

    five_hour = usage.get("five_hour")
    if not isinstance(five_hour, dict):
        return t("usage.unknown")

    percent = five_hour.get("utilization")
    parts: list[str] = []
    if isinstance(percent, (int, float)):
        parts.append(t("usage.five_hour", percent=int(percent)))
    else:
        parts.append(t("usage.five_hour_unknown"))

    seven_day = usage.get("seven_day")
    if isinstance(seven_day, dict):
        weekly = seven_day.get("utilization")
        if isinstance(weekly, (int, float)):
            parts.append(t("usage.seven_day", percent=int(weekly)))

    reset = _parse_reset(five_hour.get("resets_at"))
    if reset is not None:
        parts.append(t("usage.reset_at", time=reset.strftime("%H:%M")))

    age = age_text(usage.get("fetchedAtMs"))
    if age and age != t("usage.age_now"):
        parts.append(f"({age})")

    return " · ".join(parts)


def format_token_state(state: str) -> str:
    key = TOKEN_LABEL_KEYS.get(state)
    return t(key) if key else state


def is_unknown(text: str) -> bool:
    return text == t("usage.unknown")
