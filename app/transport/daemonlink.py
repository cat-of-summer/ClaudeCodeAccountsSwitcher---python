"""What a session and the CLI need to talk to a running daemon.

Kept apart from `app/daemon.py` so that the session module can import it
without pulling the daemon (which imports the session) in a circle.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from core.sessions import _pid_alive
from core.store import app_dir, read_json


def daemon_file() -> Path:
    return app_dir() / "daemon.json"


def read_daemon() -> dict[str, Any] | None:
    """The live daemon's record, or None."""
    raw = read_json(daemon_file())
    if not isinstance(raw, dict):
        return None
    if not _pid_alive(int(raw.get("pid", 0) or 0)):
        return None
    return raw


class LinkError(Exception):
    pass


def call(port: int, method: str, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 10.0) -> dict[str, Any]:
    body = json.dumps(payload or {}).encode("utf-8") if method in {"POST", "PUT"} else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body, method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise LinkError(f"{path}: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise LinkError(str(exc)) from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise LinkError("bad answer") from exc
    return parsed if isinstance(parsed, dict) else {}


def register(port: int, route: dict[str, Any]) -> str:
    answer = call(port, "POST", "/route", route)
    if not answer.get("ok"):
        raise LinkError("route refused")
    return str(answer.get("tag") or "")


def poll(port: int, pid: int, *, wait: int) -> list[dict[str, Any]]:
    answer = call(port, "GET", f"/poll?pid={pid}&wait={wait}", timeout=wait + 15)
    updates = answer.get("updates")
    return [entry for entry in updates if isinstance(entry, dict)] if isinstance(updates, list) else []


def unregister(port: int, pid: int) -> None:
    try:
        call(port, "DELETE", f"/route?pid={pid}", timeout=5)
    except LinkError:
        pass


def status(port: int) -> dict[str, Any]:
    return call(port, "GET", "/status", timeout=5)


def stop(port: int) -> None:
    call(port, "POST", "/stop", {}, timeout=5)
