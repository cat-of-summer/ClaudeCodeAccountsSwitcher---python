from __future__ import annotations

import time
from unittest import mock
from pathlib import Path
from typing import Any

from app.transport.conversation import ChatTarget, Conversation, resolve_dir
from app.transport.driver import Event
from app.transport.profiles import Profile
from app.transport.prompter import Prompter
from core import telegram
from core.store import Config
from tests.base import TempHome


class FakeBot:
    id = 42

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.cleared: list[int] = []
        self.deleted: set[int] = set()
        self.deleted_messages: list[int] = []
        self.answered: list[tuple[str, str]] = []

    def send_message(
        self, chat_id: int, text: str, *, thread_id: int = 0, reply_markup: Any = None, reply_to: int = 0, **_: Any
    ) -> int:
        self.sent.append(
            {
                "id": len(self.sent) + 1,
                "chat": chat_id,
                "text": text,
                "thread": thread_id,
                "markup": reply_markup,
                "reply_to": reply_to,
            }
        )
        return len(self.sent)

    def edit_markup(self, chat_id: int, message_id: int, reply_markup: Any) -> None:
        self.cleared.append(message_id)

    def edit_text(self, chat_id: int, message_id: int, text: str, **_: Any) -> bool:
        self.edits.append({"id": message_id, "text": text})
        if message_id in self.deleted:
            return False
        for entry in self.sent:
            if entry["id"] == message_id:
                entry["text"] = text
        return True

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answered.append((callback_id, text))

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted_messages.append(message_id)

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
        self.mode = ""
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
        self.mode = mode


def incoming(text: str, *, chat: int = -100, user: int = 7, thread: int = 0) -> telegram.Incoming:
    return telegram.Incoming(update_id=1, chat_id=chat, thread_id=thread, user_id=user, text=text, message_id=5)


def press(data: str, *, chat: int = -100) -> telegram.Incoming:
    return telegram.Incoming(update_id=2, chat_id=chat, thread_id=0, user_id=7, text="", message_id=6, callback_id="cb", callback_data=data)


class ConversationBase(TempHome):
    def setUp(self) -> None:
        super().setUp()
        Config().save()
        self.bot = FakeBot()
        self.driver = FakeDriver()
        self.console: list[str] = []

    def make(self, name: str = "default", user: int = 7, **fields: Any) -> Conversation:
        target = ChatTarget(bot=self.bot, chat=-100, user=user)  # type: ignore[arg-type]
        conversation = Conversation(
            Config.load(),
            slot=1,
            target=target,
            profile=Profile(name=name, **fields),
            cwd=self.home,
            args=[],
            on_console=self.console.append,
        )
        conversation.driver = self.driver  # type: ignore[assignment]
        conversation.prompter = Prompter(self.driver)  # type: ignore[arg-type]
        conversation.session_id = self.driver.session_id
        return conversation


