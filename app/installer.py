from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

from core import claudecfg, detect, log
from core.store import (
    Accounts,
    Config,
    DEFAULT_ARGS,
    app_dir,
    bin_dir,
    creds_file,
    ensure_layout,
    ensure_slot_dir,
    identity_file,
    is_installed,
    legacy_store_dir,
    manager_name,
    read_json,
    shim_name,
    write_json_atomic,
)
from core.version import SCHEMA_VERSION, __version__
from system import secure
from ui import i18n
from ui.i18n import t

IS_WINDOWS = os.name == "nt"
PENDING_SUFFIX = ".new"

_LEGACY_CRED_RE = re.compile(r"^account-(\d+)\.credentials\.json$")
_LEGACY_IDENTITY_RE = re.compile(r"^account-(\d+)\.identity\.json$")


class InstallError(Exception):
    pass


def _pathenv():
    if IS_WINDOWS:
        from system import pathenv_win

        return pathenv_win
    from system import pathenv_posix

    return pathenv_posix


def ensure_path_entry() -> Any:
    return _pathenv().ensure_first(bin_dir())


def remove_path_entry() -> Any:
    return _pathenv().remove(bin_dir())


def path_entry_present() -> bool:
    return bool(_pathenv().is_on_path(bin_dir()))


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _write_dev_launcher(destination: Path, invoked_as: str) -> None:
    python = sys.executable
    root = _source_root()
    bootstrap = (
        f"import sys; sys.argv[0]={invoked_as!r}; "
        "from main import main; sys.exit(main())"
    )

    if IS_WINDOWS:
        content = (
            "@echo off\r\n"
            f'set "PYTHONPATH={root}"\r\n'
            f'"{python}" -c "{bootstrap}" %*\r\n'
        )
        destination.write_text(content, encoding="utf-8", newline="")
    else:
        content = (
            "#!/bin/sh\n"
            f'PYTHONPATH="{root}" exec "{python}" -c "{bootstrap}" "$@"\n'
        )
        destination.write_text(content, encoding="utf-8", newline="\n")
        destination.chmod(0o755)


def _replace_binary(source: Path, destination: Path) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, destination)
        if not IS_WINDOWS:
            destination.chmod(0o755)
        return False
    except (PermissionError, OSError):
        pending = destination.with_name(destination.name + PENDING_SUFFIX)
        shutil.copyfile(source, pending)
        if not IS_WINDOWS:
            pending.chmod(0o755)
        log.write(f"deferred replacement of {destination} via {pending}")
        return True


def apply_pending_upgrade() -> bool:
    applied = False
    directory = bin_dir()
    if not directory.is_dir():
        return False
    for pending in directory.glob(f"*{PENDING_SUFFIX}"):
        target = pending.with_name(pending.name[: -len(PENDING_SUFFIX)])
        try:
            os.replace(pending, target)
            applied = True
        except OSError:
            continue
    return applied


def install_shims() -> list[Path]:
    bin_dir().mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if is_frozen():
        source = Path(sys.executable)
        for name in (shim_name(), manager_name()):
            destination = bin_dir() / name
            _replace_binary(source, destination)
            written.append(destination)
    else:
        suffix = ".cmd" if IS_WINDOWS else ""
        for stem in ("claude", "ccas"):
            destination = bin_dir() / f"{stem}{suffix}"
            _write_dev_launcher(destination, stem)
            written.append(destination)

    return written


def migrate_legacy_store(accounts: Accounts) -> list[int]:
    legacy = legacy_store_dir()
    if not legacy.is_dir():
        return []

    imported: list[int] = []
    for entry in sorted(legacy.iterdir()):
        match = _LEGACY_CRED_RE.match(entry.name)
        if not match:
            continue
        number = int(match.group(1))
        destination = creds_file(number)
        if destination.exists():
            continue
        ensure_slot_dir(number)
        shutil.copyfile(entry, destination)
        with contextlib.suppress(secure.PermissionWarning, OSError):
            secure.harden_file(destination)
        accounts.ensure(number)
        imported.append(number)

    for entry in sorted(legacy.iterdir()):
        match = _LEGACY_IDENTITY_RE.match(entry.name)
        if not match:
            continue
        number = int(match.group(1))
        raw = read_json(entry)
        if not isinstance(raw, dict):
            continue
        if not identity_file(number).exists():
            write_json_atomic(identity_file(number), raw)
        slot = accounts.ensure(number)
        oauth = raw.get("oauthAccount")
        if isinstance(oauth, dict):
            slot.email = oauth.get("emailAddress") or slot.email
            slot.account_uuid = oauth.get("accountUuid") or slot.account_uuid
        user_id = raw.get("userID")
        if isinstance(user_id, str):
            slot.user_id = user_id

    active_marker = legacy / ".active"
    if active_marker.exists() and not accounts.active:
        with contextlib.suppress(OSError, ValueError):
            value = active_marker.read_text(encoding="utf-8", errors="replace").strip()
            if value.isdigit():
                accounts.active = int(value)

    if imported:
        log.write(f"migrated legacy slots: {imported}")
    return imported


