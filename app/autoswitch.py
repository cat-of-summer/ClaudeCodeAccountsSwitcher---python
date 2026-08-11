from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import claudecfg, log
from core.store import (
    Accounts,
    Config,
    Slot,
    app_dir,
    creds_file,
    file_lock,
    read_json,
    write_json_atomic,
)
from ui import usage

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Flags that hand the session to something we cannot restart behind the user's
# back: a pipe, a background agent, a cloud run, a stream protocol.
OPT_OUT_FLAGS = frozenset(
    {
        "-p",
        "--print",
        "--bg",
        "--background",
        "--cloud",
        "--fork-session",
        "--no-session-persistence",
        "--input-format",
        "--output-format",
    }
)

RESUME_FLAGS = frozenset({"-r", "--resume"})
CONTINUE_FLAGS = frozenset({"-c", "--continue"})
SESSION_ID_FLAG = "--session-id"
PERMISSION_MODE_FLAG = "--permission-mode"
BYPASS_FLAG = "--dangerously-skip-permissions"
ALLOW_BYPASS_FLAG = "--allow-dangerously-skip-permissions"

# `default` appears in transcripts but is not one of the values the CLI accepts,
# so restoring it would abort the relaunch we are in the middle of.
PERMISSION_MODES = frozenset(
    {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}
)

WATCH_POLL_SECONDS = 2.0
CONFIRM_DELAY_SECONDS = 5.0
PLAN_TTL_SECONDS = 300.0
PLAN_LOCK_TIMEOUT = 5.0

FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"


# --------------------------------------------------------------------------
# argument inspection
# --------------------------------------------------------------------------


def _split(token: str) -> tuple[str, str | None]:
    if token.startswith("--") and "=" in token:
        name, value = token.split("=", 1)
        return name, value
    return token, None


@dataclass
class ArgFacts:
    opt_out: bool = False
    session_id: str | None = None
    resumes: bool = False


def inspect_args(args: list[str]) -> ArgFacts:
    facts = ArgFacts()
    index = 0
    while index < len(args):
        name, inline = _split(args[index])

        if name in OPT_OUT_FLAGS:
            facts.opt_out = True
        elif name == SESSION_ID_FLAG:
            value = inline
            if value is None and index + 1 < len(args):
                value = args[index + 1]
                index += 1
            if value and UUID_RE.match(value):
                facts.session_id = value
        elif name in RESUME_FLAGS:
            facts.resumes = True
            value = inline
            if value is None and index + 1 < len(args) and not args[index + 1].startswith("-"):
                value = args[index + 1]
                index += 1
            if value and UUID_RE.match(value):
                facts.session_id = value
        elif name in CONTINUE_FLAGS or name == "--from-pr":
            facts.resumes = True

        index += 1
    return facts


@dataclass
class SessionPlan:
    supervise: bool
    session_id: str | None
    args: list[str]


def plan_session(config: Config, args: list[str], *, interactive: bool) -> SessionPlan:
    """Decide whether this launch can be supervised, and under which session id.

    The id is injected into the launch arguments only, never into
    `Config.default_args`: a session id stored in config.json would collapse
    every future conversation into one, and `merge_default_args` would drop it
    on the relaunch anyway.
    """
    plain = SessionPlan(False, None, list(args))

    if not config.auto_switch.get("enabled"):
        return plain
    if not interactive:
        return plain

    from app.wrapper import CLAUDE_SUBCOMMANDS  # imported late: wrapper imports us

    if args and args[0] in CLAUDE_SUBCOMMANDS:
        return plain

    facts = inspect_args(args)
    if facts.opt_out:
        return plain
    if facts.session_id:
        return SessionPlan(True, facts.session_id, list(args))
    if facts.resumes:
        # `-r` with no id opens a picker and `-c` takes the latest: we cannot
        # know the id up front, so the relaunch falls back to --continue.
        return SessionPlan(True, None, list(args))

    fresh = str(uuid.uuid4())
    return SessionPlan(True, fresh, [SESSION_ID_FLAG, fresh, *args])


