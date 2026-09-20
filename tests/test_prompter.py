from __future__ import annotations

import time
from typing import Any

from app.transport import prompter as prompter_module
from app.transport.driver import Event
from app.transport.prompter import Prompter
from tests.base import TempHome


class FakeDriver:
    def __init__(self) -> None:
        self.responses: list[tuple[str, dict[str, Any] | None, str]] = []

    def respond(self, request_id: str, *, result: dict[str, Any] | None = None, error: str = "") -> None:
        self.responses.append((request_id, result, error))


def ask(request_id: str, questions: list[dict[str, Any]]) -> Event:
    return Event(
        "ask",
        {
            "request_id": request_id,
            "tool_name": "AskUserQuestion",
            "input": {"questions": questions},
            "interactive": True,
            "suggestions": [],
        },
    )


def permission(request_id: str, tool: str, tool_input: dict[str, Any], suggestions: list | None = None) -> Event:
    return Event(
        "ask",
        {"request_id": request_id, "tool_name": tool, "input": tool_input, "suggestions": suggestions or []},
    )


COLOUR = {"question": "Colour?", "header": "Colour", "options": [{"label": "Red", "description": "r"}, {"label": "Blue"}], "multiSelect": False}
SIZE = {"question": "Size?", "header": "Size", "options": [{"label": "S"}, {"label": "M"}, {"label": "L"}], "multiSelect": True}


class Questions(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.driver = FakeDriver()
        self.prompter = Prompter(self.driver)  # type: ignore[arg-type]

    def test_single_choice_answers_the_request(self) -> None:
        prompt = self.prompter.on_ask(ask("r1", [COLOUR]))
        self.assertIn("Colour?", prompt.text)
        self.assertEqual([c.label for row in prompt.rows for c in row][:2], ["1. Red", "2. Blue"])

        outcome = self.prompter.on_callback(prompt.rows[1][0].data)
        self.assertTrue(outcome.consumed)
        self.assertEqual(outcome.finished_request, "r1")
        self.assertIn("Blue", outcome.summary)
        request_id, result, _ = self.driver.responses[-1]
        self.assertEqual(request_id, "r1")
        assert result is not None
        self.assertEqual(result["behavior"], "allow")
        self.assertEqual(result["updatedInput"]["answers"], {"Colour?": "Blue"})
        self.assertEqual(self.prompter.open, [])

    def test_two_questions_are_asked_one_after_the_other(self) -> None:
        first = self.prompter.on_ask(ask("r2", [COLOUR, SIZE]))
        self.assertIn("1/2", first.text)
        outcome = self.prompter.on_callback(first.rows[0][0].data)
        self.assertIsNotNone(outcome.next_prompt)
        self.assertEqual(self.driver.responses, [])

        second = outcome.next_prompt
        assert second is not None
        self.assertTrue(second.multi)
        self.prompter.on_callback(second.rows[0][0].data)  # S
        toggled = self.prompter.on_callback(second.rows[2][0].data)  # L
        assert toggled.next_prompt is not None
        self.assertTrue(toggled.next_prompt.rows[0][0].label.startswith(prompter_module.DONE_MARK))
        done = self.prompter.on_callback([c for row in toggled.next_prompt.rows for c in row if c.data.endswith(":d")][0].data)
        self.assertEqual(done.finished_request, "r2")
        result = self.driver.responses[-1][1]
        assert result is not None
        self.assertEqual(result["updatedInput"]["answers"], {"Colour?": "Red", "Size?": "S, L"})

    def test_custom_answer_waits_for_the_next_line(self) -> None:
        prompt = self.prompter.on_ask(ask("r3", [COLOUR]))
        custom = [c for row in prompt.rows for c in row if c.data.endswith(":t")][0]
        outcome = self.prompter.on_callback(custom.data)
        self.assertTrue(outcome.consumed)
        self.assertTrue(self.prompter.awaiting_text)

        typed = self.prompter.on_text("teal, actually")
        self.assertEqual(typed.finished_request, "r3")
        result = self.driver.responses[-1][1]
        assert result is not None
        self.assertEqual(result["updatedInput"]["answers"], {"Colour?": "teal, actually"})

    def test_a_typed_digit_picks_a_button(self) -> None:
        self.prompter.on_ask(ask("r4", [COLOUR]))
        self.assertFalse(self.prompter.on_text("hello").consumed)
        outcome = self.prompter.on_text("2")
        self.assertEqual(outcome.finished_request, "r4")
        self.assertEqual(self.driver.responses[-1][1]["updatedInput"]["answers"]["Colour?"], "Blue")  # type: ignore[index]

    def test_stale_and_unknown_presses_are_harmless(self) -> None:
        prompt = self.prompter.on_ask(ask("r5", [COLOUR, SIZE]))
        self.prompter.on_callback(prompt.rows[0][0].data)
        stale = self.prompter.on_callback(prompt.rows[0][0].data)
        self.assertTrue(stale.consumed)
        self.assertEqual(stale.finished_request, "")
        self.assertFalse(self.prompter.on_callback("garbage").consumed)
        self.assertTrue(self.prompter.on_callback("a:99:0:0").consumed)

    def test_cancel_forgets_the_request(self) -> None:
        prompt = self.prompter.on_ask(ask("r6", [COLOUR]))
        self.assertEqual(self.prompter.on_cancel("r6"), prompt.key)
        self.assertEqual(self.prompter.open, [])
        self.assertEqual(self.prompter.on_cancel("r6"), "")


class Permissions(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.driver = FakeDriver()
        self.prompter = Prompter(self.driver)  # type: ignore[arg-type]

    def test_allow_deny_and_always(self) -> None:
        suggestion = [{"type": "addRules", "rules": [{"toolName": "Bash"}]}]
        prompt = self.prompter.on_ask(permission("p1", "Bash", {"command": "rm -rf build"}, suggestion))
        self.assertIn("rm -rf build", prompt.text)
        self.prompter.on_callback(prompt.rows[0][1].data)  # always
        result = self.driver.responses[-1][1]
        assert result is not None
        self.assertEqual(result["behavior"], "allow")
        self.assertEqual(result["updatedPermissions"], suggestion)

        prompt = self.prompter.on_ask(permission("p2", "Edit", {"file_path": "a.py", "new_string": "x = 1"}))
        self.assertIn("file_path: a.py", prompt.text)
        self.prompter.on_callback(prompt.rows[1][0].data)  # deny
        result = self.driver.responses[-1][1]
        assert result is not None
        self.assertEqual(result["behavior"], "deny")

        self.prompter.on_ask(permission("p3", "WebFetch", {"url": "https://x"}))
        self.assertTrue(self.prompter.on_text("y").consumed)
        self.assertEqual(self.driver.responses[-1][1]["behavior"], "allow")  # type: ignore[index]
        self.assertNotIn("updatedPermissions", self.driver.responses[-1][1])  # type: ignore[arg-type]

    def test_unanswered_prompts_are_declined_after_the_timeout(self) -> None:
        prompter = Prompter(self.driver, timeout_seconds=0.05)  # type: ignore[arg-type]
        prompt = prompter.on_ask(permission("p4", "Bash", {"command": "ls"}))
        self.assertEqual(prompter.tick(), [])
        time.sleep(0.1)
        outcomes = prompter.tick()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].close_prompt, prompt.key)
        self.assertEqual(self.driver.responses[-1][1]["behavior"], "deny")  # type: ignore[index]
        self.assertEqual(prompter.open, [])

    def test_describe_input_prefers_the_telling_field(self) -> None:
        describe = prompter_module.describe_input
        self.assertEqual(describe("Bash", {"command": "ls", "description": "list"}), "ls")
        self.assertTrue(describe("Write", {"file_path": "f", "content": "body"}).startswith("file_path: f\nbody"))
        self.assertEqual(describe("Odd", {"k": 1}), '{"k": 1}')