def adopt_current_login(accounts: Accounts) -> int | None:
    shared = claudecfg.shared_credentials_path()
    if not shared.exists():
        return None

    identity = claudecfg.read_identity()
    oauth = (identity or {}).get("oauthAccount")
    current_uuid = oauth.get("accountUuid") if isinstance(oauth, dict) else None

    if current_uuid:
        if any(slot.account_uuid == current_uuid for slot in accounts.ordered()):
            return None
    elif accounts.slots:
        return None

    number = accounts.next_free_number()
    ensure_slot_dir(number)
    shutil.copyfile(shared, creds_file(number))
    with contextlib.suppress(secure.PermissionWarning, OSError):
        secure.harden_file(creds_file(number))

    slot = accounts.ensure(number)
    slot.created_at = time.time()

    if identity and identity.get("oauthAccount"):
        write_json_atomic(identity_file(number), identity)
        if isinstance(oauth, dict):
            slot.email = oauth.get("emailAddress") or ""
            slot.account_uuid = oauth.get("accountUuid") or ""
        if isinstance(identity.get("userID"), str):
            slot.user_id = identity["userID"]

    accounts.active = number
    return number


def resolve_claude(explicit: str | None = None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            raise InstallError(t("error.claude_path_invalid", path=candidate))
        return candidate

    found = detect.find_real_claude()
    if found is None:
        raise InstallError(t("error.claude_not_found"))
    return found


def install(
    *,
    claude_path: str | None = None,
    skip_permissions: bool | None = None,
    language: str | None = None,
    ask: Callable[[str, bool], bool] | None = None,
    reporter: Callable[[str], None] = print,
) -> Config:
    upgrading = is_installed()
    previous = Config.load() if upgrading else Config()

    chosen_language = i18n.set_language(language or previous.language or None)

    ensure_layout()
    apply_pending_upgrade()

    real_claude = resolve_claude(claude_path or (previous.real_claude_path or None))
    mode, probe = detect.probe_cred_mode(real_claude, previous.cred_mode_probe or None)

    if skip_permissions is None:
        if upgrading:
            default_args = list(previous.default_args)
        elif ask is not None:
            wanted = ask(t("install.ask_skip_permissions"), True)
            default_args = list(DEFAULT_ARGS) if wanted else []
        else:
            default_args = list(DEFAULT_ARGS)
    else:
        default_args = list(DEFAULT_ARGS) if skip_permissions else []

    config = Config(
        schema=SCHEMA_VERSION,
        version=__version__,
        installed_at=previous.installed_at or time.time(),
        real_claude_path=str(real_claude),
        cred_mode=mode,
        cred_mode_probe=probe,
        default_args=default_args,
        shim_dir=str(bin_dir()),
        language=chosen_language,
    )
    config.save()

    install_shims()
    ensure_path_entry()

    accounts = Accounts.load()
    imported = migrate_legacy_store(accounts)
    adopted = adopt_current_login(accounts)
    accounts.save()

    header = "install.header_upgraded" if upgrading else "install.header_installed"
    reporter(t(header, version=__version__))
    reporter(t("install.real_claude", path=real_claude))

    mode_key = (
        "install.cred_mode_env"
        if mode == detect.CRED_MODE_ENV
        else "install.cred_mode_copy"
    )
    reporter(t("install.cred_mode", mode=t(mode_key)))
    reporter(
        t(
            "install.default_args",
            args=" ".join(default_args) if default_args else t("common.none"),
        )
    )
    reporter(t("install.language", language=chosen_language))

    if imported:
        reporter(t("install.migrated", slots=", ".join(str(n) for n in imported)))
    if adopted:
        reporter(t("install.adopted", slot=adopted))

    reporter(t("install.directory", path=app_dir()))
    reporter("")
    reporter(t("install.reopen_terminal"))

    log.write(f"install complete: version={__version__} mode={mode} lang={chosen_language}")
    return config


def uninstall(*, purge: bool = False, reporter: Callable[[str], None] = print) -> None:
    remove_path_entry()

    for name in (shim_name(), manager_name(), "claude", "claude.cmd", "ccas", "ccas.cmd"):
        candidate = bin_dir() / name
        with contextlib.suppress(OSError):
            if candidate.exists():
                candidate.unlink()

    if purge:
        shutil.rmtree(app_dir(), ignore_errors=True)
        reporter(t("uninstall.purged"))
    else:
        with contextlib.suppress(OSError):
            bin_dir().rmdir()
        reporter(t("uninstall.kept"))
        reporter(f"  {app_dir()}")
        reporter(t("uninstall.purge_hint"))

    reporter(t("uninstall.reopen_terminal"))
    log.write(f"uninstall complete purge={purge}")


def refresh_claude_path(config: Config) -> bool:
    found = detect.find_real_claude()
    if found is None or str(found) == config.real_claude_path:
        return False

    config.real_claude_path = str(found)
    mode, probe = detect.probe_cred_mode(found, None)
    config.cred_mode = mode
    config.cred_mode_probe = probe
    config.save()
    log.write(f"real claude path refreshed to {found}")
    return True
