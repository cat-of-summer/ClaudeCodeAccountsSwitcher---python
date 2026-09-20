from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.transport.driver import Event
from app.transport.prompter import Prompter
from app.transport.session import ChatTarget, Session, resolve_dir
from core import telegram
from core.store import Config
from tests.base import TempHome


class FakeBot:
    id = 42

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.cleared: list[int] = []
        self.answered: list[tuple[str, str]] = []

    def send_message(self, chat_id: int, text: str, *, thread_id: int = 0, reply_markup: Any = None, **_: Any) -> int:
        self.sent.append({"chat": chat_id, "text": text, "thread": thread_id, "markup": reply_markup})
        return len(self.sent)

    def edit_markup(self, chat_id: int, message_id: int, reply_markup: Any) -> None:
        self.cleared.append(message_id)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answered.append((callback_id, text))

    def typing(self, chat_id: int, *, thread_id: int = 0) -> None:
        return

    def texts(self) -> list[str]:
        return [entry["text"] for entry in self.sent]


class FakeDriver:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.responses: list[tuple[str, dict[str, Any] | None]] = []
        self.interrupts = 0
        self.turn_active = False
        self.model = "fake"
        self.started_at = time.time()
        self.session_id = "0123456789abcdef"
        self.commands: list[dict[str, Any]] = []

    def send_user(self, text: str) -> None:
        self.sent.append(text)

    def respond(self, request_id: str, *, result: dict[str, Any] | None = None, error: str = "") -> None:
        self.responses.append((request_id, result))

    def interrupt(self) -> None:
        self.interrupts += 1

    def set_model(self, model: str) -> None:
        self.model = model

    def set_permission_mode(self, mode: str) -> None:
        return


def incoming(text: str, *, chat: int = -100, user: int = 7, thread: int = 0) -> telegram.Incoming:
    return telegram.Incoming(update_id=1, chat_id=chat, thread_id=thread, user_id=user, text=text, message_id=5)


def press(data: str, *, chat: int = -100) -> telegram.Incoming:
    return telegram.Incoming(update_id=2, chat_id=chat, thread_id=0, user_id=7, text="", message_id=6, callback_id="cb", callback_data=data)


