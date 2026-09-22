from __future__ import annotations

import time
from unittest import mock
from pathlib import Path
from typing import Any

from app import autoswitch
from app.transport.conversation import ChatTarget, Conversation, mode_mark, resolve_dir
from app.transport.driver import Event
from app.transport.profiles import Profile
from app.transport.prompter import Prompter
from core import telegram
from core.store import Config
from tests.base import TempHome


def mark(conversation: Conversation) -> str:
    """Every answer opens with the mark of the mode it was written in."""
    return f"{mode_mark(conversation.mode)} "


def free(slot: int) -> autoswitch.Election:
    """An election that says `slot` can take work right now."""
    return autoswitch.Election(slot, "limits")


def opens_at(slot: int, moment: float) -> autoswitch.Election:
    """An election that says `slot` is the first to come back, at `moment`."""
    return autoswitch.Election(slot, "waiting", available_at=moment)


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

    def make(self, alias: str = "bot", user: int = 7, **fields: Any) -> Conversation:
        target = ChatTarget(bot=self.bot, chat=-100, user=user)  # type: ignore[arg-type]
        conversation = Conversation(
            Config.load(),
            slot=1,
            target=target,
            profile=Profile(id=fields.pop("id", 1), alias=alias, **fields),
            cwd=self.home,
            args=[],
            on_console=self.console.append,
        )
        conversation.driver = self.driver  # type: ignore[assignment]
        conversation.prompter = Prompter(self.driver, bypass_available=conversation.bypass_available)  # type: ignore[arg-type]
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
        # The answer is written under the question, in the same message.
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(self.bot.edits[-1]["id"], asked["id"])
        self.assertTrue(asked["text"].startswith("❓"))
        self.assertIn("\n\n✅ Colour? — <b>Blue</b>", asked["text"])
        self.assertEqual(self.bot.deleted_messages, [])

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
        self.assertEqual(self.bot.texts()[-1], mark(conversation) + "<b>bold</b> answer")

        conversation._on_event(Event("tool_use", {"name": "Bash", "input": {"command": "git status"}}))
        conversation._on_event(Event("tool_result", {"id": "x", "content": "clean"}))
        conversation._on_event(Event("result", {"subtype": "success", "duration_ms": 1000, "cost": 0.1}))
        self.assertEqual(self.bot.texts()[-1], mark(conversation) + "<b>bold</b> answer")  # nothing new reached the chat
        self.assertTrue(any("git status" in line for line in self.console))

    def test_debug_turns_the_tool_log_back_on(self) -> None:
        conversation = self.make(expanded=True, debug=True)
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
        self.assertEqual(self.bot.sent[0]["text"], mark(conversation) + "done, here is the review")
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
        self.assertEqual(self.bot.sent[-1]["text"], mark(conversation) + "second")

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
    def test_a_change_claude_made_is_announced_only_in_debug(self) -> None:
        conversation = self.make()
        conversation._on_event(Event("init", {"permission_mode": "plan", "model": "m"}))
        self.assertEqual(conversation.mode, "plan")
        self.assertEqual(self.bot.sent, [])  # the first report only corrects the console

        conversation._on_event(Event("init", {"permission_mode": "acceptEdits", "model": "m"}))
        self.assertEqual(conversation.mode, "acceptEdits")
        self.assertEqual(self.bot.sent, [])  # a chat without debug hears only what it asked for
        self.assertIn("acceptEdits", self.console[-1])

        loud = self.make(debug=True)
        loud._on_event(Event("init", {"permission_mode": "plan", "model": "m"}))
        loud._on_event(Event("init", {"permission_mode": "acceptEdits", "model": "m"}))
        self.assertEqual(self.bot.texts()[-1], "🔵 acceptEdits")

    def test_a_subagents_mode_is_not_the_sessions(self) -> None:
        from core.hookbus import HookEvent

        conversation = self.make(debug=True)
        conversation._on_event(Event("init", {"permission_mode": "plan", "model": "m"}))
        conversation._on_hook(HookEvent("PreToolUse", {"permission_mode": "dontAsk", "agent_id": "a1"}))
        self.assertTrue(conversation._inbox.empty())
        conversation._on_hook(HookEvent("PreToolUse", {"permission_mode": "acceptEdits"}))
        self.assertEqual(conversation._inbox.get_nowait(), ("mode", "acceptEdits"))

    def test_the_skip_flag_is_bypass_and_says_so(self) -> None:
        self.assertEqual(self.make().mode, "bypassPermissions")

    def test_commands_switch_it_and_say_so(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("/plan"))
        self.assertEqual(self.driver.mode, "plan")
        self.assertEqual(conversation.mode, "plan")
        self.assertEqual(self.bot.texts()[-1], "🟡 Mode chosen: <b>plan</b>")
        conversation._on_incoming(incoming("/bypass"))
        self.assertEqual(self.driver.mode, "bypassPermissions")
        self.assertIn("bypassPermissions", self.bot.texts()[-1])
        # claude confirming the switch through a hook is not news.
        conversation._note_mode("bypassPermissions")
        self.assertEqual(len(self.bot.sent), 2)

    def test_a_mode_switch_with_a_prompt_says_it_is_starting(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("/plan Давай сделаем"))
        self.assertEqual(self.driver.mode, "plan")
        self.assertEqual(self.bot.texts()[-1], "🟡 Mode chosen: <b>plan</b>, starting work…")
        self.assertEqual(self.driver.sent, ["Давай сделаем"])

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

        # As if the relaunch had happened.
        conversation._relaunch = None
        conversation.cwd = project
        conversation._on_incoming(incoming("/save"))
        saved = Config.load().telegram["profiles"]["1"]
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

        with mock.patch.object(conversation, "_elect", return_value=free(2)):
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


