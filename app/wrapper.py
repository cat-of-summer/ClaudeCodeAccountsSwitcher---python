from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from app import autoswitch
from core import claudecfg, hookbus, log
from core.detect import CRED_MODE_COPY, CRED_MODE_ENV
from core.sessions import (  # noqa: F401  -- re-exported, see note below
    LOCK_STALE_SECONDS,
    acquire_lock,
    other_live_sessions,
    read_lock,
    register_session,
    release_lock,
    sessions_dir,
    unregister_session,
    update_session,
)
from core.store import (
    Accounts,
    Config,
    Slot,
    creds_file,
    ensure_slot_dir,
    identity_file,
    is_installed,
    read_json,
    update_accounts,
    write_json_atomic,
)
from system import console
from ui import notify
from ui.i18n import t

# Session and lock bookkeeping moved to core/sessions.py so that core modules
# can consult it without importing app/. Re-exported here because it reads as
# wrapper vocabulary at every call site in this file.

_SLOT_RE = re.compile(r"^\d+$")
_ALIAS_RE = re.compile(r"^@(.+)$")

CLAUDE_SUBCOMMANDS = frozenset(
    {
        "agents",
        "auth",
        "auto-mode",
        "doctor",
        "gateway",
        "install",
        "mcp",
        "plugin",
        "plugins",
        "project",
        "setup-token",
        "ultrareview",
        "update",
        "upgrade",
    }
)


class WrapperError(Exception):
    pass


def resolve_slot(
    args: list[str], accounts: Accounts
) -> tuple[int | None, list[str], bool]:
    if not args:
        return None, [], False

    head = args[0]

    if head == "--":
        return None, args[1:], False

    if _SLOT_RE.match(head):
        return int(head), args[1:], True

    alias_match = _ALIAS_RE.match(head)
    if alias_match:
        alias = alias_match.group(1)
        slot = accounts.by_alias(alias)
        if slot is None:
            raise WrapperError(t("error.unknown_alias", alias=alias))
        return slot.number, args[1:], True

    return None, args, False


def merge_default_args(default_args: list[str], user_args: list[str]) -> list[str]:
    if user_args and user_args[0] in CLAUDE_SUBCOMMANDS:
        return list(user_args)

    present = {arg.split("=", 1)[0] for arg in user_args if arg.startswith("-")}
    if "--allow-dangerously-skip-permissions" in present:
        present.add("--dangerously-skip-permissions")

    merged = [arg for arg in default_args if arg.split("=", 1)[0] not in present]
    return merged + list(user_args)


def default_slot(accounts: Accounts) -> int:
    """The slot a bare `claude` should resume, without asking."""
    current = accounts.get(accounts.active)
    if current is not None and current.has_credentials():
        return current.number

    usable = [slot for slot in accounts.ordered() if slot.has_credentials()]
    if usable:
        return max(usable, key=lambda slot: slot.last_used_at).number

    return min(accounts.slots) if accounts.slots else accounts.next_free_number()


def adopt_existing_login(config: Config) -> bool:
    if config.cred_mode != CRED_MODE_COPY:
        return False

    from app.installer import adopt_current_login

    adopted: list[int] = []

    def _adopt(fresh: Accounts) -> None:
        number = adopt_current_login(fresh)
        if number is not None:
            adopted.append(number)

    update_accounts(_adopt)
    if not adopted:
        return False

    log.write(f"adopted existing login as slot {adopted[0]}")
    return True


def identity_uuid(identity: dict[str, Any] | None) -> str:
    if not isinstance(identity, dict):
        return ""
    oauth = identity.get("oauthAccount")
    if not isinstance(oauth, dict):
        return ""
    return oauth.get("accountUuid") or ""


def _creds_touched_since(slot: int, moment: float) -> bool:
    try:
        return creds_file(slot).stat().st_mtime >= moment
    except OSError:
        return False