def plan(request_id: str, text: str = "Do the thing") -> Event:
    return Event(
        "ask",
        {"request_id": request_id, "tool_name": "ExitPlanMode", "input": {"plan": text}, "suggestions": []},
    )


class LeavingPlanMode(TempHome):
    """Approving the plan is also where the next mode is chosen."""

    def setUp(self) -> None:
        super().setUp()
        self.driver = FakeDriver()
        self.prompter = Prompter(self.driver)  # type: ignore[arg-type]

    def test_the_plan_is_shown_with_a_way_out(self) -> None:
        prompt = self.prompter.on_ask(plan("p1", "Rewrite the parser"))
        self.assertIn("Rewrite the parser", prompt.text)
        labels = [choice.label for row in prompt.rows for choice in row]
        self.assertEqual(len(labels), 3)

    def test_approving_switches_the_mode_in_the_same_answer(self) -> None:
        prompt = self.prompter.on_ask(plan("p2"))
        outcome = self.prompter.on_callback(prompt.rows[0][0].data)  # go ahead
        self.assertEqual(outcome.mode, "acceptEdits")
        request_id, result, _ = self.driver.responses[-1]
        self.assertEqual(request_id, "p2")
        assert result is not None
        self.assertEqual(result["behavior"], "allow")
        self.assertEqual(
            result["updatedPermissions"],
            [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}],
        )

        prompt = self.prompter.on_ask(plan("p3"))
        self.prompter.on_callback(prompt.rows[0][1].data)  # go, but ask
        self.assertEqual(self.driver.responses[-1][1]["updatedPermissions"][0]["mode"], "manual")  # type: ignore[index]

    def test_sending_it_back_is_a_denial_with_a_reason(self) -> None:
        prompt = self.prompter.on_ask(plan("p4"))
        outcome = self.prompter.on_callback(prompt.rows[1][0].data)
        self.assertEqual(outcome.mode, "")
        result = self.driver.responses[-1][1]
        assert result is not None
        self.assertEqual(result["behavior"], "deny")
        self.assertTrue(result["message"])

    def test_a_typed_digit_does_not_mean_allow_here(self) -> None:
        self.prompter.on_ask(plan("p5"))
        self.assertTrue(self.prompter.on_text("1").consumed)
        self.assertEqual(self.driver.responses[-1][1]["updatedPermissions"][0]["mode"], "acceptEdits")  # type: ignore[index]