class WaitingOutALimit(ConversationBase):
    """Nobody is free: the session parks on the slot that opens first."""

    def _parked(self, slot: int = 2, seconds: float = 1800.0) -> Conversation:
        conversation = self.make()
        conversation.config = Config.load()
        with mock.patch.object(
            conversation, "_elect", return_value=opens_at(slot, time.time() + seconds)
        ):
            conversation._on_event(Event("rate_limit", {"status": "rejected", "window": "five_hour"}))
        return conversation

    def test_the_relaunch_is_scheduled_and_the_loop_stays_put(self) -> None:
        conversation = self._parked()
        assert conversation._relaunch is not None
        self.assertEqual(conversation._relaunch.slot, 2)
        self.assertTrue(conversation._relaunch.pending())
        # Not due yet: claude stays up and the chat keeps working.
        self.assertFalse(conversation._due())
        self.assertIn("⏳", self.bot.texts()[-1])

    def test_waiting_for_a_reset_is_not_the_chat_going_quiet(self) -> None:
        """The delay is ours, so it must not count towards the idle close."""
        conversation = self._parked()
        conversation.last_seen = time.time() - 7200
        conversation._tick()
        self.assertAlmostEqual(conversation.last_seen, time.time(), delta=5)

    def test_a_line_typed_meanwhile_becomes_the_first_prompt(self) -> None:
        conversation = self._parked()
        conversation._on_incoming(incoming("first thought"))
        conversation._on_incoming(incoming("no, this one"))
        assert conversation._relaunch is not None
        self.assertEqual(conversation._relaunch.prompt, "no, this one")
        self.assertEqual(self.driver.sent, [])  # claude is walled; nothing goes in

    def test_commands_still_answer_while_parked(self) -> None:
        conversation = self._parked()
        conversation._on_incoming(incoming("/status"))
        self.assertIn("2", self.bot.texts()[-1])
        assert conversation._relaunch is not None
        self.assertEqual(conversation._relaunch.after, [])

    def test_an_explicit_switch_cancels_the_wait(self) -> None:
        self._two_slots()
        conversation = self._parked()
        conversation._on_incoming(incoming("/switch 2"))
        assert conversation._relaunch is not None
        self.assertEqual(conversation._relaunch.slot, 2)
        self.assertFalse(conversation._relaunch.pending())
        self.assertTrue(conversation._due())

    def test_when_the_moment_comes_the_relaunch_goes_through(self) -> None:
        conversation = self._parked()
        parked_message = self.bot.sent[-1]["id"]
        assert conversation._relaunch is not None
        conversation._relaunch.not_before = time.time() - 1
        conversation._tick()
        self.assertTrue(conversation._due())
        self.assertEqual(conversation._relaunch.not_before, 0.0)
        # The countdown message has done its job; one line says what happened.
        self.assertIn(parked_message, self.bot.deleted_messages)
        self.assertEqual(conversation._limit_messages, [])

    def _two_slots(self) -> None:
        from core import store
        from core.store import Accounts, Slot

        accounts = Accounts()
        for number in (1, 2):
            self.write_credentials(store.creds_file(number))
            accounts.slots[number] = Slot(number=number, alias=f"s{number}")
        accounts.save()


