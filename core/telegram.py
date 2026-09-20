"""The slice of the Telegram Bot API the transport needs, on urllib alone.

Long polling is the one call with a rule attached: Telegram lets a single
consumer call `getUpdates` per bot token, and answers a second one with 409.
That is not a nuisance to work around but the guarantee the transport is
built on -- whoever holds the poll for a token is the only process handling
that bot's chats, on this machine or any other. `TokenBusy` is how the loser
finds out.
"""

from __future__ import annotations

import contextlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from core import log

API_ROOT = "https://api.telegram.org"
MESSAGE_LIMIT = 4096
CALLBACK_DATA_LIMIT = 64
POLL_TIMEOUT_SECONDS = 50
REQUEST_TIMEOUT_SECONDS = 30
BACKOFF_START = 1.0
BACKOFF_MAX = 60.0

_TOKEN_RE = re.compile(r"^(\d+):[A-Za-z0-9_-]{20,}$")


class TelegramError(Exception):
    def __init__(self, method: str, code: int, description: str) -> None:
        super().__init__(f"{method}: {code} {description}")
        self.method = method
        self.code = code
        self.description = description


class TokenBusy(TelegramError):
    """Another process is already polling this bot."""


class Unreachable(Exception):
    """The network, not Telegram, said no; worth retrying."""


def bot_id(token: str) -> int:
    match = _TOKEN_RE.match(token.strip())
    return int(match.group(1)) if match else 0


def looks_like_token(token: str) -> bool:
    return bot_id(token) > 0


def mask(token: str) -> str:
    text = token.strip()
    if not text:
        return ""
    head = text.split(":", 1)[0]
    return f"{head}:…{text[-4:]}" if len(text) > 8 else "…"


@dataclass(frozen=True)
class Incoming:
    """A message or a button press, flattened to what routing needs."""

    update_id: int
    chat_id: int
    thread_id: int
    user_id: int
    text: str
    message_id: int = 0
    callback_id: str = ""
    callback_data: str = ""
    username: str = ""

    @property
    def is_callback(self) -> bool:
        return bool(self.callback_id)


def parse_update(update: dict[str, Any]) -> Incoming | None:
    update_id = int(update.get("update_id") or 0)
    message = update.get("message") or update.get("edited_message")
    if isinstance(message, dict):
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = message.get("text")
        if not isinstance(text, str):
            caption = message.get("caption")
            text = caption if isinstance(caption, str) else ""
        return Incoming(
            update_id=update_id,
            chat_id=int(chat.get("id") or 0),
            thread_id=int(message.get("message_thread_id") or 0),
            user_id=int(sender.get("id") or 0),
            text=text,
            message_id=int(message.get("message_id") or 0),
            username=str(sender.get("username") or ""),
        )
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        origin = callback.get("message") or {}
        chat = origin.get("chat") or {}
        sender = callback.get("from") or {}
        return Incoming(
            update_id=update_id,
            chat_id=int(chat.get("id") or 0),
            thread_id=int(origin.get("message_thread_id") or 0),
            user_id=int(sender.get("id") or 0),
            text="",
            message_id=int(origin.get("message_id") or 0),
            callback_id=str(callback.get("id") or ""),
            callback_data=str(callback.get("data") or ""),
            username=str(sender.get("username") or ""),
        )
    return None


def keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    """An inline keyboard from (label, callback_data) rows."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data[:CALLBACK_DATA_LIMIT]} for label, data in row]
            for row in rows
            if row
        ]
    }


class Bot:
    def __init__(self, token: str, *, api_root: str = API_ROOT) -> None:
        self.token = token.strip()
        self.api_root = api_root.rstrip("/")
        self.id = bot_id(self.token)

    # -- transport ---------------------------------------------------------

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = REQUEST_TIMEOUT_SECONDS
    ) -> Any:
        url = f"{self.api_root}/bot{self.token}/{method}"
        body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            payload = _decode(raw)
            description = str(payload.get("description") or exc.reason)
            if exc.code == 409:
                raise TokenBusy(method, 409, description) from None
            raise TelegramError(method, exc.code, description) from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise Unreachable(str(exc)) from exc

        payload = _decode(raw)
        if not payload.get("ok"):
            raise TelegramError(
                method, int(payload.get("error_code") or 0), str(payload.get("description") or "")
            )
        return payload.get("result")

    def upload(
        self, method: str, params: dict[str, Any], *, field_name: str, filename: str, content: bytes
    ) -> Any:
        boundary = f"----ccas{uuid.uuid4().hex}"
        parts: list[bytes] = []
        for key, value in params.items():
            parts.append(
                (
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n"
                ).encode("utf-8")
            )
        parts.append(
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
        )
        parts.append(content)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)
        url = f"{self.api_root}/bot{self.token}/{method}"
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS * 2) as response:
                payload = _decode(response.read())
        except urllib.error.HTTPError as exc:
            payload = _decode(exc.read())
            raise TelegramError(method, exc.code, str(payload.get("description") or exc.reason)) from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise Unreachable(str(exc)) from exc
        if not payload.get("ok"):
            raise TelegramError(method, int(payload.get("error_code") or 0), str(payload.get("description") or ""))
        return payload.get("result")

    # -- methods -----------------------------------------------------------

    def get_me(self) -> dict[str, Any]:
        result = self.call("getMe")
        return result if isinstance(result, dict) else {}

    def get_updates(self, offset: int, *, timeout: int = POLL_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
        result = self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "edited_message", "callback_query"],
            },
            timeout=timeout + REQUEST_TIMEOUT_SECONDS,
        )
        return [entry for entry in (result or []) if isinstance(entry, dict)]

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int = 0,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = "HTML",
        reply_to: int = 0,
    ) -> int:
        """Send `text`, split into Telegram-sized pieces; returns the last id.

        HTML that Telegram rejects (an unbalanced tag from a code block cut in
        two, say) falls back to the same text sent plain rather than lost.
        """
        last = 0
        pieces = split_message(text)
        for index, piece in enumerate(pieces):
            params: dict[str, Any] = {
                "chat_id": chat_id,
                "text": piece,
                "link_preview_options": {"is_disabled": True},
            }
            if thread_id:
                params["message_thread_id"] = thread_id
            if reply_to and index == 0:
                params["reply_parameters"] = {"message_id": reply_to}
            if reply_markup and index == len(pieces) - 1:
                params["reply_markup"] = reply_markup
            if parse_mode:
                params["parse_mode"] = parse_mode
            try:
                result = self.call("sendMessage", params)
            except TelegramError as exc:
                if parse_mode is None or exc.code != 400:
                    raise
                params.pop("parse_mode", None)
                params["text"] = strip_html(piece)
                result = self.call("sendMessage", params)
            if isinstance(result, dict):
                last = int(result.get("message_id") or 0)
        return last

    def edit_markup(self, chat_id: int, message_id: int, reply_markup: dict[str, Any] | None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        params["reply_markup"] = reply_markup or {"inline_keyboard": []}
        with contextlib.suppress(TelegramError):
            self.call("editMessageReplyMarkup", params)

    def edit_text(
        self, chat_id: int, message_id: int, text: str, *, parse_mode: str | None = "HTML"
    ) -> bool:
        """Rewrite a message; False when it is not there to rewrite.

        A message the person deleted answers 400 "message to edit not found",
        and a caller keeping one live message per turn has to know the
        difference between that and a hiccup -- it has to start a new one.
        Text Telegram will not parse falls back to plain, as sending does.
        """
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text[:MESSAGE_LIMIT]}
        if parse_mode:
            params["parse_mode"] = parse_mode
        try:
            self.call("editMessageText", params)
        except TelegramError as exc:
            if _message_gone(exc):
                return False
            if parse_mode is None or exc.code != 400:
                raise
            params.pop("parse_mode", None)
            params["text"] = strip_html(text)[:MESSAGE_LIMIT]
            try:
                self.call("editMessageText", params)
            except TelegramError as retry:
                if _message_gone(retry):
                    return False
                if not _unchanged(retry):
                    raise
        return True

    def delete_message(self, chat_id: int, message_id: int) -> None:
        """Take a message down; one already gone is not an error."""
        with contextlib.suppress(TelegramError, Unreachable):
            self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        params: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            params["text"] = text[:200]
        with contextlib.suppress(TelegramError):
            self.call("answerCallbackQuery", params)

    def typing(self, chat_id: int, *, thread_id: int = 0) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "action": "typing"}
        if thread_id:
            params["message_thread_id"] = thread_id
        with contextlib.suppress(TelegramError, Unreachable):
            self.call("sendChatAction", params, timeout=10)

    def send_document(
        self, chat_id: int, filename: str, content: bytes, *, caption: str = "", thread_id: int = 0
    ) -> None:
        params: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            params["caption"] = caption[:1024]
        if thread_id:
            params["message_thread_id"] = thread_id
        self.upload("sendDocument", params, field_name="document", filename=filename, content=content)


# What Telegram says when the message is gone, and when the new text is the
# same as the old -- neither is a failure worth raising.
_GONE_MARKS = ("message to edit not found", "message can't be edited", "message to delete not found")
_UNCHANGED_MARK = "message is not modified"


def _message_gone(exc: TelegramError) -> bool:
    lowered = exc.description.lower()
    return exc.code == 400 and any(mark in lowered for mark in _GONE_MARKS)


def _unchanged(exc: TelegramError) -> bool:
    return exc.code == 400 and _UNCHANGED_MARK in exc.description.lower()


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------------
# polling
# --------------------------------------------------------------------------


@dataclass
class PollState:
    offset: int = 0
    backoff: float = BACKOFF_START
    last_error: str = ""
    errors: int = 0
    updates: int = field(default=0)


def skip_pending(bot: Bot, state: PollState) -> bool:
    """Mark everything queued before now as read; True when there was a backlog.

    Nobody was listening while those piled up, and replaying a day of
    `/claude` lines the moment the daemon comes up would open a window per
    line. `offset=-1` is Telegram's way of asking for just the last update.
    """
    try:
        last = bot.call("getUpdates", {"offset": -1, "timeout": 0}, timeout=REQUEST_TIMEOUT_SECONDS)
    except TokenBusy:
        raise
    except (TelegramError, Unreachable) as exc:
        log.write(f"telegram: could not skip the backlog ({exc}); starting from it")
        return False
    entries = [entry for entry in (last or []) if isinstance(entry, dict)]
    if not entries:
        return False
    state.offset = int(entries[-1].get("update_id") or 0) + 1
    log.write("telegram: backlog skipped, listening from now")
    return True


def poll_once(bot: Bot, state: PollState, *, timeout: int = POLL_TIMEOUT_SECONDS) -> list[Incoming]:
    """One getUpdates round with the offset and backoff bookkeeping done.

    Raises TokenBusy straight through: that is a verdict, not a hiccup.
    Anything else is logged, slept over with a growing pause, and retried by
    the caller's next round.
    """
    try:
        updates = bot.get_updates(state.offset, timeout=timeout)
    except TokenBusy:
        raise
    except (TelegramError, Unreachable) as exc:
        state.errors += 1
        state.last_error = str(exc)
        if state.errors == 1 or state.backoff >= BACKOFF_MAX:
            log.write(f"telegram: poll failed ({exc}); retrying in {state.backoff:.0f}s")
        time.sleep(state.backoff)
        state.backoff = min(state.backoff * 2, BACKOFF_MAX)
        return []

    state.backoff = BACKOFF_START
    state.errors = 0
    incoming: list[Incoming] = []
    for update in updates:
        state.offset = max(state.offset, int(update.get("update_id") or 0) + 1)
        parsed = parse_update(update)
        if parsed is not None:
            incoming.append(parsed)
    state.updates += len(incoming)
    return incoming


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```([\w+-]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_BULLET_RE = re.compile(r"^(\s*)[-*]\s+", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_TAG_RE = re.compile(r"</?(b|i|code|pre|a)(\s[^>]*)?>")


def markdown_to_html(text: str) -> str:
    """The markdown claude writes, as the HTML subset Telegram renders.

    Code is protected first so nothing inside it is touched; everything else
    is escaped, then a handful of markers are turned into tags. Whatever this
    misses renders as literal text, which is the right failure.
    """
    slots: list[str] = []

    def _stash(rendered: str) -> str:
        slots.append(rendered)
        return f"\x00{len(slots) - 1}\x00"

    def _fence(match: re.Match[str]) -> str:
        # A bare <pre>, no nested <code class=...>: the splitter closes and
        # reopens <pre> across a cut, and a nested tag would unbalance that.
        code = html.escape(match.group(2).rstrip("\n"))
        return _stash(f"<pre>{code}</pre>")

    work = _FENCE_RE.sub(_fence, text)
    work = _INLINE_CODE_RE.sub(lambda m: _stash(f"<code>{html.escape(m.group(1))}</code>"), work)
    work = _LINK_RE.sub(lambda m: _stash(f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1))}</a>'), work)
    work = html.escape(work, quote=False)
    work = _HEADING_RE.sub(lambda m: f"<b>{m.group(1)}</b>", work)
    work = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", work)
    work = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1)}</i>", work)
    work = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", work)
    return re.sub(r"\x00(\d+)\x00", lambda m: slots[int(m.group(1))], work)


def strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text))


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Cut at line breaks, keeping <pre> blocks balanced across the cut."""
    if len(text) <= limit:
        return [text] if text else []
    pieces: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 4:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 4:
            cut = limit
        head, rest = rest[:cut], rest[cut:].lstrip("\n")
        if head.count("<pre>") > head.count("</pre>"):
            head += "</pre>"
            rest = "<pre>" + rest
        pieces.append(head)
    if rest:
        pieces.append(rest)
    return pieces
