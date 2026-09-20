"""Who a chat message is for, and what a `/claude` line asks to launch.

Pure functions, shared by the wrapper (which parses the command line), the
session (which reads its own chat) and the daemon (which reads everyone's).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.store import DEFAULT_PROFILE

TRANSPORTS = ("telegram",)
CLAUDE_COMMAND = "/claude"

_ALIAS_RE = re.compile(r"^[A-Za-z0-9_][\w.-]{0,31}$")


@dataclass
class LaunchOptions:
    """ccas's own flags, lifted out of a `claude ...` argument list."""

    transport: str = ""
    token: str = ""
    chat: int = 0
    thread: int = 0
    cwd: str = ""
    name: str = ""

    @property
    def wants_transport(self) -> bool:
        return bool(self.transport)


def _value(args: list[str], index: int, inline: str | None) -> tuple[str, int]:
    if inline is not None:
        return inline, index + 1
    if index + 1 < len(args):
        return args[index + 1], index + 2
    return "", index + 1


def split_launch_options(args: list[str]) -> tuple[LaunchOptions, list[str]]:
    """Take ccas flags out of `args`; the rest is claude's.

    `-n/--name` is read but left in place: claude wants it too, it is the
    session's display name, and ccas merely reuses it as the chat alias.
    """
    options = LaunchOptions()
    rest: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        name, inline = (token.split("=", 1) if token.startswith("--") and "=" in token else (token, None))

        if name in {"-t", "--transport"}:
            value, index = _value(args, index, inline)
            options.transport = value.strip().lower()
            continue
        if name == "--tg-token":
            options.token, index = _value(args, index, inline)
            continue
        if name == "--tg-chat":
            value, index = _value(args, index, inline)
            options.chat = _int(value)
            continue
        if name == "--tg-thread":
            value, index = _value(args, index, inline)
            options.thread = _int(value)
            continue
        if name == "-C":
            options.cwd, index = _value(args, index, inline)
            continue
        if name in {"-n", "--name"}:
            value, next_index = _value(args, index, inline)
            options.name = value.strip()
            rest.extend(args[index:next_index])
            index = next_index
            continue
        rest.append(token)
        index += 1
    return options, rest


def _int(value: str) -> int:
    try:
        return int(value.strip())
    except ValueError:
        return 0


def valid_alias(alias: str) -> bool:
    return bool(_ALIAS_RE.match(alias))


def tokenize(text: str) -> list[str]:
    """Split a chat line the way a shell would, minus backslash escapes.

    Backslashes stay literal: the paths typed here are Windows paths more
    often than not, and shlex would eat every one of them.
    """
    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    pending = False
    for char in text:
        if quote:
            if char == quote:
                quote = ""
            else:
                current.append(char)
            continue
        if char in {'"', "'"}:
            quote = char
            pending = True
            continue
        if char.isspace():
            if current or pending:
                tokens.append("".join(current))
                current = []
                pending = False
            continue
        current.append(char)
    if current or pending:
        tokens.append("".join(current))
    return tokens


@dataclass(frozen=True)
class Addressed:
    """A chat line after the prefix and alias have been peeled off."""

    alias: str
    body: str


def address(text: str, *, prefix: str, aliases: list[str]) -> Addressed | None:
    """None when the line is not for us: the prefix is required and missing.

    With a prefix set, slash commands and lines that open with a known alias
    still work without it -- both are unambiguous. A known alias as the
    first word routes to that session; the alias alone is a ping.
    """
    line = text.strip()
    if prefix and line.startswith(prefix):
        line = line[len(prefix) :].strip()
    head, _, tail = line.partition(" ")
    if head and head in aliases:
        return Addressed(head, tail.strip())
    if prefix and not text.strip().startswith(prefix) and not line.startswith("/"):
        return None
    if not line:
        return Addressed("", "")
    return Addressed("", line)


@dataclass(frozen=True)
class ClaudeCommand:
    options: LaunchOptions
    args: list[str] = field(default_factory=list)


def parse_claude_command(body: str) -> ClaudeCommand | None:
    """`/claude ...` as the wrapper would see it, or None if not that command."""
    line = body.strip()
    if line.split("@", 1)[0].split(" ", 1)[0] != CLAUDE_COMMAND:
        return None
    _, _, tail = line.partition(" ")
    options, rest = split_launch_options(tokenize(tail))
    return ClaudeCommand(options, rest)


@dataclass(frozen=True)
class Routed:
    """Which profile a chat line belongs to, and what is left of the line."""

    profile: str
    body: str
    addressed: bool = False


def route(
    text: str,
    *,
    prefix: str,
    profiles: dict[str, Any],
    chat: int,
    thread: int = 0,
) -> Routed | None:
    """Whose line this is, or None when it is nobody's.

    The order is the one a person would guess: a name at the front wins; a
    chat named by exactly one profile belongs to that profile; otherwise the
    default profile takes it, but only where it is allowed to work. A named
    profile that listed no chats is reachable by its name alone -- it has not
    claimed anything, so plain talk in a shared chat is not silently its.
    """
    line = text.strip()
    if prefix and line.startswith(prefix):
        line = line[len(prefix) :].strip()
        prefixed = True
    else:
        prefixed = False

    head, _, tail = line.partition(" ")
    if head and head in profiles and head != DEFAULT_PROFILE:
        return Routed(head, tail.strip(), addressed=True)

    if prefix and not prefixed and not line.startswith("/"):
        return None

    owners = [
        name
        for name, profile in profiles.items()
        if profile.claims(chat, thread)
    ]
    if len(owners) == 1:
        return Routed(owners[0], line)
    if owners:
        # Several profiles named the same chat: only an explicit alias can
        # tell them apart, so a plain line has no owner.
        return None

    fallback = profiles.get(DEFAULT_PROFILE)
    if fallback is not None and fallback.open_to(chat, thread):
        return Routed(DEFAULT_PROFILE, line)
    return None