class StartingOver(ConversationBase):
    """`/clear` is answered here: a fresh session, not a prompt to claude."""

    def test_clear_relaunches_with_a_new_session_id(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("/new"))
        self.assertIn("🧹", self.bot.texts()[-1])
        self.assertEqual(self.driver.sent, [])
        assert conversation._relaunch is not None
        self.assertTrue(conversation._relaunch.fresh)
        self.assertIsNone(conversation._relaunch.cwd)
        self.assertIsNone(conversation._relaunch.slot)


class ServerAsksSomething(ConversationBase):
    """An MCP elicitation reaches the chat instead of stalling the session."""

    def test_the_question_gets_buttons_and_the_answer_goes_back(self) -> None:
        conversation = self.make()
        conversation._on_event(
            Event(
                "elicit",
                {
                    "request_id": "e1",
                    "server": "registry",
                    "message": "Дать доступ к проекту?",
                    "mode": "form",
                    "schema": {"type": "object", "properties": {"approve": {"type": "boolean", "title": "Разрешить?"}}},
                },
            )
        )
        shown = self.bot.sent[-1]
        self.assertIn("Дать доступ", shown["text"])
        self.assertIsNotNone(shown["markup"])
        data = shown["markup"]["inline_keyboard"][0][0]["callback_data"]

        conversation._on_incoming(press(data))
        self.assertEqual(self.driver.responses[-1], ("e1", {"action": "accept", "content": {"approve": True}}))


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
        self.assertEqual(self.bot.sent[-1]["text"], mark(conversation) + "half way")
        self.assertNotEqual(self.bot.sent[-1]["id"], old_live)
        conversation._on_event(Event("text", {"text": "done"}))
        conversation._flush_live()
        self.assertEqual(self.bot.sent[-1]["text"], mark(conversation) + "done")
        self.assertEqual(self.bot.edits[-1]["id"], self.bot.sent[-1]["id"])

    def test_expanded_mode_leaves_messages_where_they_are(self) -> None:
        conversation = self.make(expanded=True)
        conversation._on_event(Event("text", {"text": "half way"}))
        question = {"question": "Colour?", "options": [{"label": "Red"}], "multiSelect": False}
        conversation._on_event(Event("ask", {"request_id": "q2", "tool_name": "AskUserQuestion", "input": {"questions": [question]}, "suggestions": []}))
        conversation._on_incoming(press(self.bot.sent[-1]["markup"]["inline_keyboard"][0][0]["callback_data"]))
        self.assertEqual(self.bot.deleted_messages, [])


