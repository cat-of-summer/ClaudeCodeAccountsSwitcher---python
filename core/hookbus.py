"""Every hook claude fires, delivered to the wrapper that launched it.

claude can post a hook's payload to an HTTP URL instead of running a command
(`"type": "http"` in the hooks block of a settings file). The wrapper opens a
listener on the loopback interface, hands claude a settings file that points
every event at it, and from then on sees what the session does -- tool calls,
prompts, notifications, the moment a turn fails on a rate limit -- without
touching `~/.claude/settings.json` and without starting a process per event.
The second point matters: the shipped ccas is a one-file PyInstaller build and
takes about a second to come up, which is too slow to sit in front of every
tool call.

Consumers, in order: in-process subscribers (the Telegram transport answers
permission requests this way), external handlers from `config.json` (a
command per event, JSON on stdin), and one line per event in the journal.

Event names: https://code.claude.com/docs/en/hooks
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from core import log

HOOK_EVENTS: tuple[str, ...] = (
    "SessionStart",
    "SessionEnd",
    "Setup",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "PermissionRequest",
    "PermissionDenied",
    "Notification",
    "MessageDisplay",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "InstructionsLoaded",
    "ConfigChange",
    "CwdChanged",
    "DirectoryAdded",
    "FileChanged",
    "TaskCreated",
    "TaskCompleted",
    "TeammateIdle",
    "Elicitation",
    "ElicitationResult",
    "PreModelSwitch",
    "PostModelSwitch",
    "WorktreeCreate",
    "WorktreeRemove",
)

# How long claude waits for our answer. Subscribers that need a human (a
# permission prompt relayed to a chat) take minutes; everything else answers
# in milliseconds. claude's own cap for SessionEnd is 1.5 s, so that one is
# left at its default rather than raised.
DECISION_TIMEOUT_SECONDS = 600
QUICK_TIMEOUT_SECONDS = 30
DECISION_EVENTS = frozenset({"PreToolUse", "PermissionRequest", "Elicitation"})
NO_TIMEOUT_EVENTS = frozenset({"SessionEnd"})

HANDLER_TIMEOUT_SECONDS = 10.0

FLAGS_DISABLING_HOOKS = frozenset({"--bare", "--safe-mode"})


@dataclass(frozen=True)
class HookEvent:
    name: str
    payload: dict[str, Any]
    received_at: float = field(default_factory=time.time)

    @property
    def tool_name(self) -> str:
        value = self.payload.get("tool_name")
        return value if isinstance(value, str) else ""

    @property
    def session_id(self) -> str:
        value = self.payload.get("session_id")
        return value if isinstance(value, str) else ""

    def summary(self) -> str:
        """One line for the journal: the event, and what it was about."""
        parts = [self.name]
        if self.tool_name:
            parts.append(f"tool={self.tool_name}")
        for key in ("notification_type", "error", "matcher", "source", "trigger"):
            value = self.payload.get(key)
            if isinstance(value, str) and value:
                parts.append(f"{key}={value}")
        if self.session_id:
            parts.append(f"session={self.session_id[:8]}")
        return " ".join(parts)


Subscriber = Callable[[HookEvent], "dict[str, Any] | None"]


@dataclass(frozen=True)
class Handler:
    """An external command from `config.hooks`.

    `event` is one name, several joined with `|`, or `*`; `matcher` is the
    same regex claude uses, applied to `tool_name`. A sync handler's stdout is
    parsed as JSON and handed to claude as the hook's answer -- that is how a
    user script blocks a tool call. An async one runs in a thread and its
    output is ignored.
    """

    event: str
    command: str
    matcher: str = ""
    sync: bool = False
    timeout: float = HANDLER_TIMEOUT_SECONDS

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Handler | None":
        event = str(raw.get("event") or "").strip()
        command = str(raw.get("command") or "").strip()
        if not event or not command:
            return None
        timeout = raw.get("timeout")
        return cls(
            event=event,
            command=command,
            matcher=str(raw.get("matcher") or ""),
            sync=bool(raw.get("sync", False)),
            timeout=float(timeout) if isinstance(timeout, (int, float)) and timeout > 0
            else HANDLER_TIMEOUT_SECONDS,
        )

    def to_dict(self) -> dict[str, Any]:
        raw: dict[str, Any] = {"event": self.event, "command": self.command}
        if self.matcher:
            raw["matcher"] = self.matcher
        if self.sync:
            raw["sync"] = True
        if self.timeout != HANDLER_TIMEOUT_SECONDS:
            raw["timeout"] = self.timeout
        return raw

    def wants(self, event: HookEvent) -> bool:
        if self.event != "*" and event.name not in self.event.split("|"):
            return False
        if not self.matcher:
            return True
        try:
            return re.search(self.matcher, event.tool_name) is not None
        except re.error:
            return self.matcher == event.tool_name


def parse_handlers(raw: Any) -> list[Handler]:
    if not isinstance(raw, list):
        return []
    handlers: list[Handler] = []
    for entry in raw:
        if isinstance(entry, dict):
            handler = Handler.from_dict(entry)
            if handler is not None:
                handlers.append(handler)
    return handlers


class HookBus:
    def __init__(
        self,
        handlers: list[Handler] | None = None,
        *,
        slot: int = 0,
        journal: bool = True,
    ) -> None:
        self._handlers = list(handlers or [])
        self._slot = slot
        self._journal = journal
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.events_seen = 0

    # -- lifecycle ---------------------------------------------------------

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    def start(self) -> int:
        if self._server is not None:
            return self.port
        bus = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 -- http.server naming
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                answer = bus._receive(raw)
                body = json.dumps(answer).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        with contextlib.suppress(Exception):
            server.shutdown()
            server.server_close()

    def __enter__(self) -> "HookBus":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- consumers ---------------------------------------------------------

    def subscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.append(subscriber)

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(subscriber)

    # -- the settings claude is launched with ------------------------------

    def settings(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """A settings document pointing every event at this bus.

        `extra` is whatever the user passed as their own `--settings`: their
        keys win, and their hooks are kept next to ours rather than replaced.
        """
        hooks: dict[str, Any] = {}
        for name in HOOK_EVENTS:
            entry: dict[str, Any] = {"type": "http", "url": self.url}
            if name not in NO_TIMEOUT_EVENTS:
                entry["timeout"] = (
                    DECISION_TIMEOUT_SECONDS
                    if name in DECISION_EVENTS
                    else QUICK_TIMEOUT_SECONDS
                )
            hooks[name] = [{"hooks": [entry]}]
        return merge_settings({"hooks": hooks}, extra)

    # -- dispatch ----------------------------------------------------------

    def _receive(self, raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {}
        name = payload.get("hook_event_name")
        if not isinstance(name, str) or not name:
            return {}
        try:
            return self.dispatch(HookEvent(name, payload))
        except Exception as exc:  # a broken consumer must not stall claude
            log.write(f"hook {name}: dispatch failed: {exc!r}")
            return {}

    def dispatch(self, event: HookEvent) -> dict[str, Any]:
        """Run every consumer; the first non-empty answer goes back to claude."""
        self.events_seen += 1
        if self._journal:
            log.write(f"hook {event.summary()}")

        decision: dict[str, Any] = {}
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                answer = subscriber(event)
            except Exception as exc:
                log.write(f"hook {event.name}: subscriber failed: {exc!r}")
                continue
            if answer and not decision:
                decision = dict(answer)

        for handler in self._handlers:
            if not handler.wants(event):
                continue
            if handler.sync:
                answer = self._run_handler(handler, event)
                if answer and not decision:
                    decision = answer
            else:
                threading.Thread(
                    target=self._run_handler, args=(handler, event), daemon=True
                ).start()
        return decision

    def _run_handler(self, handler: Handler, event: HookEvent) -> dict[str, Any]:
        env = dict(os.environ)
        env["CCAS_HOOK_EVENT"] = event.name
        env["CCAS_SESSION_ID"] = event.session_id
        env["CCAS_SLOT"] = str(self._slot)
        try:
            completed = subprocess.run(
                handler.command,
                shell=True,
                input=json.dumps(event.payload, ensure_ascii=False).encode("utf-8"),
                capture_output=True,
                timeout=handler.timeout,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            log.write(f"hook {event.name}: handler timed out: {handler.command}")
            return {}
        except OSError as exc:
            log.write(f"hook {event.name}: handler failed to start: {exc}")
            return {}

        if completed.returncode != 0:
            tail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
            reason = tail[-1] if tail else f"exit {completed.returncode}"
            log.write(f"hook {event.name}: handler {handler.command!r}: {reason}")
            if completed.returncode == 2 and handler.sync:
                # claude's own convention: exit 2 blocks, stderr says why.
                return _block(event, reason)
            return {}

        if not handler.sync:
            return {}
        text = completed.stdout.decode("utf-8", "replace").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


# claude has no "local artifacts" mode: `enableArtifact: false` takes the
# tool away whole, and with it the agent's sense of when a page is the right
# answer. So the tool stays, and the one step that puts the page on
# claude.ai is refused on its way out.
ARTIFACT_TOOL = "Artifact"
ARTIFACT_PUBLISH_ACTIONS = frozenset({"", "publish"})
ARTIFACT_LOCAL_REASON = (
    "Publishing artifacts is turned off in this environment: the page stays a local file and "
    "nothing is uploaded. Do not retry the publish and do not look for another way to share it. "
    "Tell the user the full local path of the file instead of a link."
)


def is_artifact_publish(tool_name: str, tool_input: Any) -> bool:
    """An Artifact call that would put something on claude.ai."""
    if tool_name != ARTIFACT_TOOL or not isinstance(tool_input, dict):
        return False
    return str(tool_input.get("action") or "") in ARTIFACT_PUBLISH_ACTIONS


def block_artifact_publish(event: HookEvent) -> dict[str, Any] | None:
    """A subscriber that keeps artifacts local; see ARTIFACT_LOCAL_REASON."""
    if event.name != "PreToolUse":
        return None
    if not is_artifact_publish(event.tool_name, event.payload.get("tool_input")):
        return None
    return _block(event, ARTIFACT_LOCAL_REASON)


def _block(event: HookEvent, reason: str) -> dict[str, Any]:
    if event.name == "PreToolUse":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    if event.name == "PermissionRequest":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "deny", "message": reason},
            }
        }
    return {"decision": "block", "reason": reason}


# --------------------------------------------------------------------------
# settings handling
# --------------------------------------------------------------------------


def merge_settings(
    ours: dict[str, Any], theirs: dict[str, Any] | None
) -> dict[str, Any]:
    if not theirs:
        return ours
    merged = dict(ours)
    for key, value in theirs.items():
        if key == "hooks" and isinstance(value, dict):
            hooks = dict(merged.get("hooks") or {})
            for name, matchers in value.items():
                existing = list(hooks.get(name) or [])
                hooks[name] = existing + list(matchers if isinstance(matchers, list) else [])
            merged["hooks"] = hooks
        else:
            merged[key] = value
    return merged


def load_settings_argument(value: str) -> dict[str, Any] | None:
    """What `--settings X` meant: inline JSON, or a file holding it."""
    text = value.strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    path = Path(text).expanduser()
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def split_settings_flag(args: list[str]) -> tuple[list[str], str | None]:
    """Pull the user's own `--settings` out of the argument list."""
    kept: list[str] = []
    value: str | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--settings" and index + 1 < len(args):
            value = args[index + 1]
            index += 2
            continue
        if token.startswith("--settings="):
            value = token.split("=", 1)[1]
            index += 1
            continue
        kept.append(token)
        index += 1
    return kept, value


def hooks_disabled_by(args: list[str]) -> bool:
    return any(arg in FLAGS_DISABLING_HOOKS for arg in args)


def prepare_args(
    bus: HookBus, args: list[str], settings_path: Path
) -> list[str]:
    """Write the settings file for this launch and point claude at it."""
    rest, own = split_settings_flag(args)
    extra = load_settings_argument(own) if own else None
    if own and extra is None:
        # claude honours one --settings, so theirs cannot ride along unread;
        # the journal says what was lost rather than the session silently
        # running without their file.
        log.write(f"--settings {own!r} could not be read, not merged")
    payload = bus.settings(extra)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    return ["--settings", str(settings_path), *rest]
