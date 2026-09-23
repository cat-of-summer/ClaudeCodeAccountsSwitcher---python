from __future__ import annotations

import dataclasses
import io
import json
import threading
import unittest
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from core import telegram


def _multipart(content_type: str, raw: bytes) -> dict[str, Any]:
    """A multipart body as the fields it carried; files under `_files`."""
    message = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode() + raw)
    body: dict[str, Any] = {"_files": {}}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            body["_files"][name] = (filename, payload)
        else:
            text = payload.decode("utf-8")
            body[name] = json.loads(text) if text[:1] in "[{" else text
    return body

TOKEN = "123456:ABCDEFghijklmnopqrstuvwxyz0123456789"


class FakeApi:
    """Just enough of api.telegram.org to drive the client through."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []
        self.busy = False
        self.reject_html = False
        # What the file endpoint serves, by request path.
        self.stored: dict[str, bytes] = {}
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                kind = self.headers.get("Content-Type") or ""
                body = _multipart(kind, raw) if kind.startswith("multipart/") else json.loads(raw or b"{}")
                method = self.path.rsplit("/", 1)[-1]
                api.calls.append((method, body))
                status, payload = api.answer(method, body)
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                api.calls.append(("GET", {"path": self.path}))
                content = api.stored.get(self.path)
                self.send_response(200 if content is not None else 404)
                self.send_header("Content-Length", str(len(content or b"")))
                self.end_headers()
                self.wfile.write(content or b"")

            def log_message(self, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def root(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def answer(self, method: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if method == "getUpdates":
            if self.busy:
                return 409, {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates request"}
            offset = int(body.get("offset") or 0)
            pending = [u for u in self.updates if u["update_id"] >= offset]
            return 200, {"ok": True, "result": pending}
        if method == "sendMessage":
            if self.reject_html and body.get("parse_mode") == "HTML":
                return 400, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}
            return 200, {"ok": True, "result": {"message_id": len(self.calls)}}
        if method == "getMe":
            return 200, {"ok": True, "result": {"id": 123456, "username": "ccas_bot"}}
        if method == "getFile":
            if body.get("file_id") == "huge":
                return 400, {"ok": False, "error_code": 400, "description": "Bad Request: file is too big"}
            return 200, {"ok": True, "result": {"file_id": body["file_id"], "file_path": f"documents/{body['file_id']}.bin"}}
        if method == "sendDocument":
            return 200, {"ok": True, "result": {"message_id": 70}}
        if method == "sendMediaGroup":
            return 200, {"ok": True, "result": [{"message_id": 80 + n} for n in range(len(body["media"]))]}
        return 200, {"ok": True, "result": True}


class BotApi(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeApi()
        self.bot = telegram.Bot(TOKEN, api_root=self.api.root)

    def tearDown(self) -> None:
        self.api.close()

    def test_token_parts(self) -> None:
        self.assertEqual(telegram.bot_id(TOKEN), 123456)
        self.assertTrue(telegram.looks_like_token(TOKEN))
        self.assertFalse(telegram.looks_like_token("nope"))
        self.assertEqual(telegram.mask(TOKEN), "123456:…6789")
        self.assertEqual(self.bot.get_me()["username"], "ccas_bot")

    def test_polling_moves_the_offset_and_flattens_updates(self) -> None:
        self.api.updates = [
            {"update_id": 10, "message": {"message_id": 1, "chat": {"id": -5}, "from": {"id": 7, "username": "ann"}, "text": "hi", "message_thread_id": 3}},
            {"update_id": 11, "callback_query": {"id": "cb", "from": {"id": 7}, "data": "a:1:0:1", "message": {"message_id": 2, "chat": {"id": -5}}}},
            {"update_id": 12, "channel_post": {"text": "ignored"}},
        ]
        state = telegram.PollState()
        incoming = telegram.poll_once(self.bot, state, timeout=0)
        self.assertEqual(state.offset, 13)
        self.assertEqual([item.update_id for item in incoming], [10, 11])
        message, press = incoming
        self.assertEqual((message.chat_id, message.thread_id, message.user_id, message.text, message.username), (-5, 3, 7, "hi", "ann"))
        self.assertTrue(press.is_callback)
        self.assertEqual(press.callback_data, "a:1:0:1")
        self.assertEqual(press.message_id, 2)

        again = telegram.poll_once(self.bot, state, timeout=0)
        self.assertEqual(again, [])

    def test_a_second_poller_is_told_the_token_is_taken(self) -> None:
        self.api.busy = True
        with self.assertRaises(telegram.TokenBusy):
            telegram.poll_once(self.bot, telegram.PollState(), timeout=0)

    def test_long_text_is_split_and_html_falls_back_to_plain(self) -> None:
        text = "<b>head</b>\n" + "\n".join(f"line {n}" for n in range(1200))
        self.bot.send_message(-5, text, thread_id=3)
        sends = [body for method, body in self.api.calls if method == "sendMessage"]
        self.assertGreater(len(sends), 1)
        self.assertTrue(all(len(body["text"]) <= telegram.MESSAGE_LIMIT for body in sends))
        self.assertEqual(sends[0]["message_thread_id"], 3)
        self.assertEqual("".join(b["text"] for b in sends).replace("\n", ""), text.replace("\n", ""))

        self.api.calls.clear()
        self.api.reject_html = True
        self.bot.send_message(-5, "<b>bold</b> & <code>x</code>")
        sends = [body for method, body in self.api.calls if method == "sendMessage"]
        self.assertEqual(len(sends), 2)
        self.assertNotIn("parse_mode", sends[1])
        self.assertEqual(sends[1]["text"], "bold & x")

    def test_keyboard_trims_callback_data(self) -> None:
        markup = telegram.keyboard([[("ok", "x" * 100)], []])
        self.assertEqual(len(markup["inline_keyboard"]), 1)
        self.assertEqual(len(markup["inline_keyboard"][0][0]["callback_data"]), telegram.CALLBACK_DATA_LIMIT)


class Files(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeApi()
        self.bot = telegram.Bot(TOKEN, api_root=self.api.root)

    def tearDown(self) -> None:
        self.api.close()

    def test_attachments_are_read_off_every_kind_of_message(self) -> None:
        message = {
            "message_id": 4,
            "chat": {"id": -5},
            "from": {"id": 7},
            "caption": "/rik look",
            "media_group_id": "album1",
            "photo": [
                {"file_id": "small", "file_unique_id": "u1", "file_size": 10},
                {"file_id": "big", "file_unique_id": "u2", "file_size": 900},
            ],
            "document": {"file_id": "doc", "file_name": "report.pdf", "file_size": 1234},
            "voice": {"file_id": "v", "file_unique_id": "u3"},
        }
        parsed = telegram.parse_update({"update_id": 1, "message": message})
        assert parsed is not None
        self.assertEqual(parsed.text, "/rik look")
        self.assertEqual(parsed.media_group, "album1")
        self.assertEqual(
            parsed.files,
            (
                telegram.Attachment("big", "photo_u2.jpg", 900),
                telegram.Attachment("doc", "report.pdf", 1234),
                telegram.Attachment("v", "voice_u3.ogg", 0),
            ),
        )

    def test_an_update_survives_the_trip_through_the_daemon(self) -> None:
        # The daemon hands updates over as `asdict` + JSON.
        original = telegram.Incoming(
            update_id=1, chat_id=-5, thread_id=0, user_id=7, text="hi",
            files=(telegram.Attachment("doc", "a.txt", 3),), media_group="g",
        )
        raw = json.loads(json.dumps(dataclasses.asdict(original)))
        self.assertEqual(telegram.Incoming.from_dict(raw), original)
        # A daemon older than this build sends neither field.
        raw.pop("files")
        raw.pop("media_group")
        self.assertEqual(telegram.Incoming.from_dict(raw).files, ())

    def test_one_file_goes_as_a_document(self) -> None:
        message_id = self.bot.send_document(-5, 'strange "name".html', b"<p>hi</p>", thread_id=3)
        self.assertEqual(message_id, 70)
        method, body = self.api.calls[-1]
        self.assertEqual(method, "sendDocument")
        self.assertEqual(body["chat_id"], "-5")
        self.assertEqual(body["message_thread_id"], "3")
        self.assertEqual(body["_files"]["document"], ("strange 'name'.html", b"<p>hi</p>"))

    def test_several_files_go_as_one_album_of_documents(self) -> None:
        ids = self.bot.send_media_group(-5, [("a.html", b"a"), ("отчёт.pdf", b"bb")])
        self.assertEqual(ids, [80, 81])
        method, body = self.api.calls[-1]
        self.assertEqual(method, "sendMediaGroup")
        self.assertEqual(
            body["media"],
            [{"type": "document", "media": "attach://f0"}, {"type": "document", "media": "attach://f1"}],
        )
        self.assertEqual(body["_files"]["f0"], ("a.html", b"a"))
        self.assertEqual(body["_files"]["f1"], ("отчёт.pdf", b"bb"))

    def test_a_file_is_fetched_by_its_path(self) -> None:
        self.api.stored[f"/file/bot{TOKEN}/documents/doc.bin"] = b"payload"
        remote = self.bot.get_file("doc")
        self.assertEqual(remote, "documents/doc.bin")
        target = io.BytesIO()
        self.bot.download(remote, target)
        self.assertEqual(target.getvalue(), b"payload")

        with self.assertRaises(telegram.TelegramError):
            self.bot.get_file("huge")
        with self.assertRaises(telegram.TelegramError):
            self.bot.download("documents/missing.bin", io.BytesIO())


class Rendering(unittest.TestCase):
    def test_markdown_becomes_telegram_html(self) -> None:
        source = "# Title\nSome **bold** and `code <x>` here.\n- item a\n- item b\n```py\nprint('<hi>')\n```\n[docs](https://example.com/a?b=1)"
        rendered = telegram.markdown_to_html(source)
        self.assertIn("<b>Title</b>", rendered)
        self.assertIn("<b>bold</b>", rendered)
        self.assertIn("<code>code &lt;x&gt;</code>", rendered)
        self.assertIn("• item a", rendered)
        self.assertIn("<pre>print(&#x27;&lt;hi&gt;&#x27;)</pre>", rendered)
        self.assertIn('<a href="https://example.com/a?b=1">docs</a>', rendered)
        self.assertNotIn("```", rendered)

    def test_plain_angle_brackets_are_escaped(self) -> None:
        self.assertEqual(telegram.markdown_to_html("a < b > c & d"), "a &lt; b &gt; c &amp; d")

    def test_split_keeps_pre_blocks_balanced(self) -> None:
        body = "<pre>" + "\n".join("x" * 50 for _ in range(200)) + "</pre>"
        pieces = telegram.split_message(body, limit=2000)
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertEqual(piece.count("<pre>"), piece.count("</pre>"))
        self.assertEqual(telegram.split_message("", limit=10), [])
        self.assertEqual(telegram.split_message("short", limit=10), ["short"])

    def test_strip_html(self) -> None:
        self.assertEqual(telegram.strip_html("<b>a</b> &amp; <pre>b</pre>"), "a & b")


class Backlog(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeApi()
        self.bot = telegram.Bot(TOKEN, api_root=self.api.root)

    def tearDown(self) -> None:
        self.api.close()

    def test_skip_pending_starts_after_the_backlog(self) -> None:
        self.api.updates = [
            {"update_id": 5, "message": {"message_id": 1, "chat": {"id": -5}, "from": {"id": 7}, "text": "/claude"}},
            {"update_id": 6, "message": {"message_id": 2, "chat": {"id": -5}, "from": {"id": 7}, "text": "/claude"}},
        ]
        state = telegram.PollState()
        self.assertTrue(telegram.skip_pending(self.bot, state))
        self.assertEqual(state.offset, 7)
        self.assertEqual(telegram.poll_once(self.bot, state, timeout=0), [])

    def test_an_empty_queue_is_not_a_backlog(self) -> None:
        state = telegram.PollState()
        self.assertFalse(telegram.skip_pending(self.bot, state))
        self.assertEqual(state.offset, 0)