class SeveralCommandsInOneLine(ConversationBase):
    """`/clear /plan Давай…` is three things done in order, not one prompt."""

    def test_clear_then_mode_then_prompt_ride_the_relaunch(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("/new /plan Давай сделаем"))
        relaunch = conversation._relaunch
        assert relaunch is not None
        self.assertTrue(relaunch.fresh)
        self.assertEqual(relaunch.mode, "plan")
        self.assertEqual(relaunch.prompt, "Давай сделаем")
        # Nothing reached the claude that is about to go.
        self.assertEqual(self.driver.sent, [])
        self.assertEqual(self.driver.mode, "")
        self.assertIn("🧹", self.bot.texts()[-2])
        self.assertEqual(self.bot.texts()[-1], "🟡 Mode chosen: <b>plan</b>, starting work…")

    def test_commands_alone_end_with_the_last_one(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("/new /plan "))
        relaunch = conversation._relaunch
        assert relaunch is not None
        self.assertEqual((relaunch.fresh, relaunch.mode, relaunch.prompt), (True, "plan", ""))
        self.assertEqual(self.bot.texts()[-1], "🟡 Mode chosen: <b>plan</b>")

    def test_cd_and_clear_make_one_relaunch(self) -> None:
        conversation = self.make()
        project = self.home / "proj"
        project.mkdir()
        conversation._on_incoming(incoming("/new /cd proj /status"))
        relaunch = conversation._relaunch
        assert relaunch is not None
        self.assertTrue(relaunch.fresh)
        self.assertEqual(relaunch.cwd, project.resolve())
        self.assertEqual(relaunch.after, ["/status"])

    def test_without_a_relaunch_the_commands_simply_run_in_order(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("/plan /model opus fix it"))
        self.assertEqual(self.driver.mode, "plan")
        self.assertEqual(self.driver.model, "opus")
        self.assertEqual(self.driver.sent, ["fix it"])
        self.assertIsNone(conversation._relaunch)

    def test_the_relaunch_starts_claude_in_the_mode_and_feeds_the_rest(self) -> None:
        conversation = self.make()
        conversation._start_mode = "plan"
        conversation._opening_lines = ["/status"]
        conversation._opening_prompt = "go"
        started: list[Any] = []

        class Started:
            session_id = "abc"

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                started.append(kwargs["args"])

            def start(self) -> None:
                return

            def close(self) -> None:
                return

            def send_user(self, text: str) -> None:
                started.append(text)

        with mock.patch("app.transport.conversation.Driver", Started), \
             mock.patch("app.transport.conversation.wrapper.wants_hook_bus", return_value=False), \
             mock.patch("app.transport.conversation.wrapper.close_hook_bus"), \
             mock.patch("app.transport.conversation.update_session"), \
             mock.patch.object(conversation, "_loop", side_effect=lambda: None), \
             mock.patch.object(conversation, "_on_line") as on_line:
            conversation._run_on_slot()
        self.assertEqual(started[0][:2], ["--permission-mode", "plan"])
        self.assertEqual(started[1], "go")
        on_line.assert_called_once_with("/status", source="relaunch")
        self.assertEqual(conversation._start_mode, "")


class InterruptingWithABang(ConversationBase):
    def test_an_urgent_line_stops_the_turn_first(self) -> None:
        conversation = self.make()
        self.driver.turn_active = True
        conversation._on_incoming(telegram.Incoming(update_id=1, chat_id=-100, thread_id=0, user_id=7, text="stop, wrong file", message_id=5, urgent=True))
        self.assertEqual(self.driver.interrupts, 1)
        self.assertEqual(self.driver.sent, ["stop, wrong file"])
        self.assertEqual(self.bot.sent, [])  # the interruption is not announced outside debug

    def test_nothing_to_interrupt_means_nothing_happens(self) -> None:
        conversation = self.make()
        conversation._on_incoming(telegram.Incoming(update_id=1, chat_id=-100, thread_id=0, user_id=7, text="hello", message_id=5, urgent=True))
        self.assertEqual(self.driver.interrupts, 0)
        self.assertEqual(self.driver.sent, ["hello"])


class OtherPeoplesButtons(ConversationBase):
    def test_a_press_from_someone_else_is_refused(self) -> None:
        conversation = self.make(user=7)
        conversation._on_event(Event("ask", {"request_id": "req_9", "tool_name": "Bash", "input": {"command": "ls"}, "suggestions": []}))
        allow = self.bot.sent[-1]["markup"]["inline_keyboard"][0][0]["callback_data"]
        stranger = telegram.Incoming(update_id=2, chat_id=-100, thread_id=0, user_id=8, text="", message_id=6, callback_id="cb", callback_data=allow)
        conversation._on_incoming(stranger)
        self.assertEqual(self.driver.responses, [])
        self.assertIn("somebody else", self.bot.answered[-1][1])
        conversation._on_incoming(press(allow))
        self.assertEqual(self.driver.responses[-1][1]["behavior"], "allow")  # type: ignore[index]

    def test_a_shared_session_takes_anyones_press(self) -> None:
        conversation = self.make(user=0, multi=True)
        conversation._on_event(Event("ask", {"request_id": "req_9", "tool_name": "Bash", "input": {"command": "ls"}, "suggestions": []}))
        allow = self.bot.sent[-1]["markup"]["inline_keyboard"][0][0]["callback_data"]
        conversation._on_incoming(telegram.Incoming(update_id=2, chat_id=-100, thread_id=0, user_id=8, text="", message_id=6, callback_id="cb", callback_data=allow))
        self.assertEqual(self.driver.responses[-1][1]["behavior"], "allow")  # type: ignore[index]


