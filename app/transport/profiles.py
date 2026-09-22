"""A profile: what is listened to, where it runs, and who may talk to it.

The profile is the unit of both configuration and launch. It is known by a
numeric id -- that is what `ccas profile set 2 ...` and `claude -t telegram
-P 2` name -- and it is *addressed* by its alias, the word a chat line opens
with (`/rik собери проект`). Aliases may repeat: two projects can both answer
to `/rik` in two different chats, and which one a line reaches is settled by
the chat it came from.

Nothing here talks to Telegram or starts anything -- the daemon, the
transport and the CLI all read the same answers from these functions, which
is what keeps "who gets this message" from being decided twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import claudecfg
from core.store import DEFAULT_TELEGRAM_PROFILE, Config

# A chat entry is the chat id, optionally narrowed to one forum topic:
# -100500 or "-100500:7".
ChatRef = tuple[int, int]
SKIP_FLAG = "--dangerously-skip-permissions"


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
    id: int = 0
    alias: str = ""
    chats: tuple[ChatRef, ...] = ()
    cwd: str = ""
    slot: int = 0
    daemon: bool = False
    multi: bool = False
    debug: bool = False
    expanded: bool = False
    mode: str = ""
    users: tuple[int, ...] = ()
    args: tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        """How the profile is named in messages and logs: `rik#2`."""
        return f"{self.alias}#{self.id}" if self.alias else f"#{self.id}"

    @property
    def command(self) -> str:
        """The word a chat line opens with to reach this profile."""
        return f"/{self.alias}"

    def answers_to(self, alias: str) -> bool:
        return bool(self.alias) and self.alias.lower() == alias.lower()

    def claims(self, chat: int, thread: int = 0) -> bool:
        """This chat is named in the profile."""
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

    def launch_args(self, config: Config, cli_args: list[str] | tuple[str, ...] = ()) -> list[str]:
        """Everything claude will be given, in the order it will be given.

        The profile's own arguments first, then what was typed for this one
        launch, then whatever ccas adds to every session (`defaultArgs`) --
        the same merge `claude` itself does, so a flag set once in `ccas
        config` reaches a chat session too.
        """
        from app import wrapper

        return wrapper.merge_default_args(config.default_args, [*self.args, *cli_args])

    def bypass_available(self, config: Config, cli_args: list[str] | tuple[str, ...] = ()) -> bool:
        """Whether this session may be in `bypassPermissions` at all.

        claude only lets a session enter that mode when it was started with
        it enabled; the answer decides what "carry on" means after a plan.
        """
        if self.mode == "bypassPermissions":
            return True
        return any(
            arg.split("=", 1)[0] in {SKIP_FLAG, "--allow-dangerously-skip-permissions"}
            for arg in self.launch_args(config, cli_args)
        )

    def resolve_mode(self, config: Config, cwd: Path | None = None, cli_args: list[str] | tuple[str, ...] = ()) -> str:
        """The mode this profile will start in, worked out without starting.

        The profile wins; otherwise the flags claude will actually be given
        decide (the skip flag *is* bypass); otherwise claude's own
        `permissions.defaultMode` from its settings. Empty means the mode
        that asks about everything.
        """
        if self.mode:
            return self.mode
        if SKIP_FLAG in self.launch_args(config, cli_args):
            return "bypassPermissions"
        return claudecfg.default_permission_mode(cwd)

    def resolve_cwd(self) -> Path:
        if self.cwd and Path(self.cwd).is_dir():
            return Path(self.cwd)
        return Path.home()

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "chats": [format_chat(ref) if ref[1] else ref[0] for ref in self.chats],
            "cwd": self.cwd,
            "slot": self.slot,
            "daemon": self.daemon,
            "multi": self.multi,
            "debug": self.debug,
            "expanded": self.expanded,
            "mode": self.mode,
            "users": list(self.users),
            "args": list(self.args),
        }

    @classmethod
    def from_dict(cls, number: int, raw: dict[str, Any] | None) -> "Profile":
        merged = {**DEFAULT_TELEGRAM_PROFILE, **(raw or {})}
        chats = [parse_chat(entry) for entry in (merged.get("chats") or [])]
        users = [int(user) for user in (merged.get("users") or []) if _is_int(user)]
        return cls(
            id=number,
            alias=str(merged.get("alias") or "").strip(),
            chats=tuple(ref for ref in chats if ref is not None),
            cwd=str(merged.get("cwd") or ""),
            slot=int(merged.get("slot") or 0),
            daemon=bool(merged.get("daemon")),
            multi=bool(merged.get("multi")),
            debug=bool(merged.get("debug")),
            expanded=bool(merged.get("expanded")),
            mode=str(merged.get("mode") or ""),
            users=tuple(users),
            args=tuple(str(part) for part in (merged.get("args") or [])),
        )


def _is_int(value: Any) -> bool:
    return str(value).strip().lstrip("-").isdigit()


def load(config: Config) -> dict[int, Profile]:
    """Every profile by id, in id order."""
    stored = config.telegram.get("profiles")
    raw = stored if isinstance(stored, dict) else {}
    found = {
        int(key): Profile.from_dict(int(key), profile)
        for key, profile in raw.items()
        if isinstance(profile, dict) and str(key).strip().isdigit()
    }
    return dict(sorted(found.items()))


def get(config: Config, number: int) -> Profile | None:
    return load(config).get(number)


def by_alias(profiles: dict[int, Profile], alias: str) -> list[Profile]:
    return [profile for profile in profiles.values() if profile.answers_to(alias)]


def candidates(profiles: dict[int, Profile], alias: str, chat: int, thread: int = 0) -> list[Profile]:
    """The profiles a line saying `/alias` in this chat may be for.

    A profile that names the chat beats one open to every chat: the chat a
    line came from is what tells two `/rik`s apart. More than one left over
    is the caller's problem to report -- nothing here guesses.
    """
    named = by_alias(profiles, alias)
    claiming = [profile for profile in named if profile.claims(chat, thread)]
    if claiming:
        return claiming
    return [profile for profile in named if not profile.chats]


def pick(profiles: dict[int, Profile], token: str) -> Profile | list[Profile]:
    """What `<id|alias>` on a command line names.

    Digits are an id. Anything else is an alias, which is an answer only
    when one profile carries it; otherwise every carrier comes back so the
    caller can list them. An unknown id or alias is an empty list.
    """
    probe = token.strip()
    if probe.isdigit():
        found = profiles.get(int(probe))
        return found if found is not None else []
    named = by_alias(profiles, probe)
    return named[0] if len(named) == 1 else named


def global_users(config: Config) -> tuple[int, ...]:
    return tuple(
        int(user) for user in (config.telegram.get("users") or []) if _is_int(user)
    )


def _stored(config: Config) -> dict[str, Any]:
    stored = config.telegram.get("profiles")
    return dict(stored) if isinstance(stored, dict) else {}


def add(profile: Profile) -> Profile:
    """Store a new profile under the next free id and return it with that id."""
    config = Config.load()
    profiles = _stored(config)
    taken = [int(key) for key in profiles if str(key).isdigit()]
    number = max(taken, default=0) + 1
    profiles[str(number)] = profile.to_dict()
    config.telegram = {**config.telegram, "profiles": profiles}
    config.save()
    return Profile.from_dict(number, profiles[str(number)])


def save(profile: Profile) -> Config:
    """Write one profile back, leaving the others as they are on disk."""
    config = Config.load()
    profiles = _stored(config)
    profiles[str(profile.id)] = profile.to_dict()
    config.telegram = {**config.telegram, "profiles": profiles}
    config.save()
    return config


def remove(number: int) -> Config:
    config = Config.load()
    profiles = _stored(config)
    profiles.pop(str(number), None)
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
    """One line for `ccas profile list`, after the id and alias."""
    chats = ", ".join(format_chat(ref) for ref in profile.chats) or "*"
    flags = []
    if profile.daemon:
        flags.append("daemon")
    if profile.multi:
        flags.append("multi")
    if profile.debug:
        flags.append("debug")
    if profile.expanded:
        flags.append("expanded")
    parts = [f"chats {chats}"]
    if profile.mode:
        parts.append(profile.mode)
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
    "Profile",
    "add",
    "by_alias",
    "candidates",
    "daemon_wanted",
    "describe",
    "format_chat",
    "get",
    "global_users",
    "load",
    "parse_chat",
    "pick",
    "remove",
    "save",
]
