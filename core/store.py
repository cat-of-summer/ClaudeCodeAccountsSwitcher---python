from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.version import SCHEMA_VERSION, __version__
from system import secure

APP_DIR_NAME = ".claude-switcher"
LEGACY_STORE_NAME = ".claude-accounts"
CREDENTIALS_FILENAME = ".credentials.json"
BACKUP_KEEP = 10

LOCK_TIMEOUT = 10.0
LOCK_POLL = 0.05

DEFAULT_ARGS = ["--dangerously-skip-permissions"]

# Off by default on purpose: this changes how `claude` is launched -- it can
# stop and restart a live session -- and that deserves an explicit yes.
DEFAULT_AUTO_SWITCH: dict[str, Any] = {
    "enabled": False,
    "strategy": "limits",
    "threshold": 95,
    "maxSwitches": 3,
    "minIntervalSeconds": 60,
    "maxWaitSeconds": 7200,
    "confirmWithApi": True,
    "restoreMode": True,
    "resumePrompt": "",
}

AUTO_SWITCH_STRATEGIES = ("limits", "order", "notify")


def home() -> Path:
    return Path.home()


def app_dir() -> Path:
    override = os.environ.get("CCAS_HOME")
    if override:
        return Path(override)
    return home() / APP_DIR_NAME


def legacy_store_dir() -> Path:
    return home() / LEGACY_STORE_NAME


def bin_dir() -> Path:
    return app_dir() / "bin"


def creds_dir(slot: int) -> Path:
    return app_dir() / "creds" / str(slot)


def creds_file(slot: int) -> Path:
    return creds_dir(slot) / CREDENTIALS_FILENAME


def identity_file(slot: int) -> Path:
    return app_dir() / "identity" / f"{slot}.json"


def backups_dir() -> Path:
    return app_dir() / "backups"


def log_file() -> Path:
    return app_dir() / "logs" / "ccas.log"


def config_path() -> Path:
    return app_dir() / "config.json"


def accounts_path() -> Path:
    return app_dir() / "accounts.json"


def accounts_lock_path() -> Path:
    return app_dir() / "accounts.lock"


def shim_name() -> str:
    return "claude.exe" if os.name == "nt" else "claude"


def manager_name() -> str:
    return "ccas.exe" if os.name == "nt" else "ccas"


