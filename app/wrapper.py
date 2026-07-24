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


def wants_menu(args: list[str], explicit_slot: bool) -> bool:
    if args or explicit_slot:
        return False
    return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())


def adopt_existing_login(accounts: Accounts, config: Config) -> bool:
    if config.cred_mode != CRED_MODE_COPY:
        return False

    from app.installer import adopt_current_login

    adopted = adopt_current_login(accounts)
    if adopted is None:
        return False

    accounts.save()
    log.write(f"adopted existing login as slot {adopted}")
    return True


def capture_identity(slot: Slot) -> dict[str, Any] | None:
    identity = claudecfg.read_identity()
    if not identity or not identity.get("oauthAccount"):
        return None

    write_json_atomic(identity_file(slot.number), identity)

    oauth = identity.get("oauthAccount") or {}
    if isinstance(oauth, dict):
        slot.email = oauth.get("emailAddress") or slot.email
        slot.account_uuid = oauth.get("accountUuid") or slot.account_uuid

    user_id = identity.get("userID")
    if isinstance(user_id, str):
        slot.user_id = user_id

    return identity


def stored_identity(slot: int) -> dict[str, Any] | None:
    raw = read_json(identity_file(slot))
    return raw if isinstance(raw, dict) else None


def capture_usage(slot: Slot) -> None:
    cached = claudecfg.read_usage_cache()
    if not cached:
        return

    account_uuid = cached.get("accountUuid")
    if slot.account_uuid and account_uuid and account_uuid != slot.account_uuid:
        return

    utilization = cached.get("utilization")
    if not isinstance(utilization, dict):
        return

    slot.usage = {
        "fetchedAtMs": cached.get("fetchedAtMs") or time.time() * 1000,
        "five_hour": utilization.get("five_hour"),
        "seven_day": utilization.get("seven_day"),
    }


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


def run_slot(config: Config, accounts: Accounts, slot_number: int, args: list[str]) -> int:
    if slot_number <= 0:
        slot_number = 1

    slot = accounts.ensure(slot_number)
    ensure_slot_dir(slot_number)

    fresh_login = not slot.has_credentials()
    previous_active = accounts.active

    if config.cred_mode == CRED_MODE_COPY:
        held = read_lock()
        if held and int(held.get("slot", 0)) != slot_number:
            _warn_concurrent(int(held.get("slot", 0)), slot_number)
        if previous_active and previous_active != slot_number:
            previous = accounts.get(previous_active)
            if previous is not None:
                copy_mode_save(previous)
        if fresh_login:
            claudecfg.clear_identity()
        else:
            copy_mode_restore(slot_number)
            claudecfg.patch_identity(stored_identity(slot_number))
        acquire_lock(slot_number)
    else:
        if fresh_login:
            claudecfg.clear_identity()
        else:
            claudecfg.patch_identity(stored_identity(slot_number))

    if fresh_login:
        _print_login_hint(slot_number)

    accounts.active = slot_number
    slot.last_used_at = time.time()
    accounts.save()

    try:
        return launch(config, slot_number, args)
    finally:
        try:
            if config.cred_mode == CRED_MODE_COPY:
                copy_mode_save(slot)
                release_lock()
            capture_identity(slot)
            capture_usage(slot)
            accounts.save()
        except Exception as exc:
            log.write(f"post-run bookkeeping failed: {exc!r}")


def _print_login_hint(slot: int) -> None:
    message = t("wrapper.login_needed", slot=slot)
    if sys.stderr and sys.stderr.isatty():
        sys.stderr.write(f"\033[33m{message}\033[0m\n")
    else:
        sys.stderr.write(f"{message}\n")


def _warn_concurrent(held_slot: int, wanted_slot: int) -> None:
    sys.stderr.write(t("wrapper.concurrent", held=held_slot, wanted=wanted_slot) + "\n")
    log.write(f"concurrent launch: held={held_slot} wanted={wanted_slot}")


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

    adopt_existing_login(accounts, config)

    if slot_number is None and wants_menu(rest, explicit):
        from ui import menu

        chosen = menu.choose(config, accounts)
        if chosen is None:
            return 0
        slot_number = chosen

    if slot_number is None:
        slot_number = accounts.active or 1

    try:
        return run_slot(config, accounts, slot_number, rest)
    except WrapperError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
