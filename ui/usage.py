from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from core import claudecfg, log, oauth, sessions
from core.store import Slot, creds_file
from ui.i18n import t

API_BASE = "https://api.anthropic.com"
USAGE_PATH = "/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "ccas"
DEFAULT_TIMEOUT = 5.0
MAX_PARALLEL = 8
DEFAULT_RESET_FORMAT = "%d.%m %H:%M"

# The 5h window runs 0 -> 100 % inside a single sitting, so a ten minute old
# snapshot of the slot you are about to launch is already suspect. The weekly
# window cannot move faster than ~1/168 of its budget per hour, which is why a
# far older snapshot is still a sound key to rank accounts by.
USAGE_TTL_SECONDS = 600.0
USAGE_TTL_IDLE_SECONDS = 3600.0
USAGE_TTL_RANKING_SECONDS = 21600.0

# The token exchange is allowed 30s by claude itself, but a listing that waits
# half a minute per dead slot is unusable, so interactive paths cut it short.
INTERACTIVE_REFRESH_TIMEOUT = 10.0


def fetch_live_ex(
    token: str, *, timeout: float = DEFAULT_TIMEOUT
) -> tuple[dict[str, Any] | None, int | None]:
    """The usage GET, with the status code the caller needs to react to.

    401 means this particular token is finished, which is worth one forced
    exchange; a timeout means the network is, which is not.
    """
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
                return None, response.status
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log.write(f"usage request rejected: HTTP {exc.code} {exc.reason}")
        return None, exc.code
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log.write(f"usage request failed: {exc!r}")
        return None, None
    return (payload if isinstance(payload, dict) else None), 200


def fetch_live(token: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any] | None:
    return fetch_live_ex(token, timeout=timeout)[0]


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


def needs_network(slot: Slot, *, active: bool) -> bool:
    ttl = USAGE_TTL_SECONDS if active else USAGE_TTL_IDLE_SECONDS
    return is_stale(slot.usage, ttl=ttl)


def is_usable_for_ranking(usage: dict[str, Any] | None) -> bool:
    """Good enough to decide which account to move a session to."""
    return not is_stale(usage, ttl=USAGE_TTL_RANKING_SECONDS)


def refreshable(slots: list[Slot]) -> list[Slot]:
    """Slots we can get an answer for, once expired tokens are exchanged."""
    return [
        slot
        for slot in slots
        if claudecfg.token_state(creds_file(slot.number)) in ("ok", "expired")
    ]


def _refresh_one(
    number: int,
    *,
    busy: set[int],
    timeout: float,
    refresh_timeout: float,
) -> dict[str, Any] | None:
    path = creds_file(number)
    state = claudecfg.token_state(path)
    if state in ("missing", "stale"):
        return None

    if state == "expired":
        oauth.refresh_slot(number, busy=busy, timeout=refresh_timeout)

    token = claudecfg.access_token(path)
    if not token:
        return None

    payload, status = fetch_live_ex(token, timeout=timeout)
    if payload is None and status in (401, 403):
        # The server retired the token ahead of its own expiresAt. One forced
        # exchange, one retry -- looping here would hammer a revoked account.
        if (
            oauth.refresh_slot(number, busy=busy, force=True, timeout=refresh_timeout)
            == oauth.REFRESHED
        ):
            retried = claudecfg.access_token(path)
            if retried:
                payload, _ = fetch_live_ex(retried, timeout=timeout)

    return _normalise(payload) if payload else None


def refresh_slots(
    slots: list[Slot],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    refresh_timeout: float = oauth.REFRESH_TIMEOUT,
) -> dict[int, dict[str, Any]]:
    targets = [slot.number for slot in refreshable(slots)]
    if not targets:
        return {}

    # Read the live-session list once: it costs a tasklist call per entry on
    # Windows, and the answer cannot meaningfully change inside one refresh.
    busy = sessions.busy_slots()

    results: dict[int, dict[str, Any]] = {}
    workers = min(MAX_PARALLEL, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _refresh_one,
                number,
                busy=busy,
                timeout=timeout,
                refresh_timeout=refresh_timeout,
            ): number
            for number in targets
        }
        for future in futures:
            number = futures[future]
            try:
                payload = future.result()
            except Exception as exc:
                log.write(f"slot {number}: usage refresh raised {exc!r}")
                payload = None
            if payload:
                results[number] = payload
    return results


def parse_iso(value: Any) -> datetime | None:
    """ISO 8601 with a zone -> an aware datetime in the local zone.

    Both the reset moments from the usage API and the timestamps claude writes
    into its transcript arrive in this shape, the latter with a `Z` suffix that
    `fromisoformat` refuses before Python 3.11.
    """
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


def format_reset(value: Any) -> str:
    """Момент сброса окна в локальной зоне пользователя: «05.08 18:50»."""
    moment = parse_iso(value)
    if moment is None:
        return ""
    pattern = t("usage.reset_format")
    if "%" not in pattern:
        # Ключа нет в каталоге — t() вернул само имя ключа, и оно уехало бы
        # в вывод литералом.
        pattern = DEFAULT_RESET_FORMAT
    return t("usage.reset_at", time=moment.strftime(pattern))


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


def _window(label: str, block: dict[str, Any]) -> str:
    reset = format_reset(block.get("resets_at"))
    return f"{label} {reset}" if reset else label


def format_usage(usage: dict[str, Any] | None) -> str:
    if not usage:
        return t("usage.unknown")

    five_hour = usage.get("five_hour")
    if not isinstance(five_hour, dict):
        return t("usage.unknown")

    percent = five_hour.get("utilization")
    parts: list[str] = []
    if isinstance(percent, (int, float)):
        # Сброс идёт при своём окне: у недельного он через несколько суток,
        # и общая метка в хвосте не сказала бы, к какому окну относится.
        parts.append(_window(t("usage.five_hour", percent=int(percent)), five_hour))
    else:
        parts.append(t("usage.five_hour_unknown"))

    seven_day = usage.get("seven_day")
    if isinstance(seven_day, dict):
        weekly = seven_day.get("utilization")
        if isinstance(weekly, (int, float)):
            parts.append(_window(t("usage.seven_day", percent=int(weekly)), seven_day))

    age = age_text(usage.get("fetchedAtMs"))
    if age and age != t("usage.age_now"):
        parts.append(f"({age})")

    return " · ".join(parts)


def is_unknown(text: str) -> bool:
    return text == t("usage.unknown")


def describe(slot: Slot, *, state: str | None = None, colour: bool = False) -> str:
    """The limits column for one slot, in every listing we have.

    An access token that expired is the normal resting state of a slot nobody
    ran today, and claude mints a new one from the refresh token the moment it
    starts. Letting that state replace the numbers -- which `ccas list` used to
    do -- threw away a perfectly good weekly figure that had not moved, and
    replaced it with a word the reader can do nothing about.
    """
    from ui import screen

    if state is None:
        state = claudecfg.token_state(creds_file(slot.number))

    def paint(text: str, colour_code: str) -> str:
        return f"{colour_code}{text}{screen.RESET}" if colour else text

    if state == "missing":
        return paint(t("menu.slot_no_login"), screen.DIM)
    if state == "stale":
        return paint(t("menu.slot_relogin"), screen.YELLOW)

    summary = format_usage(slot.usage)
    detail = paint(summary, screen.DIM) if is_unknown(summary) else summary
    if state == "expired":
        detail = f"{detail}  {paint(t('menu.token_self_refresh'), screen.DIM)}"
    return detail
