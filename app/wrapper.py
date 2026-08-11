from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app import autoswitch
from core import claudecfg, log
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


def launch(
    config: Config,
    slot: int,
    args: list[str],
    *,
    on_started: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> int:
    executable = config.real_claude_path
    if not executable or not Path(executable).exists():
        raise WrapperError(t("error.real_claude_missing"))

    command = [executable, *merge_default_args(config.default_args, args)]
    env = build_environment(config, slot)
    log.write(f"launch slot={slot} mode={config.cred_mode} args={args}")

    try:
        process = subprocess.Popen(command, env=env)
    except OSError as exc:
        raise WrapperError(t("error.launch_failed", error=exc)) from exc

    if on_started is not None:
        on_started(process)

    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            # Ctrl-C reached claude too and it handles its own shutdown. Waiting
            # again is what keeps us from returning while the child still owns
            # the terminal.
            continue


def run_once(
    config: Config,
    slot_number: int,
    args: list[str],
    *,
    on_started: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> int:
    """One claude, start to finish, with the per-slot bookkeeping around it."""
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

    if state["fresh_login"]:
        _print_login_hint(slot_number)

    register_session(slot_number)
    try:
        return launch(config, slot_number, args, on_started=on_started)
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


def _notice(message: str) -> None:
    if sys.stderr and sys.stderr.isatty():
        sys.stderr.write(f"\033[33mccas: {message}\033[0m\n")
    else:
        sys.stderr.write(f"ccas: {message}\n")
    sys.stderr.flush()


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
                _notice(t("autoswitch.gave_up", count=switches))
            return code

        mode = str(decision.get("mode") or "")
        args = autoswitch.relaunch_args(
            args, session_id=session_id, mode=mode, config=config
        )
        slot_number = int(target)
        switches += 1
        last_switch = time.time()
        _notice(t("autoswitch.resumed", slot=slot_number))


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
) -> None:
    while not signal.wait(0.5):
        if process.poll() is not None:
            return

    window = signal.window
    _notice(
        t(
            "autoswitch.limit_hit",
            slot=slot_number,
            window=t(f"usage.window_{window}") if window else t("usage.window_unknown"),
        )
    )

    if config.cred_mode == CRED_MODE_COPY and other_live_sessions():
        # In copy mode the credentials live in one shared file; moving this
        # session would silently move the neighbour's too.
        _notice(t("autoswitch.copy_mode_busy"))
        return

    accounts = Accounts.load()
    slot = accounts.ensure(slot_number)

    if config.auto_switch.get("confirmWithApi", True):
        _notice(t("autoswitch.confirming"))
        confirmed = autoswitch.confirm_exhausted(slot, threshold=threshold)
        if confirmed is None:
            _notice(t("autoswitch.unconfirmed"))
            return
        if not confirmed:
            _notice(t("autoswitch.not_exhausted"))
            return
        _store_usage({slot_number: slot.usage})

    _refresh_candidates(accounts, current=slot_number, tried=tried)

    election = autoswitch.elect_target(
        accounts,
        current=slot_number,
        tried=tried,
        threshold=threshold,
        strategy=str(config.auto_switch.get("strategy") or "limits"),
    )
    if election.target is None:
        _notice(t("autoswitch.no_target", slot=slot_number))
        return

    if process.poll() is not None:
        # The user quit while we were checking. Relaunching now would reopen a
        # session they just closed on purpose.
        log.write("autoswitch: session ended before the switch, standing down")
        return

    decision["target"] = election.target
    decision["mode"] = signal.permission_mode

    too_soon = not_before and time.time() < not_before
    if config.auto_switch.get("strategy") == "notify" or not may_kill or too_soon:
        hint = autoswitch.relaunch_args(
            [], session_id=session_id, mode=signal.permission_mode, config=config
        )
        _notice(
            t(
                "autoswitch.notify_command",
                command=f"claude {election.target} {' '.join(hint)}".strip(),
            )
        )
        decision.pop("target", None)
        return

    target_label = accounts.ensure(election.target).label
    _notice(
        t(
            "autoswitch.switching",
            from_slot=slot_number,
            to_slot=election.target,
            label=target_label,
            mode=signal.permission_mode or t("common.none"),
        )
    )
    terminate(process)


def _store_usage(payloads: dict[int, dict[str, Any]]) -> None:
    if not payloads:
        return

    def _write(current: Accounts) -> None:
        for number, payload in payloads.items():
            if payload:
                current.ensure(number).usage = payload

    update_accounts(_write)


def _refresh_candidates(accounts: Accounts, *, current: int, tried: set[int]) -> None:
    """Make sure the ranking is done on numbers worth ranking by."""
    from ui import usage as usage_module

    pool = autoswitch.candidates(accounts, current=current, tried=tried)
    stale = [
        slot for slot in pool if not usage_module.is_usable_for_ranking(slot.usage)
    ]
    if not stale:
        return

    fresh = usage_module.refresh_slots(
        stale, refresh_timeout=usage_module.INTERACTIVE_REFRESH_TIMEOUT
    )
    for number, payload in fresh.items():
        accounts.ensure(number).usage = payload
    _store_usage(fresh)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    args = argv[1:]

    if not is_installed():
        sys.stderr.write(t("error.not_installed_wrapper") + "\n")
        return 2

    config = Config.load()
    accounts = Accounts.load()

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

    try:
        return run_slot(config, slot_number, rest)
    except WrapperError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