class ChatConversation(ConversationBase):
    def test_what_it_is_given_goes_to_claude(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("hello"))
        self.assertEqual(self.driver.sent, ["hello"])

    def test_answers_reply_to_the_message_that_asked(self) -> None:
        conversation = self.make(expanded=True)
        conversation._on_incoming(incoming("hello"))
        conversation._on_event(Event("text", {"text": "hi"}))
        self.assertEqual(self.bot.sent[-1]["reply_to"], 5)
        # Only the first answer of a turn is a reply; the rest read as a thread.
        conversation._on_event(Event("text", {"text": "more"}))
        self.assertEqual(self.bot.sent[-1]["reply_to"], 0)

    def test_the_console_mirror_goes_through_the_callback(self) -> None:
        conversation = self.make(expanded=True)
        conversation._on_event(Event("text", {"text": "visible"}))
        self.assertIn("visible", self.console[-1])

    def test_own_commands(self) -> None:
        conversation = self.make("rikroot")
        conversation._on_incoming(incoming("/stop"))
        self.assertEqual(self.driver.interrupts, 1)
        conversation._on_incoming(incoming("/model opus"))
        self.assertEqual(self.driver.model, "opus")
        conversation._on_incoming(incoming("/pwd"))
        self.assertIn(str(self.home), self.bot.texts()[-1])
        conversation._on_incoming(incoming("/help"))
        self.assertIn("/switch", self.bot.texts()[-1])
        conversation._on_incoming(incoming("/compact"))  # not ours: straight to claude
        self.assertEqual(self.driver.sent, ["/compact"])
        conversation._on_incoming(incoming("/kill"))
        self.assertTrue(conversation._closing)

    def test_shell_lines_run_here_and_reach_claude_in_tui_shape(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("!echo hi"))
        self.assertIn("hi", self.bot.texts()[-1])
        self.assertEqual(len(self.driver.sent), 1)
        payload = self.driver.sent[0]
        self.assertTrue(payload.startswith("<bash-input>echo hi</bash-input><bash-stdout>"))
        self.assertIn("hi", payload)
        self.assertIn("<bash-stderr>", payload)

    def test_a_question_becomes_buttons_and_a_press_answers_it(self) -> None:
        conversation = self.make()
        question = {"question": "Colour?", "header": "C", "options": [{"label": "Red"}, {"label": "Blue"}], "multiSelect": False}
        conversation._on_event(Event("ask", {"request_id": "req_1", "tool_name": "AskUserQuestion", "input": {"questions": [question]}, "suggestions": []}))
        asked = self.bot.sent[-1]
        self.assertIn("Colour?", asked["text"])
        rows = asked["markup"]["inline_keyboard"]
        blue = rows[1][0]["callback_data"]
        conversation._on_incoming(press(blue))
        self.assertEqual(self.driver.responses[-1][0], "req_1")
        self.assertEqual(self.driver.responses[-1][1]["updatedInput"]["answers"], {"Colour?": "Blue"})  # type: ignore[index]
        self.assertEqual(self.bot.cleared, [len(self.bot.sent) - 1])
        self.assertIn("Blue", self.bot.texts()[-1])

    def test_a_typed_custom_answer_is_not_sent_as_a_prompt(self) -> None:
        conversation = self.make()
        question = {"question": "Name?", "options": [{"label": "A"}], "multiSelect": False}
        conversation._on_event(Event("ask", {"request_id": "req_2", "tool_name": "AskUserQuestion", "input": {"questions": [question]}, "suggestions": []}))
        custom = [b for row in self.bot.sent[-1]["markup"]["inline_keyboard"] for b in row if b["callback_data"].endswith(":t")][0]
        conversation._on_incoming(press(custom["callback_data"]))
        conversation._on_incoming(incoming("Bartholomew"))
        self.assertEqual(self.driver.sent, [])
        self.assertEqual(self.driver.responses[-1][1]["updatedInput"]["answers"], {"Name?": "Bartholomew"})  # type: ignore[index]

    def test_permission_prompt_from_the_console(self) -> None:
        conversation = self.make()
        conversation._on_event(Event("ask", {"request_id": "req_3", "tool_name": "Bash", "input": {"command": "ls"}, "suggestions": []}))
        conversation._on_line("n", source="console")
        self.assertEqual(self.driver.responses[-1][1]["behavior"], "deny")  # type: ignore[index]

    def test_a_chat_hears_answers_and_nothing_technical(self) -> None:
        conversation = self.make(expanded=True)
        conversation._on_event(Event("text", {"text": "**bold** answer"}))
        self.assertEqual(self.bot.texts()[-1], "<b>bold</b> answer")

        conversation._on_event(Event("tool_use", {"name": "Bash", "input": {"command": "git status"}}))
        conversation._on_event(Event("tool_result", {"id": "x", "content": "clean"}))
        conversation._on_event(Event("result", {"subtype": "success", "duration_ms": 1000, "cost": 0.1}))
        self.assertEqual(self.bot.texts()[-1], "<b>bold</b> answer")  # nothing new reached the chat
        self.assertTrue(any("git status" in line for line in self.console))

    def test_tech_turns_the_tool_log_back_on(self) -> None:
        conversation = self.make(expanded=True, tech=True)
        conversation._on_event(Event("tool_use", {"name": "Bash", "input": {"command": "git status"}}))
        self.assertIn("git status", self.bot.texts()[-1])
        conversation._on_event(Event("tool_result", {"id": "x", "content": "clean"}))
        self.assertIn("clean", self.bot.texts()[-1])