class WhatBecomesOfAnAnsweredPrompt(ConversationBase):
    def _ask_permission(self, conversation: Conversation) -> dict[str, Any]:
        conversation._on_event(Event("ask", {"request_id": "p1", "tool_name": "Edit", "input": {"file_path": "a.py", "new_string": "x"}, "suggestions": []}))
        return self.bot.sent[-1]

    def test_a_permission_is_taken_down_and_the_work_stays_put(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("do it"))
        conversation._on_event(Event("text", {"text": "half way"}))
        conversation._flush_live()
        live = self.bot.sent[-1]["id"]

        asked = self._ask_permission(conversation)
        conversation._on_incoming(press(asked["markup"]["inline_keyboard"][0][0]["callback_data"]))
        self.assertEqual(self.driver.responses[-1][1]["behavior"], "allow")  # type: ignore[index]
        self.assertEqual(self.bot.deleted_messages, [asked["id"]])
        self.assertEqual(self.bot.sent[-1]["id"], asked["id"])  # nothing new was sent
        self.assertNotIn(live, self.bot.deleted_messages)  # and the working message was not moved
        self.assertEqual(conversation._live_id, live)

    def test_in_debug_the_permission_stays_with_its_answer(self) -> None:
        conversation = self.make(debug=True)
        asked = self._ask_permission(conversation)
        conversation._on_incoming(press(asked["markup"]["inline_keyboard"][1][0]["callback_data"]))
        self.assertEqual(self.bot.deleted_messages, [])
        self.assertIn("\n\n⛔ Edit", asked["text"])

    def test_an_mcp_request_is_taken_down_too(self) -> None:
        conversation = self.make()
        conversation._on_event(Event("elicit", {"request_id": "e1", "server": "registry", "message": "ok?", "mode": "form", "schema": {}}))
        asked = self.bot.sent[-1]
        conversation._on_incoming(press(asked["markup"]["inline_keyboard"][0][0]["callback_data"]))
        self.assertEqual(self.bot.deleted_messages, [asked["id"]])

    def test_a_multi_select_toggle_redraws_the_keyboard_in_place(self) -> None:
        conversation = self.make()
        question = {"question": "Which?", "options": [{"label": "A"}, {"label": "B"}], "multiSelect": True}
        conversation._on_event(Event("ask", {"request_id": "q3", "tool_name": "AskUserQuestion", "input": {"questions": [question]}, "suggestions": []}))
        asked = self.bot.sent[-1]
        conversation._on_incoming(press(asked["markup"]["inline_keyboard"][0][0]["callback_data"]))
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(self.bot.cleared, [asked["id"]])  # the fake records every markup edit here
        self.assertEqual(self.bot.edits, [])

    def test_a_withdrawn_permission_disappears_with_the_request(self) -> None:
        conversation = self.make()
        asked = self._ask_permission(conversation)
        conversation._on_event(Event("cancel", {"request_id": "p1"}))
        self.assertEqual(self.bot.deleted_messages, [asked["id"]])


class PlanModeWithBypass(ConversationBase):
    def test_tool_permissions_are_granted_without_asking(self) -> None:
        conversation = self.make()
        self.assertTrue(conversation.bypass_available)
        conversation._on_incoming(incoming("/plan"))
        conversation._on_event(Event("ask", {"request_id": "p2", "tool_name": "Bash", "input": {"command": "ls"}, "suggestions": []}))
        self.assertEqual(self.driver.responses[-1], ("p2", {"behavior": "allow", "updatedInput": {"command": "ls"}}))
        self.assertEqual(self.bot.sent[-1]["markup"], None)  # no buttons went out
        self.assertTrue(any("🔓" in line for line in self.console))

    def test_questions_and_the_plan_itself_still_reach_the_person(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("/plan"))
        question = {"question": "Colour?", "options": [{"label": "Red"}], "multiSelect": False}
        conversation._on_event(Event("ask", {"request_id": "q4", "tool_name": "AskUserQuestion", "input": {"questions": [question]}, "suggestions": []}))
        conversation._on_event(Event("ask", {"request_id": "x1", "tool_name": "ExitPlanMode", "input": {"plan": "do things"}, "suggestions": []}))
        self.assertEqual(self.driver.responses, [])
        self.assertEqual(len([m for m in self.bot.sent if m["markup"]]), 2)

    def test_without_bypass_plan_mode_asks_as_claude_does(self) -> None:
        config = Config.load()
        config.default_args = []
        config.save()
        conversation = self.make()
        self.assertFalse(conversation.bypass_available)
        conversation._on_incoming(incoming("/plan"))
        conversation._on_event(Event("ask", {"request_id": "p3", "tool_name": "Bash", "input": {"command": "ls"}, "suggestions": []}))
        self.assertEqual(self.driver.responses, [])
        self.assertIsNotNone(self.bot.sent[-1]["markup"])

    def test_approving_the_plan_goes_back_to_bypass(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("/plan"))
        conversation._on_event(Event("ask", {"request_id": "x2", "tool_name": "ExitPlanMode", "input": {"plan": "do things"}, "suggestions": []}))
        asked = self.bot.sent[-1]
        go = asked["markup"]["inline_keyboard"][0][0]
        self.assertIn("bypass", go["text"])
        conversation._on_incoming(press(go["callback_data"]))
        result = self.driver.responses[-1][1]
        self.assertEqual(result["updatedPermissions"][0]["mode"], "bypassPermissions")  # type: ignore[index]
        self.assertEqual(conversation.mode, "bypassPermissions")
        self.assertIn("bypassPermissions", asked["text"])  # the plan stays, the decision under it


