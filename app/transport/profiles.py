"""A profile: what is listened to, where it runs, and who may talk to it.

The profile is the unit of both configuration and launch. `default` is the
one every unaddressed message belongs to; it always exists, even when the
config file has never mentioned it. A named profile is addressed by its name
(the alias) at the start of a chat line, and `claude -t telegram -n <name>`
is how it is raised by hand.

Nothing here talks to Telegram or starts anything -- the daemon, the
transport and the CLI all read the same answers from these functions, which
is what keeps "who gets this message" from being decided twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.store import DEFAULT_PROFILE, DEFAULT_TELEGRAM_PROFILE, Config

# A chat entry is the chat id, optionally narrowed to one forum topic:
# -100500 or "-100500:7".
ChatRef = tuple[int, int]


def parse_chat(raw: Any) -> ChatRef | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return (raw, 0)
    text = str(raw or "").strip()
    if not text:
        return None
    chat, _, thread = text.partition(":")
    try:
        return (int(chat), int(thread) if thread.strip() else 0)
    except ValueError:
        return None


def format_chat(ref: ChatRef) -> str:
    chat, thread = ref
    return f"{chat}:{thread}" if thread else str(chat)


@dataclass(frozen=True)
class Profile:
    name: str
    chats: tuple[ChatRef, ...] = ()
    cwd: str = ""
    slot: int = 0
    daemon: bool = False
    multi: bool = False
    users: tuple[int, ...] = ()
    args: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_default(self) -> bool:
        return self.name == DEFAULT_PROFILE

    @property
    def alias(self) -> str:
        """What a chat line says to reach this profile; the default has none."""
        return "" if self.is_default else self.name

    def claims(self, chat: int, thread: int = 0) -> bool:
        """This chat is named in the profile, so plain lines here are its own."""
        return any(
            ref[0] == chat and (ref[1] == 0 or ref[1] == thread) for ref in self.chats
        )

    def open_to(self, chat: int, thread: int = 0) -> bool:
        """The profile may work in this chat: it named it, or it named none."""
        return not self.chats or self.claims(chat, thread)

    def allows(self, user_id: int, global_users: tuple[int, ...] | list[int] = ()) -> bool:
        """The global whitelist is the floor; a profile may only narrow it."""
        if global_users and user_id not in [int(user) for user in global_users]:
            return False
        return not self.users or user_id in self.users

    def resolve_cwd(self, config: Config) -> Path:
        for candidate in (self.cwd, str(config.telegram.get("workdir") or "")):
            if candidate and Path(candidate).is_dir():
                return Path(candidate)
        return Path.home()

    def to_dict(self) -> dict[str, Any]:
        return {
            "chats": [format_chat(ref) if ref[1] else ref[0] for ref in self.chats],
            "cwd": self.cwd,
            "slot": self.slot,
            "daemon": self.daemon,
            "multi": self.multi,
            "users": list(self.users),
            "args": list(self.args),
        }

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any] | None) -> "Profile":
        merged = {**DEFAULT_TELEGRAM_PROFILE, **(raw or {})}
        chats = [parse_chat(entry) for entry in (merged.get("chats") or [])]
        users = [int(user) for user in (merged.get("users") or []) if _is_int(user)]
        return cls(
            name=name,
            chats=tuple(ref for ref in chats if ref is not None),
            cwd=str(merged.get("cwd") or ""),
            slot=int(merged.get("slot") or 0),
            daemon=bool(merged.get("daemon")),
            multi=bool(merged.get("multi")),
            users=tuple(users),
            args=tuple(str(part) for part in (merged.get("args") or [])),
        )


def _is_int(value: Any) -> bool:
    return str(value).strip().lstrip("-").isdigit()


def load(config: Config) -> dict[str, Profile]:
    """Every profile, `default` included whether or not it is on disk."""
    stored = config.telegram.get("profiles")
    raw = stored if isinstance(stored, dict) else {}
    found = {
        name: Profile.from_dict(name, profile)
        for name, profile in raw.items()
        if isinstance(profile, dict) and str(name).strip()
    }
    found.setdefault(DEFAULT_PROFILE, Profile.from_dict(DEFAULT_PROFILE, None))
    return found


def get(config: Config, name: str) -> Profile | None:
    return load(config).get(name or DEFAULT_PROFILE)


def global_users(config: Config) -> tuple[int, ...]:
    return tuple(
        int(user) for user in (config.telegram.get("users") or []) if _is_int(user)
    )


def save(profile: Profile) -> Config:
    """Write one profile back, leaving the others as they are on disk."""
    config = Config.load()
    profiles = dict(config.telegram.get("profiles") or {})
    profiles[profile.name] = profile.to_dict()
    config.telegram = {**config.telegram, "profiles": profiles}
    config.save()
    return config


def remove(name: str) -> Config:
    """Delete a profile; the default one is reset instead, never dropped."""
    config = Config.load()
    profiles = dict(config.telegram.get("profiles") or {})
    if name == DEFAULT_PROFILE:
        profiles[DEFAULT_PROFILE] = dict(DEFAULT_TELEGRAM_PROFILE)
    else:
        profiles.pop(name, None)
    config.telegram = {**config.telegram, "profiles": profiles}
    config.save()
    return config


def daemon_wanted(config: Config) -> bool:
    """Whether anything needs the daemon running without being asked.

    This is what the OS autostart entry is derived from: a profile the daemon
    may raise on its own is the only reason to have it up before a transport
    is started by hand.
    """
    return any(profile.daemon for profile in load(config).values())


def describe(profile: Profile) -> str:
    """One line for `ccas profile list`."""
    chats = ", ".join(format_chat(ref) for ref in profile.chats) or "*"
    flags = []
    if profile.daemon:
        flags.append("daemon")
    if profile.multi:
        flags.append("multi")
    parts = [f"chats {chats}"]
    if profile.slot:
        parts.append(f"slot {profile.slot}")
    if profile.cwd:
        parts.append(profile.cwd)
    if profile.users:
        parts.append("users " + ",".join(str(user) for user in profile.users))
    if profile.args:
        parts.append(" ".join(profile.args))
    if flags:
        parts.append("[" + " ".join(flags) + "]")
    return "  ".join(parts)


__all__ = [
    "ChatRef",
    "DEFAULT_PROFILE",
    "Profile",
    "daemon_wanted",
    "describe",
    "format_chat",
    "get",
    "global_users",
    "load",
    "parse_chat",
    "remove",
    "save",
]
