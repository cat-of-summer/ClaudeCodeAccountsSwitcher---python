from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.version import SCHEMA_VERSION, __version__
from system import secure

APP_DIR_NAME = ".claude-switcher"
LEGACY_STORE_NAME = ".claude-accounts"
CREDENTIALS_FILENAME = ".credentials.json"
BACKUP_KEEP = 10

DEFAULT_ARGS = ["--dangerously-skip-permissions"]


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

    @classmethod
    def load(cls) -> "Config":
        raw = read_json(config_path())
        if not isinstance(raw, dict):
            return cls()
        return cls(
            schema=raw.get("schema", SCHEMA_VERSION),
            version=raw.get("version", __version__),
            installed_at=raw.get("installedAt", 0.0),
            real_claude_path=raw.get("realClaudePath", ""),
            cred_mode=raw.get("credMode", "env"),
            cred_mode_probe=raw.get("credModeProbe") or {},
            default_args=raw.get("defaultArgs") or list(DEFAULT_ARGS),
            shim_dir=raw.get("shimDir", ""),
            language=raw.get("language", ""),
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
            },
        )


def is_installed() -> bool:
    return config_path().exists()


@dataclass
class Slot:
    number: int
    alias: str = ""
    email: str = ""
    account_uuid: str = ""
    user_id: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
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
