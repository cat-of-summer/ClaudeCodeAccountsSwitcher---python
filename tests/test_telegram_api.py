from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from core import telegram

TOKEN = "123456:ABCDEFghijklmnopqrstuvwxyz0123456789"


class FakeApi:
    """Just enough of api.telegram.org to drive the client through."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []
        self.busy = False
        self.reject_html = False
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                method = self.path.rsplit("/", 1)[-1]
                api.calls.append((method, body))
                status, payload = api.answer(method, body)
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

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