class CollapsedTurn(ConversationBase):
    """One message per turn, rewritten -- the default way a chat reads."""

    def test_the_turn_is_written_into_one_message(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("do it"))
        conversation._on_event(Event("text", {"text": "thinking"}))
        conversation._flush_live()
        self.assertEqual(len(self.bot.sent), 1)
        first = self.bot.sent[0]["id"]

        conversation._on_event(Event("text", {"text": "done, here is the review"}))
        conversation._flush_live()
        self.assertEqual(len(self.bot.sent), 1)  # still one message in the chat
        self.assertEqual(self.bot.sent[0]["text"], "done, here is the review")
        self.assertEqual(self.bot.edits[-1]["id"], first)

    def test_a_deleted_message_is_replaced_not_lost(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("do it"))
        conversation._on_event(Event("text", {"text": "first"}))
        conversation._flush_live()
        self.bot.deleted.add(self.bot.sent[0]["id"])

        conversation._on_event(Event("text", {"text": "second"}))
        conversation._flush_live()
        self.assertEqual(len(self.bot.sent), 2)
        self.assertEqual(self.bot.sent[-1]["text"], "second")

    def test_a_new_turn_takes_the_buttons_off_the_old_message(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("first turn"))
        conversation._on_event(Event("text", {"text": "answer"}))
        conversation._flush_live()
        live = self.bot.sent[0]["id"]

        conversation._on_incoming(incoming("second turn"))
        self.assertIn(live, self.bot.cleared)
        conversation._on_event(Event("text", {"text": "another answer"}))
        conversation._flush_live()
        self.assertEqual(len(self.bot.sent), 2)


class Modes(ConversationBase):
    def test_the_mode_is_announced_when_it_changes(self) -> None:
        conversation = self.make()
        conversation._on_event(Event("init", {"permission_mode": "plan", "model": "m"}))
        self.assertEqual(conversation.mode, "plan")
        self.assertEqual(self.bot.sent, [])  # the first report only corrects the console

        conversation._on_event(Event("init", {"permission_mode": "acceptEdits", "model": "m"}))
        self.assertEqual(self.bot.texts()[-1], "🔵 acceptEdits")

    def test_the_skip_flag_is_bypass_and_says_so(self) -> None:
        self.assertEqual(self.make().mode, "bypassPermissions")

    def test_commands_switch_it(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("/plan"))
        self.assertEqual(self.driver.mode, "plan")
        self.assertEqual(conversation.mode, "plan")
        conversation._on_incoming(incoming("/bypass"))
        self.assertEqual(self.driver.mode, "bypassPermissions")
        self.assertIn("bypassPermissions", self.bot.texts()[-1])

    def test_the_starting_mode_comes_from_the_settings(self) -> None:
        # Without the skip flag, which is a bypass of its own and wins.
        config = Config.load()
        config.default_args = []
        config.save()
        (self.home / ".claude").mkdir(exist_ok=True)
        (self.home / ".claude" / "settings.json").write_text(
            '{"permissions": {"defaultMode": "acceptEdits"}}', encoding="utf-8"
        )
        conversation = self.make()
        self.assertEqual(conversation.mode, "acceptEdits")

    def test_cd_requests_a_relaunch_and_save_writes_a_profile(self) -> None:
        conversation = self.make("rikroot")
        project = self.home / "proj"
        project.mkdir()
        conversation._on_incoming(incoming("/cd nowhere"))
        self.assertIsNone(conversation._relaunch)
        conversation._on_incoming(incoming("/cd proj"))
        assert conversation._relaunch is not None
        self.assertEqual(conversation._relaunch.cwd, project.resolve())

        conversation.cwd = project
        conversation._on_incoming(incoming("/save"))
        saved = Config.load().telegram["profiles"]["rikroot"]
        self.assertEqual(saved["chats"], [-100])
        self.assertEqual((saved["cwd"], saved["slot"]), (str(project), 1))

    def test_exit_is_reported_once(self) -> None:
        conversation = self.make()
        conversation._on_event(Event("exit", {"code": 1, "stderr": "boom"}))
        self.assertIn("boom", self.bot.texts()[-1])
        self.assertEqual(conversation.exit_code, 1)


class Directories(TempHome):
    def test_resolve_dir_respects_roots(self) -> None:
        inside = self.home / "a" / "b"
        inside.mkdir(parents=True)
        self.assertEqual(resolve_dir("a/b", self.home, roots=[]), inside.resolve())
        self.assertEqual(resolve_dir(str(inside), Path("/"), roots=[str(self.home)]), inside.resolve())
        self.assertIsNone(resolve_dir(str(inside), Path("/"), roots=[str(self.home / "elsewhere")]))
        self.assertIsNone(resolve_dir("missing", self.home, roots=[]))