class TheModeIsOnEveryAnswer(ConversationBase):
    """No greeting carries it any more, so each answer says where it stands."""

    def test_the_mark_is_the_mode_the_answer_was_written_in(self) -> None:
        conversation = self.make(expanded=True)
        conversation._on_event(Event("text", {"text": "first"}))
        self.assertEqual(self.bot.texts()[-1], "🔴 first")  # bypass: the test config skips permissions

        conversation._on_incoming(incoming("/plan"))
        conversation._on_event(Event("text", {"text": "second"}))
        self.assertEqual(self.bot.texts()[-1], "🟡 second")

        conversation._on_incoming(incoming("/edits"))
        conversation._on_event(Event("text", {"text": "third"}))
        self.assertEqual(self.bot.texts()[-1], "🔵 third")

    def test_the_collapsed_message_is_remarked_as_the_mode_changes(self) -> None:
        conversation = self.make()
        conversation._on_event(Event("text", {"text": "working"}))
        conversation._flush_live()
        self.assertEqual(self.bot.sent[-1]["text"], "🔴 working")

        conversation.mode = "plan"
        conversation._on_event(Event("text", {"text": "still working"}))
        conversation._flush_live()
        self.assertEqual(self.bot.sent[-1]["text"], "🟡 still working")

    def test_prompts_keep_their_own_marks(self) -> None:
        conversation = self.make()
        conversation._on_event(Event("ask", {"request_id": "m1", "tool_name": "Bash", "input": {"command": "ls"}, "suggestions": []}))
        self.assertTrue(self.bot.texts()[-1].startswith("🔐"))

    def test_help_lists_the_modes_with_their_marks(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("/help"))
        text = self.bot.texts()[-1]
        self.assertIn("🟡 /plan", text)
        self.assertIn("🔴 /bypass", text)


class ThePlanStaysWhereItWasWritten(ConversationBase):
    def test_the_working_message_holding_the_plan_is_kept(self) -> None:
        conversation = self.make()
        conversation._on_incoming(incoming("what would you do?"))
        conversation._on_event(Event("text", {"text": "Here is the plan: rewrite the parser"}))
        conversation._flush_live()
        written = self.bot.sent[-1]["id"]

        conversation._on_event(Event("ask", {"request_id": "x1", "tool_name": "ExitPlanMode", "input": {"plan": "rewrite the parser"}, "suggestions": []}))
        asked = self.bot.sent[-1]
        conversation._on_incoming(press(asked["markup"]["inline_keyboard"][0][0]["callback_data"]))

        # Neither message moved, and the work that follows starts its own.
        self.assertEqual(self.bot.deleted_messages, [])
        conversation._on_event(Event("text", {"text": "done"}))
        conversation._flush_live()
        self.assertNotEqual(self.bot.sent[-1]["id"], written)
        self.assertIn("rewrite the parser", [entry["text"] for entry in self.bot.sent if entry["id"] == written][0])

    def test_a_plan_with_nothing_written_before_it_needs_no_keeping(self) -> None:
        conversation = self.make()
        conversation._on_event(Event("ask", {"request_id": "x2", "tool_name": "ExitPlanMode", "input": {"plan": "do it"}, "suggestions": []}))
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(self.bot.deleted_messages, [])


