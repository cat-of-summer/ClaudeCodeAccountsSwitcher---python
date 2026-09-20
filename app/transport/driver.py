"""One claude driven over stream-json, the way the Agent SDK drives it.

`claude -p --input-format stream-json --output-format stream-json` keeps the
process alive between turns: user messages go in as JSON lines, everything
the session does comes out the same way. With `--permission-prompt-tool
stdio` the process also *asks* over that channel -- a `control_request` of
subtype `can_use_tool` for every permission prompt and every AskUserQuestion,
which is what makes those usable from a chat. The contract was read off the
SDK's own transport and confirmed against claude 2.1.278; the interactive
TUI has no supported way in, so this is the one road.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import wrapper
from core import hookbus, log
from core.store import Config

INITIALIZE_TIMEOUT_SECONDS = 60.0
CONTROL_TIMEOUT_SECONDS = 30.0
STOP_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class Event:
    """What the session did, flattened for whoever renders it."""

    kind: str  # init | text | tool_use | tool_result | result | ask | rate_limit | stderr | exit
    data: dict[str, Any] = field(default_factory=dict)


Listener = Callable[[Event], None]


class DriverError(Exception):
    pass


class Driver:
    def __init__(
        self,
        config: Config,
        slot: int,
        *,
        cwd: Path,
        args: list[str],
        name: str = "",
        session_id: str = "",
        resume: bool = False,
        listener: Listener,
        bus: hookbus.HookBus | None = None,
    ) -> None:
        self.config = config
        self.slot = slot
        self.cwd = cwd
        self.args = list(args)
        self.name = name
        self.session_id = session_id or str(uuid.uuid4())
        self.resume = resume
        self.listener = listener
        self.bus = bus

        self.process: subprocess.Popen[bytes] | None = None
        self.model = ""
        self.tools: list[str] = []
        self.commands: list[dict[str, Any]] = []
        self.turn_active = False
        self.started_at = 0.0

        self._write_lock = threading.Lock()
        self._counter = 0
        self._pending: dict[str, threading.Event] = {}
        self._answers: dict[str, dict[str, Any]] = {}
        self._reader: threading.Thread | None = None
        self._stderr_tail: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    def command_line(self) -> list[str]:
        executable = self.config.real_claude_path
        head = [
            executable,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-prompt-tool",
            "stdio",
        ]
        if self.resume:
            head += ["--resume", self.session_id]
        else:
            head += ["--session-id", self.session_id]
            if self.name:
                head += ["--name", self.name]
        args = list(self.args)
        if self.bus is not None:
            args = hookbus.prepare_args(self.bus, args, wrapper.settings_file())
        return [*head, *wrapper.merge_default_args(self.config.default_args, args)]

    def start(self) -> None:
        executable = self.config.real_claude_path
        if not executable or not Path(executable).exists():
            raise DriverError("real claude missing")
        command = self.command_line()
        env = wrapper.build_environment(self.config, self.slot)
        env["CLAUDE_CODE_ENTRYPOINT"] = "ccas-telegram"
        log.write(
            f"transport: launch slot={self.slot} cwd={self.cwd} session={self.session_id[:8]} "
            f"resume={self.resume} args={self.args}"
        )
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(self.cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise DriverError(str(exc)) from exc
        self.started_at = time.time()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        threading.Thread(target=self._initialize, daemon=True).start()

    def _initialize(self) -> None:
        try:
            answer = self.control({"subtype": "initialize", "hooks": None}, timeout=INITIALIZE_TIMEOUT_SECONDS)
        except DriverError as exc:
            log.write(f"transport: initialize failed: {exc}")
            return
        commands = answer.get("commands")
        if isinstance(commands, list):
            self.commands = [entry for entry in commands if isinstance(entry, dict)]

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def wait(self, timeout: float | None = None) -> int | None:
        if self.process is None:
            return None
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def close(self) -> None:
        """End the session politely: no more input, then wait a little."""
        if self.process is None:
            return
        with contextlib.suppress(OSError, ValueError):
            if self.process.stdin:
                self.process.stdin.close()
        if self.wait(STOP_GRACE_SECONDS) is None:
            wrapper.terminate(self.process)

    def kill(self) -> None:
        if self.process is not None:
            wrapper.terminate(self.process)

    # -- output ------------------------------------------------------------

    def _read_stdout(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        for raw in iter(process.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                log.write(f"transport: unparsable line: {line[:200]}")
                continue
            if isinstance(message, dict):
                try:
                    self._handle(message)
                except Exception as exc:  # the reader must outlive a bad renderer
                    log.write(f"transport: handler failed: {exc!r}")
        code = process.wait()
        self.turn_active = False
        for event in list(self._pending.values()):
            event.set()
        for pipe in (process.stdin, process.stdout):
            with contextlib.suppress(OSError, ValueError):
                if pipe is not None:
                    pipe.close()
        self._emit("exit", code=code, stderr="\n".join(self._stderr_tail[-5:]))

    def _read_stderr(self) -> None:
        process = self.process
        assert process is not None and process.stderr is not None
        for raw in iter(process.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr_tail = [*self._stderr_tail[-19:], line]
                log.write(f"transport: claude stderr: {line[:300]}")
        with contextlib.suppress(OSError, ValueError):
            process.stderr.close()

    def _emit(self, kind: str, **data: Any) -> None:
        self.listener(Event(kind, data))

    def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")

        if kind == "control_response":
            response = message.get("response") or {}
            request_id = str(response.get("request_id") or "")
            if request_id in self._pending:
                self._answers[request_id] = response
                self._pending[request_id].set()
            return

        if kind == "control_request":
            request = message.get("request") or {}
            request_id = str(message.get("request_id") or "")
            if request.get("subtype") == "can_use_tool":
                self._emit(
                    "ask",
                    request_id=request_id,
                    tool_name=str(request.get("tool_name") or ""),
                    input=request.get("input") or {},
                    interactive=bool(request.get("requires_user_interaction")),
                    suggestions=request.get("permission_suggestions") or [],
                    title=str(request.get("title") or ""),
                    description=str(request.get("description") or ""),
                    tool_use_id=str(request.get("tool_use_id") or ""),
                )
            else:
                self.respond(request_id, error=f"unsupported: {request.get('subtype')}")
            return

        if kind == "control_cancel_request":
            self._emit("cancel", request_id=str(message.get("request_id") or ""))
            return

        if kind == "system":
            subtype = message.get("subtype")
            if subtype == "init":
                self.session_id = str(message.get("session_id") or self.session_id)
                self.model = str(message.get("model") or "")
                tools = message.get("tools")
                self.tools = [tool for tool in tools if isinstance(tool, str)] if isinstance(tools, list) else []
                self._emit("init", session_id=self.session_id, model=self.model, cwd=str(message.get("cwd") or ""))
            return

        if kind == "assistant":
            self.turn_active = True
            content = (message.get("message") or {}).get("content") or []
            parent = message.get("parent_tool_use_id")
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    self._emit("text", text=str(block["text"]), subagent=bool(parent))
                elif block.get("type") == "tool_use":
                    self._emit(
                        "tool_use",
                        name=str(block.get("name") or ""),
                        input=block.get("input") or {},
                        id=str(block.get("id") or ""),
                        subagent=bool(parent),
                    )
            return

        if kind == "user":
            content = (message.get("message") or {}).get("content") or []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        self._emit(
                            "tool_result",
                            id=str(block.get("tool_use_id") or ""),
                            content=_flatten(block.get("content")),
                            is_error=bool(block.get("is_error")),
                        )
            return

        if kind == "result":
            self.turn_active = False
            self._emit(
                "result",
                subtype=str(message.get("subtype") or ""),
                text=str(message.get("result") or ""),
                is_error=bool(message.get("is_error")),
                duration_ms=int(message.get("duration_ms") or 0),
                cost=float(message.get("total_cost_usd") or 0.0),
                turns=int(message.get("num_turns") or 0),
                api_error_status=message.get("api_error_status"),
                terminal_reason=str(message.get("terminal_reason") or ""),
            )
            return

        if kind == "rate_limit_event":
            info = message.get("rate_limit_info") or {}
            self._emit(
                "rate_limit",
                status=str(info.get("status") or ""),
                window=str(info.get("rateLimitType") or ""),
                resets_at=float(info.get("resetsAt") or 0.0),
                utilization=info.get("utilization"),
            )
            return

    # -- input -------------------------------------------------------------

    def _write(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise DriverError("session is not running")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._write_lock:
            try:
                process.stdin.write(line.encode("utf-8"))
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise DriverError(str(exc)) from exc

    def send_user(self, text: str) -> None:
        self.turn_active = True
        self._write({"type": "user", "message": {"role": "user", "content": text}})

    def respond(
        self, request_id: str, *, result: dict[str, Any] | None = None, error: str = ""
    ) -> None:
        """Answer a can_use_tool request: allow with input, or deny with why."""
        if error:
            response: dict[str, Any] = {"subtype": "error", "request_id": request_id, "error": error}
        else:
            response = {"subtype": "success", "request_id": request_id, "response": result or {}}
        with contextlib.suppress(DriverError):
            self._write({"type": "control_response", "response": response})

    def control(self, request: dict[str, Any], *, timeout: float = CONTROL_TIMEOUT_SECONDS) -> dict[str, Any]:
        self._counter += 1
        request_id = f"ccas_{self._counter}_{os.urandom(3).hex()}"
        done = threading.Event()
        self._pending[request_id] = done
        try:
            self._write({"type": "control_request", "request_id": request_id, "request": request})
            if not done.wait(timeout):
                raise DriverError(f"control {request.get('subtype')} timed out")
            answer = self._answers.pop(request_id, {})
        finally:
            self._pending.pop(request_id, None)
        if answer.get("subtype") == "error":
            raise DriverError(str(answer.get("error") or "control request failed"))
        result = answer.get("response")
        return result if isinstance(result, dict) else {}

    def interrupt(self) -> None:
        with contextlib.suppress(DriverError):
            self.control({"subtype": "interrupt"}, timeout=10)

    def set_model(self, model: str) -> None:
        self.control({"subtype": "set_model", "model": model or None})

    def set_permission_mode(self, mode: str) -> None:
        self.control({"subtype": "set_permission_mode", "mode": mode})


def _flatten(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return ""