class SwitchingSlots(ConversationBase):
    """A limit is announced with a button; the switch tidies the chat up."""

    def _two_slots(self) -> None:
        from core import store
        from core.store import Accounts, Slot

        accounts = Accounts()
        for number in (1, 2):
            self.write_credentials(store.creds_file(number))
            accounts.slots[number] = Slot(number=number, alias=f"s{number}")
        accounts.save()

    def test_the_limit_messages_go_and_one_line_stays(self) -> None:
        self._two_slots()
        config = Config.load()
        config.auto_switch = {**config.auto_switch, "resumePrompt": "carry on"}
        config.save()
        conversation = self.make()
        conversation.config = Config.load()

        with mock.patch.object(conversation, "_elect", return_value=2):
            conversation._on_event(Event("rate_limit", {"status": "rejected", "window": "five_hour"}))
        limit_message = self.bot.sent[-1]
        self.assertIsNotNone(limit_message["markup"])
        self.assertEqual(conversation._limit_messages, [limit_message["id"]])

        conversation._on_incoming(press(f"{conversation.tag}:sw:2"))
        self.assertIn(limit_message["id"], self.bot.deleted_messages)
        self.assertIn("2", self.bot.texts()[-1])
        self.assertIn("1", self.bot.texts()[-1])
        assert conversation._relaunch is not None
        self.assertEqual(conversation._relaunch.slot, 2)
        # The resumed session is told to go on, or it would sit idle until
        # the person wrote again.
        self.assertEqual(conversation._relaunch.prompt, "carry on")


class StartingOver(ConversationBase):
    """`/clear` is answered here: a fresh session, not a prompt to claude."""

    def test_clear_relaunches_with_a_new_session_id(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("/clear"))
        self.assertIn("🧹", self.bot.texts()[-1])
        self.assertEqual(self.driver.sent, [])
        assert conversation._relaunch is not None
        self.assertTrue(conversation._relaunch.fresh)
        self.assertIsNone(conversation._relaunch.cwd)
        self.assertIsNone(conversation._relaunch.slot)


class WorkStaysAtTheBottom(ConversationBase):
    """A question answered mid-turn is a record; the work moves below it."""

    def test_the_working_message_is_recreated_under_an_answered_question(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("do it"))
        conversation._on_event(Event("text", {"text": "half way"}))
        conversation._flush_live()
        old_live = self.bot.sent[-1]["id"]

        question = {"question": "Colour?", "options": [{"label": "Red"}, {"label": "Blue"}], "multiSelect": False}
        conversation._on_event(Event("ask", {"request_id": "q1", "tool_name": "AskUserQuestion", "input": {"questions": [question]}, "suggestions": []}))
        asked = self.bot.sent[-1]
        conversation._on_incoming(press(asked["markup"]["inline_keyboard"][1][0]["callback_data"]))

        # The old message is gone, the same text sits below the answered
        # question, and further work goes into the new one.
        self.assertIn(old_live, self.bot.deleted_messages)
        self.assertEqual(self.bot.sent[-1]["text"], "half way")
        self.assertNotEqual(self.bot.sent[-1]["id"], old_live)
        conversation._on_event(Event("text", {"text": "done"}))
        conversation._flush_live()
        self.assertEqual(self.bot.sent[-1]["text"], "done")
        self.assertEqual(self.bot.edits[-1]["id"], self.bot.sent[-1]["id"])

    def test_expanded_mode_leaves_messages_where_they_are(self) -> None:
        conversation = self.make(expanded=True)
        conversation._on_event(Event("text", {"text": "half way"}))
        question = {"question": "Colour?", "options": [{"label": "Red"}], "multiSelect": False}
        conversation._on_event(Event("ask", {"request_id": "q2", "tool_name": "AskUserQuestion", "input": {"questions": [question]}, "suggestions": []}))
        conversation._on_incoming(press(self.bot.sent[-1]["markup"]["inline_keyboard"][0][0]["callback_data"]))
        self.assertEqual(self.bot.deleted_messages, [])