class TidyingUpTheChat(ConversationBase):
    """`/new` starts over, `/clear` also empties the chat, `/exit` leaves the record."""

    def _worked(self, conversation: Conversation) -> dict[str, int]:
        """A turn with a plan, a permission, an answer and a mode line."""
        conversation._on_incoming(incoming("do it"))
        conversation._on_event(Event("text", {"text": "the plan"}))
        conversation._flush_live()
        plan_text = self.bot.sent[-1]["id"]
        conversation._on_event(Event("ask", {"request_id": "x9", "tool_name": "ExitPlanMode", "input": {"plan": "do it"}, "suggestions": []}))
        plan_prompt = self.bot.sent[-1]["id"]
        conversation._on_incoming(press(self.bot.sent[-1]["markup"]["inline_keyboard"][0][1]["callback_data"]))
        conversation._on_incoming(incoming("/status"))
        service = self.bot.sent[-1]["id"]
        conversation._on_event(Event("text", {"text": "all done"}))
        conversation._flush_live()
        answer = self.bot.sent[-1]["id"]
        return {"plan_text": plan_text, "plan_prompt": plan_prompt, "service": service, "answer": answer}

    def test_new_starts_a_fresh_session_and_leaves_the_chat_alone(self) -> None:
        conversation = self.make()
        self._worked(conversation)
        before = len(self.bot.sent)
        conversation._on_incoming(incoming("/new"))
        self.assertEqual(self.bot.deleted_messages, [])
        self.assertIn("🧹", self.bot.texts()[-1])
        self.assertEqual(len(self.bot.sent), before + 1)
        assert conversation._relaunch is not None
        self.assertTrue(conversation._relaunch.fresh)

    def test_clear_takes_everything_down_and_starts_a_fresh_session(self) -> None:
        conversation = self.make()
        shown = self._worked(conversation)
        conversation._on_incoming(incoming("/clear"))
        self.assertEqual(sorted(self.bot.deleted_messages), sorted(shown.values()))
        assert conversation._relaunch is not None
        self.assertTrue(conversation._relaunch.fresh)
        self.assertEqual(conversation._messages, [])
        self.assertEqual(conversation._live_id, 0)

    def test_exit_keeps_the_plan_and_the_last_answer_and_closes(self) -> None:
        conversation = self.make()
        shown = self._worked(conversation)
        conversation._on_incoming(incoming("/exit"))
        self.assertTrue(conversation._closing)
        self.assertEqual(
            sorted(self.bot.deleted_messages), sorted([shown["service"]])
        )
        kept = [message_id for message_id, _ in conversation._messages]
        self.assertEqual(kept, sorted([shown["plan_text"], shown["plan_prompt"], shown["answer"]]))
        # Nothing is said on the way out: the chat is meant to be left tidy.
        self.assertNotIn("👋", self.bot.texts()[-1])

    def test_exit_after_an_interrupted_turn_keeps_only_the_plan(self) -> None:
        conversation = self.make()
        conversation._on_event(Event("ask", {"request_id": "x8", "tool_name": "ExitPlanMode", "input": {"plan": "do it"}, "suggestions": []}))
        plan_prompt = self.bot.sent[-1]["id"]
        conversation._on_incoming(press(self.bot.sent[-1]["markup"]["inline_keyboard"][1][0]["callback_data"]))
        conversation._on_incoming(incoming("/exit"))
        self.assertEqual([message_id for message_id, _ in conversation._messages], [plan_prompt])

    def test_an_answered_permission_is_gone_before_the_tidy_up(self) -> None:
        conversation = self.make()
        conversation._on_event(Event("ask", {"request_id": "p9", "tool_name": "Bash", "input": {"command": "ls"}, "suggestions": []}))
        asked = self.bot.sent[-1]["id"]
        conversation._on_incoming(press(self.bot.sent[-1]["markup"]["inline_keyboard"][0][0]["callback_data"]))
        self.assertEqual(self.bot.deleted_messages, [asked])
        conversation._on_incoming(incoming("/clear"))
        # Taken down once, not twice.
        self.assertEqual(self.bot.deleted_messages.count(asked), 1)
