from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from core import claudecfg, log
from core.detect import CRED_MODE_COPY, CRED_MODE_ENV
from core.store import (
    Accounts,
    Config,
    Slot,
    app_dir,
    creds_file,
    ensure_slot_dir,
    identity_file,
    is_installed,
    read_json,
    update_accounts,
    write_json_atomic,
)
from ui.i18n import t

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

LOCK_STALE_SECONDS = 12 * 60 * 60


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


def sessions_dir() -> Path:
    return app_dir() / "sessions"


def register_session(slot: int) -> None:
    write_json_atomic(
        sessions_dir() / f"{os.getpid()}.json",
        {"pid": os.getpid(), "slot": slot, "at": time.time()},
        harden=False,
    )


def unregister_session() -> None:
    with contextlib.suppress(OSError):
        (sessions_dir() / f"{os.getpid()}.json").unlink()


def other_live_sessions() -> list[dict[str, Any]]:
    """Wrapper processes other than this one that are still running.

    Tells apart the two reasons the shared config can name an unfamiliar
    account: a deliberate re-login on this slot (nobody else is running) versus
    a neighbouring terminal having just written its own (someone is).
    """
    directory = sessions_dir()
    if not directory.is_dir():
        return []

    mine = os.getpid()
    live: list[dict[str, Any]] = []
    for entry in directory.glob("*.json"):
        raw = read_json(entry)
        if not isinstance(raw, dict):
            continue
        pid = int(raw.get("pid", 0) or 0)
        if pid == mine:
            continue
        stale = time.time() - float(raw.get("at", 0) or 0) > LOCK_STALE_SECONDS
        if stale or not _pid_alive(pid):
            with contextlib.suppress(OSError):
                entry.unlink()
            continue
        live.append(raw)
    return live


def _lock_path() -> Path:
    return app_dir() / ".lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock() -> dict[str, Any] | None:
    raw = read_json(_lock_path())
    if not isinstance(raw, dict):
        return None
    if time.time() - float(raw.get("at", 0)) > LOCK_STALE_SECONDS:
        return None
    if not _pid_alive(int(raw.get("pid", 0) or 0)):
        return None
    return raw


def acquire_lock(slot: int) -> None:
    write_json_atomic(_lock_path(), {"pid": os.getpid(), "slot": slot, "at": time.time()})


def release_lock() -> None:
    raw = read_json(_lock_path())
    if isinstance(raw, dict) and int(raw.get("pid", 0) or 0) != os.getpid():
        return
    with contextlib.suppress(OSError):
        _lock_path().unlink()


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


def launch(config: Config, slot: int, args: list[str]) -> int:
    executable = config.real_claude_path
    if not executable or not Path(executable).exists():
        raise WrapperError(t("error.real_claude_missing"))

    command = [executable, *merge_default_args(config.default_args, args)]
    env = build_environment(config, slot)
    log.write(f"launch slot={slot} mode={config.cred_mode} args={args}")

    try:
        completed = subprocess.run(command, env=env)
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        raise WrapperError(t("error.launch_failed", error=exc)) from exc

    return completed.returncode


def run_slot(config: Config, slot_number: int, args: list[str]) -> int:
    if slot_number <= 0:
        slot_number = 1

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
        return launch(config, slot_number, args)
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


def _print_login_hint(slot: int) -> None:
    message = t("wrapper.login_needed", slot=slot)
    if sys.stderr and sys.stderr.isatty():
        sys.stderr.write(f"\033[33m{message}\033[0m\n")
    else:
        sys.stderr.write(f"{message}\n")


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
