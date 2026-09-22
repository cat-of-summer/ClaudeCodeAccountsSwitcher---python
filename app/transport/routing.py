"""Who a chat message is for, and what a `/claude` line asks to launch.

Pure functions, shared by the wrapper (which parses the command line), the
session (which reads its own chat) and the daemon (which reads everyone's).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

TRANSPORTS = ("telegram",)
CLAUDE_COMMAND = "/claude"

_ALIAS_RE = re.compile(r"^[A-Za-z0-9_][\w.-]{0,31}$")
# `/rik`, `/rik!`, `/rik@bot`, `/rik!@bot` -- the alias, an optional bang
# meaning "drop what you are doing", and the bot suffix Telegram adds when
# a command is picked from its menu in a group.
_ADDRESS_RE = re.compile(r"^/([A-Za-z0-9_][\w.-]{0,31})(!?)(?:@\w+)?$")


@dataclass
class LaunchOptions:
    """ccas's own flags, lifted out of a `claude ...` argument list."""

    transport: str = ""
    token: str = ""
    chat: int = 0
    thread: int = 0
    cwd: str = ""
    name: str = ""
    profile: int = 0

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
    session's display name, and ccas merely reuses it to find the profile
    when `-P` does not say which.
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
        if name in {"-P", "--profile"}:
            value, index = _value(args, index, inline)
            options.profile = _int(value)
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
class Address:
    """A chat line after the `/alias` at its front has been peeled off."""

    alias: str
    body: str
    urgent: bool = False


def parse_address(text: str) -> Address | None:
    """`/rik text` -> who is called and what is said; None for anything else.

    `/rik!` is the same call with the bang meaning: interrupt whatever the
    agent is doing and take this now.
    """
    line = text.strip()
    head, _, tail = re.split(r"(\s)", line, maxsplit=1) if re.search(r"\s", line) else (line, "", "")
    match = _ADDRESS_RE.match(head)
    if match is None:
        return None
    return Address(match.group(1), tail.strip(), urgent=bool(match.group(2)))


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
    """Which profile a chat line belongs to, and what is left of the line.

    `profile` is 0 with `ambiguous` filled when several profiles answer to
    the alias in this chat -- the line is for one of them, and nobody here
    can say which.
    """

    profile: int
    body: str
    urgent: bool = False
    ambiguous: tuple[int, ...] = ()


def route(
    text: str,
    *,
    profiles: dict[int, Any],
    chat: int,
    thread: int = 0,
) -> Routed | None:
    """Whose line this is, or None when it is nobody's.

    Every line for an agent opens with its alias, and that is the *only* way
    in: a profile hears nothing that does not say its name. That is what
    makes a conversation endable -- stop saying the name and the agent stops
    answering. A line that names no known alias is not for a profile at all;
    the daemon may still read it as one of its own commands.
    """
    from app.transport import profiles as profiles_module

    address = parse_address(text)
    if address is None:
        return None
    found = profiles_module.candidates(profiles, address.alias, chat, thread)
    if not found:
        return None
    if len(found) > 1:
        return Routed(0, address.body, address.urgent, ambiguous=tuple(p.id for p in found))
    return Routed(found[0].id, address.body, address.urgent)


# -- the session's own commands, several to a line ---------------------------

# What a conversation handles itself rather than passing to claude. A line
# may carry several in a row -- `/new /plan Давай…` -- and they are run
# in order; the first word that is none of these starts the prompt.
NO_ARG_COMMANDS = frozenset(
    {
        "/stop", "/kill", "/exit", "/quit", "/clear", "/new", "/status", "/help", "/usage",
        "/pwd", "/save", "/plan", "/bypass", "/auto", "/edits", "/ask",
    }
)
ONE_ARG_COMMANDS = frozenset({"/cd", "/switch", "/model", "/mode"})


def _next_word(text: str, start: int) -> tuple[str, int]:
    """The token at `start` (quotes honoured, kept out of the value) and
    where the text continues after it."""
    index = start
    quote = ""
    parts: list[str] = []
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
            else:
                parts.append(char)
        elif char in {'"', "'"}:
            quote = char
        elif char.isspace():
            break
        else:
            parts.append(char)
        index += 1
    return "".join(parts), index


def split_commands(body: str) -> tuple[list[tuple[str, str]], str]:
    """Own commands at the front of a line, and whatever follows them.

    `/new /plan Давай сделаем` -> [("/new", ""), ("/plan", "")], "Давай
    сделаем". A one-argument command takes the next word, quoted if it has
    spaces. The remainder is returned as typed -- newlines and all -- since
    it is the prompt.
    """
    commands: list[tuple[str, str]] = []
    index = 0
    text = body
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] != "/":
            break
        word, after = _next_word(text, index)
        head = word.lower().split("@", 1)[0]
        if head in NO_ARG_COMMANDS:
            commands.append((head, ""))
            index = after
            continue
        if head in ONE_ARG_COMMANDS:
            arg_start = after
            while arg_start < len(text) and text[arg_start].isspace():
                arg_start += 1
            argument, after = _next_word(text, arg_start)
            commands.append((head, argument))
            index = after
            continue
        break
    return commands, text[index:].strip()