def capture_identity(
    slot: Slot, accounts: Accounts, launched_at: float
) -> dict[str, Any] | None:
    """Record who this slot belongs to, refusing to adopt a neighbour's account.

    `~/.claude.json` is shared by every session, so by the time this session ends
    the identity sitting there may well have been written by a claude running in
    another terminal on another slot. Copying it in blindly is what made two slots
    collapse into one account.
    """
    identity = claudecfg.read_identity()
    if not identity or not identity.get("oauthAccount"):
        return None

    uuid = identity_uuid(identity)
    if not uuid:
        return None

    if uuid != slot.account_uuid:
        claimed = next(
            (
                other
                for other in accounts.ordered()
                if other.number != slot.number and other.account_uuid == uuid
            ),
            None,
        )
        if claimed is not None:
            log.write(
                f"identity capture skipped for slot {slot.number}: {uuid} already "
                f"belongs to slot {claimed.number}"
            )
            return None

        concurrent = other_live_sessions()
        if concurrent:
            slots = ", ".join(str(entry.get("slot")) for entry in concurrent)
            log.write(
                f"identity capture skipped for slot {slot.number}: {uuid} is "
                f"unfamiliar while slots {slots} are still running"
            )
            return None

        if not _creds_touched_since(slot.number, launched_at):
            log.write(
                f"identity capture skipped for slot {slot.number}: own credentials "
                "were never written during this session"
            )
            return None

    write_json_atomic(identity_file(slot.number), identity)

    oauth = identity.get("oauthAccount") or {}
    if isinstance(oauth, dict):
        slot.email = oauth.get("emailAddress") or slot.email
    slot.account_uuid = uuid

    user_id = identity.get("userID")
    if isinstance(user_id, str):
        slot.user_id = user_id

    return identity


def stored_identity(slot: int) -> dict[str, Any] | None:
    raw = read_json(identity_file(slot))
    return raw if isinstance(raw, dict) else None


def capture_usage(slot: Slot, identity: dict[str, Any] | None = None) -> None:
    """Opportunistically lift limits out of the shared config.

    Current claude builds no longer write `cachedUsageUtilization`, so this is a
    bonus rather than a source: `ccas list` refreshes over the network. Both uuids
    must be known and equal -- an unattributable block is another slot's data as
    often as it is ours.
    """
    cached = claudecfg.read_usage_cache()
    if not cached:
        return

    account_uuid = cached.get("accountUuid") or identity_uuid(identity)
    if not slot.account_uuid or not account_uuid:
        return
    if account_uuid != slot.account_uuid:
        return

    utilization = cached.get("utilization")
    if not isinstance(utilization, dict):
        return

    slot.usage = {
        "fetchedAtMs": cached.get("fetchedAtMs") or time.time() * 1000,
        "five_hour": utilization.get("five_hour"),
        "seven_day": utilization.get("seven_day"),
    }


def _shared_account_uuid() -> str:
    oauth = claudecfg.read_credentials(claudecfg.shared_credentials_path())
    if not oauth:
        return ""
    identity = claudecfg.read_identity() or {}
    account = identity.get("oauthAccount")
    if isinstance(account, dict):
        return account.get("accountUuid") or ""
    return ""


def copy_mode_save(slot: Slot) -> bool:
    shared = claudecfg.shared_credentials_path()
    if not shared.exists():
        return False

    current_uuid = _shared_account_uuid()
    if slot.account_uuid and current_uuid and current_uuid != slot.account_uuid:
        log.write(
            f"copy-mode save skipped for slot {slot.number}: shared credentials "
            f"now belong to {current_uuid}"
        )
        return False

    ensure_slot_dir(slot.number)
    shutil.copyfile(shared, creds_file(slot.number))
    return True


def copy_mode_restore(slot: int) -> bool:
    source = creds_file(slot)
    if not source.exists():
        return False

    shared = claudecfg.shared_credentials_path()
    shared.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, shared)

    with contextlib.suppress(Exception):
        from system import secure

        secure.harden_file(shared)
    return True


def build_environment(config: Config, slot: int) -> dict[str, str]:
    env = dict(os.environ)
    if config.cred_mode == CRED_MODE_ENV:
        env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = str(ensure_slot_dir(slot))
    return env


TERMINATE_GRACE_SECONDS = 5.0