def strip_session_flags(args: list[str]) -> list[str]:
    kept: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        name, inline = _split(token)

        if name == SESSION_ID_FLAG:
            if inline is None and index + 1 < len(args):
                index += 1
            index += 1
            continue
        if name in RESUME_FLAGS:
            if inline is None and index + 1 < len(args) and not args[index + 1].startswith("-"):
                index += 1
            index += 1
            continue
        if name in CONTINUE_FLAGS:
            index += 1
            continue
        if name == PERMISSION_MODE_FLAG:
            if inline is None and index + 1 < len(args):
                index += 1
            index += 1
            continue

        kept.append(token)
        index += 1
    return kept


def relaunch_args(
    args: list[str],
    *,
    session_id: str | None,
    mode: str = "",
    config: Config | None = None,
) -> list[str]:
    """Rebuild the command line that resumes this conversation on another slot."""
    auto = (config.auto_switch if config is not None else {}) or {}
    rest = strip_session_flags(args)

    # A prompt given on the command line was already delivered to the session we
    # are resuming; sending it again would restart the task from the top.
    if rest and not rest[0].startswith("-"):
        rest = rest[1:]

    head: list[str] = []
    if auto.get("restoreMode", True) and mode in PERMISSION_MODES:
        head += [PERMISSION_MODE_FLAG, mode]
        defaults = config.default_args if config is not None else []
        if mode != "bypassPermissions" and BYPASS_FLAG in defaults:
            # merge_default_args() treats the two spellings as one flag, so
            # naming the permissive variant here suppresses the bypass that
            # would otherwise override the mode we just restored.
            head.append(ALLOW_BYPASS_FLAG)

    if session_id:
        head = ["--resume", session_id, *head]
    else:
        head = ["--continue", *head]

    prompt = str(auto.get("resumePrompt") or "").strip()
    tail = [prompt] if prompt else []
    return [*head, *rest, *tail]


# --------------------------------------------------------------------------
# watching the transcript
# --------------------------------------------------------------------------