def read_json(path: str | os.PathLike[str]) -> Any:
    target = Path(path)
    if not target.exists():
        return None
    with target.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_json_atomic(
    path: str | os.PathLike[str], obj: Any, *, harden: bool = True
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(obj, indent=2, ensure_ascii=False)

    handle_fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f"{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise

    if harden:
        with contextlib.suppress(secure.PermissionWarning, OSError):
            secure.harden_file(target)


def _lock_exclusive(handle: int) -> None:
    """Take an exclusive lock on the first byte, or raise OSError if held."""
    if os.name == "nt":
        import msvcrt

        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(
    path: str | os.PathLike[str], *, timeout: float = LOCK_TIMEOUT
) -> Iterator[bool]:
    """Serialise writers across processes.

    Yields True when the lock was taken. On timeout it yields False and lets the
    caller proceed anyway: a contended store is worth a rare lost update, but a
    claude session that refuses to start is not.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(str(target), os.O_RDWR | os.O_CREAT, 0o600)

    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                _lock_exclusive(handle)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    from core import log

                    log.write(f"lock timeout on {target.name}, proceeding unlocked")
                    break
                time.sleep(LOCK_POLL)
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                _unlock(handle)
        with contextlib.suppress(OSError):
            os.close(handle)


def backup_file(path: str | os.PathLike[str], *, keep: int = BACKUP_KEEP) -> Path | None:
    source = Path(path)
    if not source.exists():
        return None

    destination_dir = backups_dir()
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = destination_dir / f"{source.name}.{stamp}"
    destination.write_bytes(source.read_bytes())

    generations = sorted(destination_dir.glob(f"{source.name}.*"))
    for stale in generations[:-keep]:
        with contextlib.suppress(OSError):
            stale.unlink()
    return destination


@dataclass
class Config:
    schema: int = SCHEMA_VERSION
    version: str = __version__
    installed_at: float = 0.0
    real_claude_path: str = ""
    cred_mode: str = "env"
    cred_mode_probe: dict[str, Any] = field(default_factory=dict)
    default_args: list[str] = field(default_factory=lambda: list(DEFAULT_ARGS))
    shim_dir: str = ""
    language: str = ""
    auto_switch: dict[str, Any] = field(
        default_factory=lambda: dict(DEFAULT_AUTO_SWITCH)
    )

    @classmethod
    def load(cls) -> "Config":
        raw = read_json(config_path())
        if not isinstance(raw, dict):
            return cls()
        stored = raw.get("autoSwitch")
        # An explicitly empty list means "add nothing", and must not be
        # confused with an absent key: `[] or DEFAULT_ARGS` used to hand the
        # bypass flag back to a user who had just turned it off.
        stored_args = raw.get("defaultArgs")
        return cls(
            schema=raw.get("schema", SCHEMA_VERSION),
            version=raw.get("version", __version__),
            installed_at=raw.get("installedAt", 0.0),
            real_claude_path=raw.get("realClaudePath", ""),
            cred_mode=raw.get("credMode", "env"),
            cred_mode_probe=raw.get("credModeProbe") or {},
            default_args=(
                list(stored_args)
                if isinstance(stored_args, list)
                else list(DEFAULT_ARGS)
            ),
            shim_dir=raw.get("shimDir", ""),
            language=raw.get("language", ""),
            # Merged rather than taken as-is: a config written by an older
            # build is missing whatever key the newer one added, and reading
            # that key would be a KeyError at the worst possible moment.
            auto_switch={
                **DEFAULT_AUTO_SWITCH,
                **(stored if isinstance(stored, dict) else {}),
            },
        )

    def save(self) -> None:
        write_json_atomic(
            config_path(),
            {
                "schema": self.schema,
                "version": self.version,
                "installedAt": self.installed_at,
                "realClaudePath": self.real_claude_path,
                "credMode": self.cred_mode,
                "credModeProbe": self.cred_mode_probe,
                "defaultArgs": self.default_args,
                "shimDir": self.shim_dir,
                "language": self.language,
                "autoSwitch": self.auto_switch,
            },
        )


def is_installed() -> bool:
    return config_path().exists()


def default_resume_prompt() -> str:
    """What to say to the resumed session so the work actually continues.

    Lives in the language catalog rather than in this dict: it is a sentence
    addressed to claude, and the user reads it in `ccas config`.
    """
    from ui.i18n import t

    return t("autoswitch.default_resume_prompt")


def migrate_config() -> bool:
    """Bring an older config.json up to the current schema.

    Schema 3 fills in `autoSwitch.resumePrompt`, which shipped empty and so let
    every switch land in a resumed session that then sat there waiting to be
    told to carry on. An empty value still means "say nothing" once it has been
    set deliberately -- this only reaches configs written before the key had a
    meaningful default.
    """
    if not is_installed():
        return False

    config = Config.load()
    if config.schema >= SCHEMA_VERSION:
        return False

    backup_file(config_path())

    if not str(config.auto_switch.get("resumePrompt") or "").strip():
        merged = dict(config.auto_switch)
        merged["resumePrompt"] = default_resume_prompt()
        config.auto_switch = merged

    config.schema = SCHEMA_VERSION
    config.save()
    from core import log

    log.write(f"config migrated to schema {SCHEMA_VERSION}")
    return True


@dataclass
class Slot:
    number: int
    alias: str = ""
    email: str = ""
    account_uuid: str = ""
    user_id: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    token: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    last_used_at: float = 0.0

    @property
    def label(self) -> str:
        from ui.i18n import t

        if self.alias and self.email:
            return f"{self.alias} ({self.email})"
        return self.alias or self.email or t("menu.slot_fallback_label", slot=self.number)

    def has_credentials(self) -> bool:
        return creds_file(self.number).exists()

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "email": self.email,
            "accountUuid": self.account_uuid,
            "userID": self.user_id,
            "usage": self.usage,
            "token": self.token,
            "createdAt": self.created_at,
            "lastUsedAt": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, number: int, raw: dict[str, Any]) -> "Slot":
        return cls(
            number=number,
            alias=raw.get("alias", "") or "",
            email=raw.get("email", "") or "",
            account_uuid=raw.get("accountUuid", "") or "",
            user_id=raw.get("userID", "") or "",
            usage=raw.get("usage") or {},
            token=raw.get("token") or {},
            created_at=raw.get("createdAt", 0.0) or 0.0,
            last_used_at=raw.get("lastUsedAt", 0.0) or 0.0,
        )


class Accounts:
    def __init__(self, active: int = 0, slots: dict[int, Slot] | None = None) -> None:
        self.active = active
        self.slots: dict[int, Slot] = slots or {}

    @classmethod
    def load(cls) -> "Accounts":
        raw = read_json(accounts_path())
        if not isinstance(raw, dict):
            return cls()
        slots: dict[int, Slot] = {}
        for key, value in (raw.get("slots") or {}).items():
            try:
                number = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                slots[number] = Slot.from_dict(number, value)
        return cls(active=int(raw.get("active", 0) or 0), slots=slots)

    def save(self) -> None:
        write_json_atomic(
            accounts_path(),
            {
                "schema": SCHEMA_VERSION,
                "active": self.active,
                "slots": {
                    str(number): slot.to_dict()
                    for number, slot in sorted(self.slots.items())
                },
            },
        )

    def ordered(self) -> list[Slot]:
        return [self.slots[number] for number in sorted(self.slots)]

    def get(self, number: int) -> Slot | None:
        return self.slots.get(number)

    def ensure(self, number: int) -> Slot:
        slot = self.slots.get(number)
        if slot is None:
            slot = Slot(number=number, created_at=time.time())
            self.slots[number] = slot
        return slot

    def next_free_number(self) -> int:
        number = 1
        while number in self.slots:
            number += 1
        return number

    def by_alias(self, alias: str) -> Slot | None:
        needle = alias.strip().lower()
        if not needle:
            return None
        for slot in self.ordered():
            if slot.alias.lower() == needle:
                return slot
        return None

    def by_email(self, needle: str) -> list[Slot]:
        probe = needle.strip().lower()
        if not probe:
            return []
        return [slot for slot in self.ordered() if slot.email.lower().startswith(probe)]

    def alias_taken(self, alias: str, *, excluding: int | None = None) -> bool:
        needle = alias.strip().lower()
        return any(
            slot.alias.lower() == needle and slot.number != excluding
            for slot in self.ordered()
        )

    def remove(self, number: int) -> None:
        self.slots.pop(number, None)
        if self.active == number:
            self.active = 0

    def backfill_identity(self) -> bool:
        """Name the slots we already know the names of.

        `capture_identity` can only run when the session ends *and* the shared
        `~/.claude.json` still names our own account -- on a machine with
        several terminals open that is the exception, so a slot could stay
        anonymous forever and show up as a bare number. The per-slot identity
        file has no such race: if the slot ever completed a login, it is there.
        """
        changed = False
        for slot in self.ordered():
            if slot.email and slot.account_uuid:
                continue

            raw: Any = None
            with contextlib.suppress(OSError, ValueError):
                raw = read_json(identity_file(slot.number))
            oauth = raw.get("oauthAccount") if isinstance(raw, dict) else None
            if not isinstance(oauth, dict):
                continue

            email = oauth.get("emailAddress")
            account_uuid = oauth.get("accountUuid")
            if not slot.email and isinstance(email, str) and email:
                slot.email = email
                changed = True
            if not slot.account_uuid and isinstance(account_uuid, str) and account_uuid:
                slot.account_uuid = account_uuid
                changed = True
        return changed


def update_accounts(mutate: Callable[[Accounts], None]) -> Accounts:
    """Apply a change to the account store without clobbering concurrent writers.

    A wrapper process outlives the store it loaded at startup: a claude session
    runs for hours while other terminals switch, rename and refresh slots. Saving
    the stale in-memory snapshot would roll all of that back, so every mutation
    re-reads the file under a lock and writes only its own edit.
    """
    with file_lock(accounts_lock_path()):
        fresh = Accounts.load()
        mutate(fresh)
        fresh.save()
    return fresh


def ensure_layout() -> None:
    root = app_dir()
    for directory in (
        root,
        bin_dir(),
        root / "creds",
        root / "identity",
        backups_dir(),
        root / "logs",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    with contextlib.suppress(secure.PermissionWarning, OSError):
        secure.harden_dir(root / "creds")


def ensure_slot_dir(slot: int) -> Path:
    directory = creds_dir(slot)
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(secure.PermissionWarning, OSError):
        secure.harden_dir(directory)
    return directory