def terminate(process: subprocess.Popen[bytes]) -> None:
    """Stop claude and everything it spawned, politely first.

    claude leaves node and ripgrep children holding the console, so on Windows
    the whole tree has to go: killing the parent alone leaves the terminal
    unusable for the session we are about to start in its place.
    """
    if process.poll() is not None:
        return

    try:
        if os.name == "nt":
            _taskkill(process.pid, force=False)
        else:
            process.terminate()
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    with contextlib.suppress(OSError, subprocess.SubprocessError):
        if os.name == "nt":
            _taskkill(process.pid, force=True)
        else:
            process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=TERMINATE_GRACE_SECONDS)


def _taskkill(pid: int, *, force: bool) -> None:
    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    subprocess.run(
        command,
        capture_output=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def settings_file() -> Path:
    return sessions_dir() / f"{os.getpid()}.settings.json"


def wants_hook_bus(config: Config, args: list[str]) -> bool:
    """Whether this launch is a session whose hooks are worth listening to.

    Subcommands (`claude mcp list`) fire nothing, and `--bare`/`--safe-mode`
    switch hooks off on claude's side, so a bus for them would only sit there.
    """
    if not config.hooks_bus:
        return False
    if args and args[0] in CLAUDE_SUBCOMMANDS:
        return False
    return not hookbus.hooks_disabled_by(args)


def open_hook_bus(config: Config, slot: int) -> hookbus.HookBus:
    bus = hookbus.HookBus(hookbus.parse_handlers(config.hooks), slot=slot)
    bus.start()
    update_session(port=bus.port)
    return bus


def close_hook_bus(bus: hookbus.HookBus | None) -> None:
    if bus is None:
        return
    bus.stop()
    with contextlib.suppress(OSError):
        settings_file().unlink()


def launch(
    config: Config,
    slot: int,
    args: list[str],
    *,
    on_started: Callable[[subprocess.Popen[bytes]], None] | None = None,
    bus: hookbus.HookBus | None = None,
) -> int:
    executable = config.real_claude_path
    if not executable or not Path(executable).exists():
        raise WrapperError(t("error.real_claude_missing"))

    # A bus handed in by the caller outlives this launch (the supervisor keeps
    # one across a switch); one opened here is closed here.
    own_bus: hookbus.HookBus | None = None
    if bus is None and wants_hook_bus(config, args):
        bus = own_bus = open_hook_bus(config, slot)
    if bus is not None:
        args = hookbus.prepare_args(bus, args, settings_file())

    command = [executable, *merge_default_args(config.default_args, args)]
    env = build_environment(config, slot)
    log.write(f"launch slot={slot} mode={config.cred_mode} args={args}")

    # Taken before claude touches the console and put back after it is gone,
    # on this thread: the reaper that kills claude runs while we are still in
    # wait(), and restoring from there would race the child for the console.
    console_state = console.snapshot()
    try:
        process = subprocess.Popen(command, env=env)
    except OSError as exc:
        close_hook_bus(own_bus)
        raise WrapperError(t("error.launch_failed", error=exc)) from exc

    if on_started is not None:
        on_started(process)

    try:
        while True:
            try:
                return process.wait()
            except KeyboardInterrupt:
                # Ctrl-C reached claude too and it handles its own shutdown.
                # Waiting again is what keeps us from returning while the
                # child still owns the terminal.
                continue
    finally:
        close_hook_bus(own_bus)
        console.restore(console_state)
        console.sanitize()


@contextlib.contextmanager
def slot_session(config: Config, slot_number: int, **record: Any) -> Iterator[bool]:
    """The per-slot bookkeeping around one claude, whoever drives it.

    Yields whether this is a fresh login. `record` goes into the session
    file next to the pid and slot -- the transport names itself there so
    `ccas` can list what is running where.
    """
    ensure_slot_dir(slot_number)
    launched_at = time.time()
    copy_mode = config.cred_mode == CRED_MODE_COPY

    if copy_mode:
        held = read_lock()
        held_slot = int(held.get("slot", 0)) if held else 0
        if held_slot and held_slot != slot_number:
            raise WrapperError(t("wrapper.concurrent", held=held_slot, wanted=slot_number))

    state = {"fresh_login": False}

    def _prepare(fresh: Accounts) -> None:
        slot = fresh.ensure(slot_number)
        state["fresh_login"] = not slot.has_credentials()

        if copy_mode:
            previous = fresh.get(fresh.active)
            if previous is not None and previous.number != slot_number:
                copy_mode_save(previous)

        if state["fresh_login"]:
            claudecfg.clear_identity()
        else:
            if copy_mode:
                copy_mode_restore(slot_number)
            claudecfg.patch_identity(stored_identity(slot_number))

        fresh.active = slot_number
        slot.last_used_at = launched_at

    update_accounts(_prepare)

    if copy_mode:
        acquire_lock(slot_number)

    register_session(slot_number, **record)
    try:
        yield state["fresh_login"]
    finally:
        try:
            unregister_session()

            def _bookkeep(fresh: Accounts) -> None:
                slot = fresh.ensure(slot_number)
                if copy_mode:
                    copy_mode_save(slot)
                identity = capture_identity(slot, fresh, launched_at)
                capture_usage(slot, identity)

            update_accounts(_bookkeep)
            if copy_mode:
                release_lock()
        except Exception as exc:
            log.write(f"post-run bookkeeping failed: {exc!r}")


def run_once(
    config: Config,
    slot_number: int,
    args: list[str],
    *,
    on_started: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> int:
    """One claude, start to finish, with the per-slot bookkeeping around it."""
    with slot_session(config, slot_number) as fresh_login:
        if fresh_login:
            _print_login_hint(slot_number)
        return launch(config, slot_number, args, on_started=on_started)


def run_slot(config: Config, slot_number: int, args: list[str]) -> int:
    if slot_number <= 0:
        slot_number = 1

    plan = autoswitch.plan_session(config, args, interactive=_interactive())
    if not plan.supervise:
        return run_once(config, slot_number, args)

    return _supervise(config, slot_number, plan)


def _print_login_hint(slot: int) -> None:
    message = t("wrapper.login_needed", slot=slot)
    if sys.stderr and sys.stderr.isatty():
        sys.stderr.write(f"\033[33m{message}\033[0m\n")
    else:
        sys.stderr.write(f"{message}\n")


def _interactive() -> bool:
    return bool(
        sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty()
    )


def _number(source: dict[str, Any], key: str, default: float) -> float:
    """Read a numeric setting without letting a legitimate 0 fall back.

    `value or default` reads naturally and is wrong here: "never switch"
    (maxSwitches 0) and "no cooldown" (minIntervalSeconds 0) are settings a
    user can pick, and they are exactly the values `or` throws away.
    """
    value = source.get(key, default)
    return float(value) if isinstance(value, (int, float)) else default


# How long after the announced reset to actually try: the moment the API names
# is when the window rolls over server-side, and a request that lands on the
# exact second has been seen to draw one more 429.
RESET_MARGIN_SECONDS = 30.0

# A declined switch does not end the watch. This is the least we wait before
# taking the next wall seriously, so that the burst of 429 retries claude makes
# on its own is not re-examined line by line.
RETRY_FLOOR_SECONDS = 30.0


def _supervise(config: Config, slot_number: int, plan: autoswitch.SessionPlan) -> int:
    """Run claude, and when it runs out of quota, run it again elsewhere.

    The switch is driven by a reaper thread rather than by the exit code
    because claude does not exit when it hits the wall -- it sits at the prompt
    refusing to work. Waiting for the user to quit would defeat the point;
    killing it before the wall would interrupt work that was still going.
    """
    auto = config.auto_switch
    threshold = _number(auto, "threshold", 95.0)
    max_switches = int(_number(auto, "maxSwitches", 3))
    min_interval = _number(auto, "minIntervalSeconds", 60.0)

    args = list(plan.args)
    session_id = plan.session_id
    tried: set[int] = set()
    switches = 0
    last_switch = 0.0

    try:
        while True:
            tried.add(slot_number)
            signal = autoswitch.LimitSignal()
            decision: dict[str, Any] = {}
            allowed = switches < max_switches
            # Evaluated when the wall is hit, not now: this session may run for
            # hours, and a cooldown measured from its launch would have expired
            # long before it ever mattered.
            not_before = last_switch + min_interval if last_switch else 0.0

            watcher = autoswitch.TranscriptWatcher(
                session_id, since=time.time(), signal=signal
            )
            watcher.start()

            def _on_started(
                process: subprocess.Popen[bytes],
                _signal: autoswitch.LimitSignal = signal,
                _decision: dict[str, Any] = decision,
                _slot: int = slot_number,
                _tried: set[int] = set(tried),
                _allowed: bool = allowed,
                _not_before: float = not_before,
            ) -> None:
                reaper = threading.Thread(
                    target=_reap,
                    args=(process, _signal, _decision, config, _slot, _tried, threshold),
                    kwargs={
                        "may_kill": _allowed,
                        "not_before": _not_before,
                        "session_id": session_id,
                        "retry_after": max(min_interval, RETRY_FLOOR_SECONDS),
                    },
                    daemon=True,
                )
                reaper.start()

            try:
                code = run_once(config, slot_number, args, on_started=_on_started)
            finally:
                watcher.stop()

            # _reap only leaves a target behind when it actually killed the child.
            target = decision.get("target")
            if not target:
                if signal.fired and not allowed and switches:
                    notify.notice(t("autoswitch.gave_up", count=switches), live=False)
                return code

            queued = ""
            wait_until = float(decision.get("wait_until") or 0.0)
            if wait_until > time.time():
                label = Accounts.load().ensure(int(target)).label
                waited, queued = _wait_for_reset(
                    wait_until, slot=int(target), label=label
                )
                if not waited:
                    return code

            mode = str(decision.get("mode") or "")
            args = autoswitch.relaunch_args(
                args,
                session_id=session_id,
                mode=mode,
                config=config,
                resume_prompt=queued or None,
            )
            slot_number = int(target)
            switches += 1
            last_switch = time.time()
            notify.notice(t("autoswitch.resumed", slot=slot_number), live=False)
    finally:
        notify.clear_title()


CANCEL_KEYS = frozenset({"\x03", "\x1b"})
ENTER_KEYS = frozenset({"\r", "\n"})
BACKSPACE_KEYS = frozenset({"\x08", "\x7f"})


def _wait_for_reset(moment: float, *, slot: int, label: str) -> tuple[bool, str]:
    """Sit out the time until `slot` opens again.

    Returns (waited, message): `waited` is False when the user gave up, and
    `message` is whatever they typed and confirmed with Enter meanwhile -- it
    goes to the resumed session as its first prompt, the way claude's own
    "continuing automatically" banner queues input while it waits.

    claude has already been stopped by now, so the terminal is ours: one line
    says what is happening and the title carries the countdown. The keyboard
    is polled directly rather than relying on Ctrl-C alone, because the very
    thing that put us here -- a killed claude -- may have left the console in
    a state where Ctrl-C is a keystroke and not a signal.
    """
    resume_at = moment + RESET_MARGIN_SECONDS
    minutes = max(1, int((resume_at - time.time()) / 60) + 1)
    notify.notice(
        t(
            "autoswitch.waiting",
            slot=slot,
            label=label,
            opens=autoswitch.stamp(moment),
            minutes=minutes,
        ),
        live=False,
    )
    notify.notice(t("autoswitch.wait_keys"), live=False)

    typed = ""
    queued = ""
    try:
        while True:
            remaining = resume_at - time.time()
            if remaining <= 0:
                _draw_input("")
                return True, queued
            notify.title(
                t("autoswitch.waiting_title", slot=slot, minutes=int(remaining / 60) + 1)
            )

            key = console.poll_key(min(1.0, remaining))
            if not key:
                continue
            if key in CANCEL_KEYS:
                if key == "\x1b" and typed:
                    # Escape while composing drops the line, not the wait.
                    typed = ""
                    _draw_input(typed)
                    continue
                raise KeyboardInterrupt
            if key == "q" and not typed:
                raise KeyboardInterrupt
            if key in ENTER_KEYS:
                if typed.strip():
                    queued = typed.strip()
                    _draw_input("")
                    notify.notice(t("autoswitch.wait_queued", text=queued), live=False)
                typed = ""
                continue
            if key in BACKSPACE_KEYS:
                typed = typed[:-1]
                _draw_input(typed)
                continue
            if key.isprintable():
                typed += key
                _draw_input(typed)
    except KeyboardInterrupt:
        _draw_input("")
        notify.notice(t("autoswitch.wait_cancelled"), live=False)
        return False, ""


def _draw_input(typed: str) -> None:
    """Redraw the line being composed under the wait notice; empty clears it."""
    stream = sys.stderr
    if stream is None or not stream.isatty():
        return
    with contextlib.suppress(OSError, ValueError):
        stream.write(f"\r\033[K{'> ' + typed if typed else ''}")
        stream.flush()


def _reap(
    process: subprocess.Popen[bytes],
    signal: autoswitch.LimitSignal,
    decision: dict[str, Any],
    config: Config,
    slot_number: int,
    tried: set[int],
    threshold: float,
    *,
    may_kill: bool,
    not_before: float = 0.0,
    session_id: str | None = None,
    retry_after: float = RETRY_FLOOR_SECONDS,
) -> None:
    """Watch for the wall, decide, and if the decision is to stay, keep watching.

    One wall used to be the end of it: a switch declined for any reason left
    the signal set, and the second wall of the evening went unanswered. Now a
    decline re-arms the signal after a pause, so the next 429 gets the same
    consideration the first one did.
    """
    while True:
        while not signal.wait(0.5):
            if process.poll() is not None:
                return

        if _decide(
            process,
            signal,
            decision,
            config,
            slot_number,
            tried,
            threshold,
            may_kill=may_kill,
            not_before=not_before,
            session_id=session_id,
        ):
            return

        log.write(f"autoswitch: staying on slot {slot_number}, watching for the next wall")
        deadline = time.time() + retry_after
        while time.time() < deadline:
            if process.poll() is not None:
                return
            time.sleep(0.5)
        signal.rearm()


def _decide(
    process: subprocess.Popen[bytes],
    signal: autoswitch.LimitSignal,
    decision: dict[str, Any],
    config: Config,
    slot_number: int,
    tried: set[int],
    threshold: float,
    *,
    may_kill: bool,
    not_before: float,
    session_id: str | None,
) -> bool:
    """One wall, one verdict. True when claude was told to stop.

    Every way of declining is written to the journal by name: while claude owns
    the screen there is nowhere else for the reason to go, and a switch that
    silently does not happen is indistinguishable from one that was never
    attempted.
    """
    window = signal.window
    notify.notice(
        t(
            "autoswitch.limit_hit",
            slot=slot_number,
            window=t(f"usage.window_{window}") if window else t("usage.window_unknown"),
        ),
        live=True,
    )

    if config.cred_mode == CRED_MODE_COPY and other_live_sessions():
        # In copy mode the credentials live in one shared file; moving this
        # session would silently move the neighbour's too.
        notify.notice(t("autoswitch.copy_mode_busy"), live=True)
        return False

    accounts = Accounts.load()
    slot = accounts.ensure(slot_number)

    confirmed_here = False
    if config.auto_switch.get("confirmWithApi", True):
        notify.notice(t("autoswitch.confirming"), live=True)
        confirmed = autoswitch.confirm_exhausted(slot, threshold=threshold)
        if confirmed is None:
            notify.notice(t("autoswitch.unconfirmed"), live=True)
            return False
        if not confirmed:
            notify.notice(t("autoswitch.not_exhausted"), live=True)
            return False
        _store_usage({slot_number: slot.usage})
        confirmed_here = True

    _refresh_candidates(accounts, skip={slot_number} if confirmed_here else set())

    election = autoswitch.elect_target(
        accounts,
        current=slot_number,
        tried=tried,
        threshold=threshold,
        strategy=str(config.auto_switch.get("strategy") or "limits"),
        current_reset=signal.resets_at,
    )
    if election.target is None:
        notify.notice(t("autoswitch.no_target", slot=slot_number), live=True)
        return False

    target_label = accounts.ensure(election.target).label

    if election.must_wait():
        max_wait = _number(config.auto_switch, "maxWaitSeconds", 7200.0)
        wait = election.available_at - time.time()
        if max_wait <= 0 or wait > max_wait:
            notify.notice(
                t(
                    "autoswitch.wait_too_long",
                    slot=election.target,
                    label=target_label,
                    opens=autoswitch.stamp(election.available_at),
                    minutes=int(wait / 60) + 1,
                    limit=int(max_wait / 60),
                ),
                live=True,
            )
            return False

    if process.poll() is not None:
        # The user quit while we were checking. Relaunching now would reopen a
        # session they just closed on purpose.
        log.write("autoswitch: session ended before the switch, standing down")
        return False

    strategy = config.auto_switch.get("strategy")
    too_soon = not_before and time.time() < not_before
    if strategy == "notify" or not may_kill or too_soon:
        hint = autoswitch.relaunch_args(
            [], session_id=session_id, mode=signal.permission_mode, config=config
        )
        if strategy == "notify":
            why = "notify strategy"
        elif not may_kill:
            why = "switch budget spent"
        else:
            why = "cooldown"
        log.write(f"autoswitch: not switching ({why})")
        notify.notice(
            t(
                "autoswitch.notify_command",
                command=f"claude {election.target} {' '.join(hint)}".strip(),
            ),
            live=True,
        )
        return False

    decision["target"] = election.target
    decision["mode"] = signal.permission_mode
    if election.must_wait():
        decision["wait_until"] = election.available_at
        notify.notice(
            t(
                "autoswitch.switching_later",
                from_slot=slot_number,
                to_slot=election.target,
                label=target_label,
                opens=autoswitch.stamp(election.available_at),
            ),
            live=True,
        )
    else:
        notify.notice(
            t(
                "autoswitch.switching",
                from_slot=slot_number,
                to_slot=election.target,
                label=target_label,
                mode=signal.permission_mode or t("common.none"),
            ),
            live=True,
        )
    terminate(process)
    return True


def _store_usage(payloads: dict[int, dict[str, Any]]) -> None:
    if not payloads:
        return

    def _write(current: Accounts) -> None:
        for number, payload in payloads.items():
            if payload:
                current.ensure(number).usage = payload

    update_accounts(_write)


def _refresh_candidates(accounts: Accounts, *, skip: set[int]) -> None:
    """Rank on numbers fetched now, not on whatever was lying in the store.

    This used to skip any slot whose snapshot was younger than six hours, and a
    5-hour window empties and refills inside that. The store would say every
    account was spent while one of them had been free for hours, and the
    session stayed put. A handful of requests once per wall is cheap; a wrong
    "no target" costs the rest of the evening.

    Every slot that could ever be waited for is refreshed, the current and the
    already-tried ones included: when nobody is free the election falls back
    to "who opens first", and that answer was being read off snapshots nobody
    had touched since the slot was left. `skip` names slots fetched moments
    ago by the caller.
    """
    from ui import usage as usage_module

    wanted = [
        slot for slot in autoswitch.waitable(accounts) if slot.number not in skip
    ]
    if not wanted:
        return

    fresh = usage_module.refresh_slots(
        wanted, refresh_timeout=usage_module.INTERACTIVE_REFRESH_TIMEOUT
    )
    for number, payload in fresh.items():
        accounts.ensure(number).usage = payload
    _store_usage(fresh)
    missed = sorted(slot.number for slot in wanted if slot.number not in fresh)
    if missed:
        log.write(f"autoswitch: no fresh usage for slots {missed}, ranking on stored data")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    args = argv[1:]

    # A claude killed in this window earlier may have left the console in raw
    # mode; the session about to start would inherit it.
    if console.repair():
        log.write("console repaired at startup")

    if not is_installed():
        sys.stderr.write(t("error.not_installed_wrapper") + "\n")
        return 2

    config = Config.load()
    accounts = Accounts.load()

    if config.telegram.get("daemon"):
        # Imported only when wanted: the transport modules are a cost every
        # plain `claude` launch need not pay.
        from app import daemon

        daemon.ensure_running(config)

    try:
        slot_number, rest, explicit = resolve_slot(args, accounts)
    except WrapperError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    del explicit

    if adopt_existing_login(config):
        accounts = Accounts.load()

    if slot_number is None:
        slot_number = default_slot(accounts)

    from app.transport.routing import split_launch_options

    options, rest = split_launch_options(rest)
    if options.wants_transport:
        from app import transport

        try:
            return transport.run(config, slot_number, rest, options)
        except (WrapperError, transport.TransportError) as exc:
            sys.stderr.write(f"{exc}\n")
            return 2

    try:
        return run_slot(config, slot_number, rest)
    except WrapperError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