class ChatSession(TempHome):
    def setUp(self) -> None:
        super().setUp()
        Config().save()
        self.bot = FakeBot()
        self.driver = FakeDriver()

    def make(self, alias: str = "", prefix: str = "", users: list[int] | None = None) -> Session:
        target = ChatTarget(bot=self.bot, chat=-100, users=users or [], prefix=prefix)  # type: ignore[arg-type]
        session = Session(Config.load(), slot=1, target=target, cwd=self.home, args=[], alias=alias, own_poller=False)
        session.driver = self.driver  # type: ignore[assignment]
        session.prompter = Prompter(self.driver)  # type: ignore[arg-type]
        session.session_id = self.driver.session_id
        return session

    def test_only_the_right_chat_and_users_reach_claude(self) -> None:
        session = self.make(users=[7])
        session._on_incoming(incoming("hello", chat=-200))
        session._on_incoming(incoming("hello", user=8))
        self.assertEqual(self.driver.sent, [])
        session._on_incoming(incoming("hello"))
        self.assertEqual(self.driver.sent, ["hello"])

    def test_prefix_and_alias_routing(self) -> None:
        session = self.make(alias="main", prefix="cc:")
        session._on_incoming(incoming("hello"))  # no prefix
        session._on_incoming(incoming("cc: hello"))  # prefix but no alias: not for a named session
        session._on_incoming(incoming("cc: other hello"))
        self.assertEqual(self.driver.sent, [])
        session._on_incoming(incoming("cc: main hello there"))
        session._on_incoming(incoming("main /claude -n x"))  # the daemon's, never ours
        self.assertEqual(self.driver.sent, ["hello there"])
        session._on_incoming(incoming("main"))  # a ping
        self.assertIn("main", self.bot.texts()[-1])

    def test_own_commands(self) -> None:
        session = self.make(alias="main")
        session._on_incoming(incoming("main /stop"))
        self.assertEqual(self.driver.interrupts, 1)
        session._on_incoming(incoming("main /model opus"))
        self.assertEqual(self.driver.model, "opus")
        session._on_incoming(incoming("main /pwd"))
        self.assertIn(str(self.home), self.bot.texts()[-1])
        session._on_incoming(incoming("main /help"))
        self.assertIn("/switch", self.bot.texts()[-1])
        session._on_incoming(incoming("main /compact"))  # not ours: straight to claude
        self.assertEqual(self.driver.sent, ["/compact"])
        session._on_incoming(incoming("main /kill"))
        self.assertTrue(session._closing)

    def test_shell_lines_run_here_and_reach_claude_in_tui_shape(self) -> None:
        session = self.make()
        session._on_incoming(incoming("!echo hi"))
        self.assertIn("hi", self.bot.texts()[-1])
        self.assertEqual(len(self.driver.sent), 1)
        payload = self.driver.sent[0]
        self.assertTrue(payload.startswith("<bash-input>echo hi</bash-input><bash-stdout>"))
        self.assertIn("hi", payload)
        self.assertIn("<bash-stderr>", payload)

    def test_a_question_becomes_buttons_and_a_press_answers_it(self) -> None:
        session = self.make()
        question = {"question": "Colour?", "header": "C", "options": [{"label": "Red"}, {"label": "Blue"}], "multiSelect": False}
        session._on_event(Event("ask", {"request_id": "req_1", "tool_name": "AskUserQuestion", "input": {"questions": [question]}, "suggestions": []}))
        asked = self.bot.sent[-1]
        self.assertIn("Colour?", asked["text"])
        rows = asked["markup"]["inline_keyboard"]
        blue = rows[1][0]["callback_data"]
        session._on_incoming(press(blue))
        self.assertEqual(self.driver.responses[-1][0], "req_1")
        self.assertEqual(self.driver.responses[-1][1]["updatedInput"]["answers"], {"Colour?": "Blue"})  # type: ignore[index]
        self.assertEqual(self.bot.cleared, [len(self.bot.sent) - 1])
        self.assertIn("Blue", self.bot.texts()[-1])

    def test_a_typed_custom_answer_is_not_sent_as_a_prompt(self) -> None:
        session = self.make()
        question = {"question": "Name?", "options": [{"label": "A"}], "multiSelect": False}
        session._on_event(Event("ask", {"request_id": "req_2", "tool_name": "AskUserQuestion", "input": {"questions": [question]}, "suggestions": []}))
        custom = [b for row in self.bot.sent[-1]["markup"]["inline_keyboard"] for b in row if b["callback_data"].endswith(":t")][0]
        session._on_incoming(press(custom["callback_data"]))
        session._on_incoming(incoming("Bartholomew"))
        self.assertEqual(self.driver.sent, [])
        self.assertEqual(self.driver.responses[-1][1]["updatedInput"]["answers"], {"Name?": "Bartholomew"})  # type: ignore[index]

    def test_permission_prompt_from_the_console(self) -> None:
        session = self.make()
        session._on_event(Event("ask", {"request_id": "req_3", "tool_name": "Bash", "input": {"command": "ls"}, "suggestions": []}))
        session._on_line("n", source="console")
        self.assertEqual(self.driver.responses[-1][1]["behavior"], "deny")  # type: ignore[index]

    def test_text_and_tools_are_rendered_by_verbosity(self) -> None:
        session = self.make()
        session._on_event(Event("text", {"text": "**bold** answer"}))
        self.assertEqual(self.bot.texts()[-1], "<b>bold</b> answer")
        session._on_event(Event("tool_use", {"name": "Bash", "input": {"command": "git status"}}))
        self.assertIn("git status", self.bot.texts()[-1])
        session._on_event(Event("tool_result", {"id": "x", "content": "clean"}))
        self.assertNotIn("clean", self.bot.texts()[-1])  # "tools" hides results
        session.verbosity = "text"
        session._on_event(Event("tool_use", {"name": "Bash", "input": {"command": "ls"}}))
        self.assertNotIn("ls", self.bot.texts()[-1])

    def test_cd_requests_a_relaunch_and_save_writes_a_profile(self) -> None:
        session = self.make(alias="main")
        project = self.home / "proj"
        project.mkdir()
        session._on_incoming(incoming("main /cd nowhere"))
        self.assertIsNone(session._relaunch)
        session._on_incoming(incoming("main /cd proj"))
        assert session._relaunch is not None
        self.assertEqual(session._relaunch.cwd, project.resolve())

        session.cwd = project
        session._on_incoming(incoming("main /save"))
        saved = Config.load().telegram["sessions"]["main"]
        self.assertEqual((saved["chat"], saved["cwd"], saved["slot"]), (-100, str(project), 1))

    def test_exit_is_reported_once(self) -> None:
        session = self.make()
        session._on_event(Event("exit", {"code": 1, "stderr": "boom"}))
        self.assertIn("boom", self.bot.texts()[-1])
        self.assertEqual(session.exit_code, 1)


class Directories(TempHome):
    def test_resolve_dir_respects_roots(self) -> None:
        inside = self.home / "a" / "b"
        inside.mkdir(parents=True)
        self.assertEqual(resolve_dir("a/b", self.home, roots=[]), inside.resolve())
        self.assertEqual(resolve_dir(str(inside), Path("/"), roots=[str(self.home)]), inside.resolve())
        self.assertIsNone(resolve_dir(str(inside), Path("/"), roots=[str(self.home / "elsewhere")]))
        self.assertIsNone(resolve_dir("missing", self.home, roots=[]))
