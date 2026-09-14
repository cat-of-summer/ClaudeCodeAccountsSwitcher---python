from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from core import claudecfg, log, oauth, sessions
from core.oauth import API_BASE, BETA_HEADER, USER_AGENT
from core.store import (
    Accounts,
    Slot,
    creds_file,
    identity_file,
    update_accounts,
    write_json_atomic,
)
from ui.i18n import t

USAGE_PATH = "/api/oauth/usage"
DEFAULT_TIMEOUT = 5.0
DEFAULT_RESET_FORMAT = "%d.%m %H:%M"

FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"

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
    """Good enough to show in a listing without asking the network first."""
    return not is_stale(usage, ttl=USAGE_TTL_RANKING_SECONDS)


# --------------------------------------------------------------------------
# reading a window
# --------------------------------------------------------------------------


def window(usage: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if not isinstance(usage, dict):
        return None
    block = usage.get(name)
    return block if isinstance(block, dict) else None


def window_reset_at(usage: dict[str, Any] | None, name: str) -> datetime | None:
    block = window(usage, name)
    return parse_iso(block.get("resets_at")) if block else None


def has_rolled_over(
    usage: dict[str, Any] | None, name: str, *, now: float | None = None
) -> bool:
    """The window named its own expiry, and that moment has passed."""
    moment = window_reset_at(usage, name)
    if moment is None:
        return False
    return moment.timestamp() <= (time.time() if now is None else now)


def effective_utilisation(
    usage: dict[str, Any] | None, name: str, *, now: float | None = None
) -> float | None:
    """How full the window is *now*, not when the snapshot was taken.

    A 5-hour window runs out and comes back inside the time a stored reading
    stays nominally usable, so a slot could sit at a recorded 100 % for hours
    after it had emptied -- which is how `ccas` came to report that every
    account was spent while one of them was free. The payload carries the
    moment the window rolls over, and once that moment is behind us the
    recorded percentage describes a window that no longer exists.
    """
    block = window(usage, name)
    if block is None:
        return None
    if has_rolled_over(usage, name, now=now):
        return 0.0
    value = block.get("utilization")
    return float(value) if isinstance(value, (int, float)) else None


# --------------------------------------------------------------------------
# asking the network
# --------------------------------------------------------------------------


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


def _budget(timeout: float, refresh_timeout: float) -> float:
    """A ceiling for the whole refresh, however badly one socket behaves.

    Worst case for a single slot is an exchange, a request, a forced exchange
    and a retry. Naming the sum is what keeps a name lookup that ignores its
    own timeout -- getaddrinfo does, on every platform -- from becoming an
    unbounded wait for the user.
    """
    return 2 * (timeout + refresh_timeout) + 5.0


def _spawn(target: Any, *args: Any) -> threading.Thread:
    """A daemon thread, deliberately not a ThreadPoolExecutor.

    The executor registers an atexit hook that joins every worker it ever
    created, so one socket wedged in a name lookup takes the whole process down
    with it on the way out -- `ccas` freezing at startup with nothing to type
    into was exactly that. A daemon thread the interpreter is willing to
    abandon cannot do it.
    """
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return thread


def _join_all(workers: list[threading.Thread], *, budget: float) -> None:
    deadline = time.monotonic() + budget
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))


def refresh_slots(
    slots: list[Slot],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    refresh_timeout: float = oauth.REFRESH_TIMEOUT,
) -> dict[int, dict[str, Any]]:
    targets = refreshable(slots)
    if not targets:
        return {}

    # Read the live-session list once: it is shared by every worker below and
    # the answer cannot meaningfully change inside one refresh.
    busy = sessions.busy_slots()

    results: dict[int, dict[str, Any]] = {}
    guard = threading.Lock()

    def _work(number: int) -> None:
        try:
            payload = _refresh_one(
                number, busy=busy, timeout=timeout, refresh_timeout=refresh_timeout
            )
        except Exception as exc:
            log.write(f"slot {number}: usage refresh raised {exc!r}")
            return
        if payload:
            with guard:
                results[number] = payload

    _join_all(
        [_spawn(_work, slot.number) for slot in targets],
        budget=_budget(timeout, refresh_timeout),
    )
    with guard:
        collected = dict(results)

    name_slots(targets, timeout=timeout)
    return collected