class LimitSignal:
    """What the watcher tells the supervisor, guarded for cross-thread reads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._window = ""
        self._mode = ""

    @property
    def fired(self) -> bool:
        return self._event.is_set()

    @property
    def window(self) -> str:
        with self._lock:
            return self._window

    @property
    def permission_mode(self) -> str:
        with self._lock:
            return self._mode

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def note_mode(self, mode: str) -> None:
        with self._lock:
            self._mode = mode

    def fire(self, window: str) -> None:
        with self._lock:
            self._window = window
        self._event.set()


def project_slug(path: Path) -> str:
    """How claude names the transcript directory for a working directory."""
    return re.sub(r"[^A-Za-z0-9-]", "-", str(path))


class TranscriptWatcher(threading.Thread):
    """Tails one session's transcript for the moment claude gives up on quota.

    Polling the usage API would tell us the account is spent, but not that this
    particular session has stopped being able to work -- and killing a claude
    that is still grinding through a long tool call is exactly what we must not
    do. The 429 line only appears once the session is actually blocked.
    """

    def __init__(
        self,
        session_id: str | None,
        *,
        since: float,
        signal: LimitSignal,
        cwd: Path | None = None,
        poll: float = WATCH_POLL_SECONDS,
    ) -> None:
        super().__init__(daemon=True)
        self._session_id = session_id
        self._since = since
        self._signal = signal
        self._cwd = cwd or Path.cwd()
        self._poll = poll
        self._stop = threading.Event()
        self._path: Path | None = None
        self._offset = 0
        self._buffer = b""

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while True:
            try:
                self.tick()
            except OSError:
                self._path = None
            if self._stop.wait(self._poll):
                return

    # -- internals, public enough for the tests to drive one step at a time --

    def tick(self) -> None:
        if self._path is None or not self._path.exists():
            found = self._locate()
            if found is None:
                return
            self._path = found
            self._offset = 0
            self._buffer = b""

        size = self._path.stat().st_size
        if size < self._offset:
            # Truncated and restarted; anything we had buffered is gone.
            self._offset = 0
            self._buffer = b""
        if size == self._offset:
            return

        with self._path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
        self._offset += len(chunk)

        parts = (self._buffer + chunk).split(b"\n")
        # A trailing fragment is a half-written record: hold it until the rest
        # of the line lands rather than failing to parse it now.
        self._buffer = parts.pop()
        for line in parts:
            self._consume(line)

    def _locate(self) -> Path | None:
        projects = claudecfg.config_dir() / "projects"
        if not projects.is_dir():
            return None

        if self._session_id:
            matches = sorted(projects.glob(f"*/{self._session_id}.jsonl"))
            return matches[0] if matches else None

        # No id to glob for: take the newest transcript this session could have
        # written, preferring the directory that belongs to our cwd.
        preferred = projects / project_slug(self._cwd)
        if preferred.is_dir():
            found = list(preferred.glob("*.jsonl"))
        else:
            found = list(projects.glob("*/*.jsonl"))

        fresh = [path for path in found if path.stat().st_mtime >= self._since]
        if not fresh:
            return None
        return max(fresh, key=lambda path: path.stat().st_mtime)

    def _consume(self, raw: bytes) -> None:
        if not raw.strip():
            return
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(record, dict):
            return

        mode = record.get("permissionMode")
        if isinstance(mode, str) and mode:
            # Written both as its own record when the mode changes and as a
            # field on every message the human typed, so tailing the file we
            # already tail is all it takes to restore the mode later.
            self._signal.note_mode(mode)

        if self._signal.fired:
            return
        if record.get("error") != "rate_limit":
            return
        try:
            status = int(record.get("apiErrorStatus") or 0)
        except (TypeError, ValueError):
            return
        if status != 429:
            return

        moment = usage.parse_iso(record.get("timestamp"))
        if moment is not None and moment.timestamp() < self._since:
            # A resumed transcript replays its history; only this run counts.
            return

        window = _window_of(record)
        log.write(f"transcript: rate limit observed ({window or 'unspecified'})")
        self._signal.fire(window)


def _window_of(record: dict[str, Any]) -> str:
    """Best-effort guess at which window ran out, for the message only.

    The text is English and has had several wordings, so nothing may depend on
    it -- the decision to switch is made from the API, not from here.
    """
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    lowered = text.lower()
    if "week" in lowered:
        return SEVEN_DAY
    if "limit" in lowered:
        return FIVE_HOUR
    return ""


# --------------------------------------------------------------------------
# deciding where to go
# --------------------------------------------------------------------------


def utilisation(payload: dict[str, Any] | None, window: str) -> float | None:
    if not isinstance(payload, dict):
        return None
    block = payload.get(window)
    if not isinstance(block, dict):
        return None
    value = block.get("utilization")
    return float(value) if isinstance(value, (int, float)) else None


def is_exhausted(payload: dict[str, Any] | None, *, threshold: float) -> bool:
    five = utilisation(payload, FIVE_HOUR)
    seven = utilisation(payload, SEVEN_DAY)
    return bool(
        (five is not None and five >= threshold)
        or (seven is not None and seven >= 100)
    )


def confirm_exhausted(
    slot: Slot, *, threshold: float, delay: float = CONFIRM_DELAY_SECONDS
) -> bool | None:
    """Ask the API whether the quota really is gone. None means "could not tell".

    claude retries a 429 several times, and a transient one looks exactly like
    a real one in the transcript. Refusing to switch when the network is down
    is the safe reading: a burst of transient 429s would otherwise scatter
    every open session across accounts for nothing.
    """
    if delay > 0:
        time.sleep(delay)

    fresh = usage.refresh_slots(
        [slot], refresh_timeout=usage.INTERACTIVE_REFRESH_TIMEOUT
    )
    payload = fresh.get(slot.number)
    if payload is None:
        return None
    slot.usage = payload
    return is_exhausted(payload, threshold=threshold)


def candidates(
    accounts: Accounts, *, current: int, tried: set[int]
) -> list[Slot]:
    usable: list[Slot] = []
    for slot in accounts.ordered():
        if slot.number == current or slot.number in tried:
            continue
        if not slot.has_credentials():
            continue
        if claudecfg.token_state(creds_file(slot.number)) == "stale":
            continue
        usable.append(slot)
    return usable


def pick_target(
    accounts: Accounts,
    *,
    current: int,
    tried: set[int],
    threshold: float,
    strategy: str = "limits",
) -> tuple[int | None, str]:
    pool = candidates(accounts, current=current, tried=tried)
    if not pool:
        return None, "no_candidates"

    if strategy != "limits":
        return _round_robin(pool, current=current), "order"

    ranked: list[Slot] = []
    unknown: list[Slot] = []
    for slot in pool:
        if not usage.is_usable_for_ranking(slot.usage):
            unknown.append(slot)
        elif not is_exhausted(slot.usage, threshold=threshold):
            ranked.append(slot)

    if ranked:
        # Weekly first: an account out of weekly budget is useless for days,
        # while a spent 5h window comes back today.
        ranked.sort(
            key=lambda slot: (
                utilisation(slot.usage, SEVEN_DAY) or 0.0,
                utilisation(slot.usage, FIVE_HOUR) or 0.0,
                slot.number,
            )
        )
        return ranked[0].number, "limits"

    if unknown:
        return _round_robin(unknown, current=current), "order"

    return None, "all_exhausted"


def _round_robin(pool: list[Slot], *, current: int) -> int:
    numbers = sorted(slot.number for slot in pool)
    for number in numbers:
        if number > current:
            return number
    return numbers[0]


# --------------------------------------------------------------------------
# agreeing with the other terminals
# --------------------------------------------------------------------------


def switch_plan_path() -> Path:
    return app_dir() / "switch-plan.json"


def switch_plan_lock_path() -> Path:
    return app_dir() / "switch-plan.lock"


def _plan_valid(plan: Any, current: int, now: float) -> bool:
    if not isinstance(plan, dict):
        return False
    if int(plan.get("from", 0) or 0) != current:
        return False
    if not int(plan.get("to", 0) or 0):
        return False
    return now < float(plan.get("expiresAt", 0) or 0)


@dataclass
class Election:
    target: int | None
    reason: str
    followed: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


def elect_target(
    accounts: Accounts,
    *,
    current: int,
    tried: set[int],
    threshold: float,
    strategy: str = "limits",
    now: float | None = None,
) -> Election:
    """Pick a slot, or adopt the pick another terminal already made.

    Keyed on the slot being left, not on the slot going active: two windows on
    the same account hit the same wall seconds apart, and the second one must
    follow the first rather than look at the freshly activated slot and choose
    a third account. A window on some other slot sees a plan that is not about
    its own slot and decides for itself.

    The lock is its own file on purpose. `accounts.lock` is taken by
    `update_accounts` further down this path, and the platform lock is not
    reentrant -- nesting them would deadlock against ourselves.
    """
    moment = time.time() if now is None else now

    with file_lock(switch_plan_lock_path(), timeout=PLAN_LOCK_TIMEOUT):
        plan = read_json(switch_plan_path())
        if _plan_valid(plan, current, moment):
            target = int(plan["to"])
            log.write(f"autoswitch: following plan {current} -> {target}")
            return Election(target, str(plan.get("reason") or "followed"), followed=True)

        target, reason = pick_target(
            accounts,
            current=current,
            tried=tried,
            threshold=threshold,
            strategy=strategy,
        )
        if target is None:
            return Election(None, reason)

        write_json_atomic(
            switch_plan_path(),
            {
                "from": current,
                "to": target,
                "reason": reason,
                "decidedAt": moment,
                "expiresAt": moment + PLAN_TTL_SECONDS,
            },
            harden=False,
        )

    log.write(f"autoswitch: elected {current} -> {target} ({reason})")
    return Election(target, reason)