def name_slots(slots: list[Slot], *, timeout: float = DEFAULT_TIMEOUT) -> int:
    """Give an unnamed slot its email, from the account its own token belongs to.

    Fires only for a slot that never had a session end cleanly enough for
    `capture_identity` to find its own account in the shared config -- on a
    machine with several terminals open that is common, and the slot would
    otherwise be listed as a bare number forever. The token knows its owner and
    nobody else's, so unlike the shared config there is no race to lose.

    The passed-in `Slot` objects are updated in place, so the caller's next
    print already shows the name.
    """
    unnamed = [slot for slot in slots if not slot.email]
    if not unnamed:
        return 0

    found: dict[int, dict[str, str]] = {}
    guard = threading.Lock()

    def _work(slot: Slot) -> None:
        token = claudecfg.access_token(creds_file(slot.number))
        if not token:
            return
        identity = oauth.fetch_profile(token, timeout=timeout)
        if not identity or not identity.get("email"):
            return
        with guard:
            found[slot.number] = identity

    _join_all([_spawn(_work, slot) for slot in unnamed], budget=timeout + 5.0)
    with guard:
        collected = dict(found)
    if not collected:
        return 0

    by_number = {slot.number: slot for slot in unnamed}
    for number, identity in collected.items():
        slot = by_number[number]
        slot.email = identity["email"]
        slot.account_uuid = identity["account_uuid"] or slot.account_uuid
        _remember_identity(number, identity)

    def _store(current: Accounts) -> None:
        for stored_number, stored in collected.items():
            target = current.ensure(stored_number)
            target.email = stored["email"]
            target.account_uuid = stored["account_uuid"] or target.account_uuid

    update_accounts(_store)
    log.write(f"named slots from the profile endpoint: {sorted(collected)}")
    return len(collected)


def _remember_identity(number: int, identity: dict[str, str]) -> None:
    """Keep the answer where `backfill_identity` finds it without a network."""
    if not identity.get("account_uuid"):
        return
    path = identity_file(number)
    if path.exists():
        return
    write_json_atomic(
        path,
        {
            "oauthAccount": {
                "emailAddress": identity.get("email", ""),
                "accountUuid": identity["account_uuid"],
            },
            "userID": None,
        },
    )


# --------------------------------------------------------------------------
# saying it out loud
# --------------------------------------------------------------------------


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


def format_moment(moment: datetime) -> str:
    """A moment in the user's zone, in the catalog's short form: «05.08 18:50»."""
    pattern = t("usage.reset_format")
    if "%" not in pattern:
        # Ключа нет в каталоге — t() вернул само имя ключа, и оно уехало бы
        # в вывод литералом.
        pattern = DEFAULT_RESET_FORMAT
    return moment.strftime(pattern)


def format_reset(value: Any) -> str:
    moment = parse_iso(value)
    if moment is None:
        return ""
    return t("usage.reset_at", time=format_moment(moment))


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


def _labelled(label: str, usage: dict[str, Any] | None, name: str) -> str:
    # A reset moment already behind us belongs to the window that just ended,
    # not to the one being described; printed next to a zero it would only
    # contradict it.
    if has_rolled_over(usage, name):
        return label
    block = window(usage, name) or {}
    reset = format_reset(block.get("resets_at"))
    return f"{label} {reset}" if reset else label


def format_usage(usage: dict[str, Any] | None) -> str:
    if not usage or window(usage, FIVE_HOUR) is None:
        return t("usage.unknown")

    percent = effective_utilisation(usage, FIVE_HOUR)
    parts: list[str] = []
    if percent is not None:
        # Сброс идёт при своём окне: у недельного он через несколько суток,
        # и общая метка в хвосте не сказала бы, к какому окну относится.
        parts.append(
            _labelled(t("usage.five_hour", percent=int(percent)), usage, FIVE_HOUR)
        )
    else:
        parts.append(t("usage.five_hour_unknown"))

    weekly = effective_utilisation(usage, SEVEN_DAY)
    if weekly is not None:
        parts.append(
            _labelled(t("usage.seven_day", percent=int(weekly)), usage, SEVEN_DAY)
        )

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
