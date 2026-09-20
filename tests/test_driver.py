from __future__ import annotations

import queue

from app.transport.driver import Driver, DriverError, Event
from core.store import Config
from tests.base import TempHome

# A stand-in for `claude -p --input-format stream-json`: answers the control
# protocol the way 2.1.278 does, echoes prompts, and asks a question when told.
FAKE_CLAUDE = '''#!/usr/bin/env python3
import json, os, sys

def out(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

args = sys.argv[1:]
session = args[args.index("--session-id") + 1] if "--session-id" in args else "resumed"
out({"type": "system", "subtype": "init", "session_id": session, "model": "fake-1",
     "tools": ["AskUserQuestion", "Bash"], "cwd": os.getcwd()})
question = {"question": "Colour?", "header": "C", "multiSelect": False,
            "options": [{"label": "Red"}, {"label": "Blue"}]}

for line in sys.stdin:
    message = json.loads(line)
    kind = message["type"]
    if kind == "control_request":
        subtype = message["request"]["subtype"]
        payload = {"commands": [{"name": "compact"}]} if subtype == "initialize" else {}
        out({"type": "control_response", "response": {"subtype": "success",
             "request_id": message["request_id"], "response": payload}})
        if subtype == "interrupt":
            out({"type": "result", "subtype": "success", "result": "", "is_error": False})
        continue
    if kind == "control_response":
        response = message["response"]["response"]
        if response["behavior"] == "allow":
            answer = response["updatedInput"]["answers"]["Colour?"]
            content = "Your questions have been answered: " + answer
        else:
            content = "denied: " + response.get("message", "")
        out({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": content}]}})
        out({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}})
        out({"type": "result", "subtype": "success", "result": "ok", "is_error": False,
             "duration_ms": 10, "total_cost_usd": 0.01, "num_turns": 1})
        continue
    if kind == "user":
        text = message["message"]["content"]
        if text == "ask":
            out({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "AskUserQuestion",
                 "input": {"questions": [question]}}]}})
            out({"type": "control_request", "request_id": "req_q",
                 "request": {"subtype": "can_use_tool", "tool_name": "AskUserQuestion",
                             "input": {"questions": [question]}, "tool_use_id": "t1",
                             "requires_user_interaction": True}})
        elif text == "wall":
            out({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected",
                 "rateLimitType": "five_hour", "resetsAt": 1790000000}})
            out({"type": "result", "subtype": "success", "result": "limit", "is_error": True,
                 "api_error_status": 429, "terminal_reason": "rate_limit"})
        elif text == "quit":
            break
        else:
            out({"type": "assistant", "message": {"content": [{"type": "text", "text": "echo: " + text}]}})
            out({"type": "result", "subtype": "success", "result": "echo: " + text,
                 "is_error": False, "duration_ms": 5, "total_cost_usd": 0.0, "num_turns": 1})
sys.exit(3)
'''


class DriverProtocol(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.script = self.fake_claude(FAKE_CLAUDE)
        self.config = Config(real_claude_path=str(self.script), default_args=[])
        self.events: queue.Queue[Event] = queue.Queue()
        self.driver: Driver | None = None

    def tearDown(self) -> None:
        if self.driver is not None:
            self.driver.kill()
        super().tearDown()

    def _next(self, kind: str, timeout: float = 10.0) -> Event:
        while True:
            event = self.events.get(timeout=timeout)
            if event.kind == kind:
                return event

    def _start(self, **kwargs: object) -> Driver:
        self.driver = Driver(
            self.config, 1, cwd=self.home, args=[], listener=self.events.put, **kwargs  # type: ignore[arg-type]
        )
        self.driver.start()
        return self.driver

    def test_command_line_is_the_sdk_shape(self) -> None:
        driver = Driver(self.config, 1, cwd=self.home, args=["--model", "opus"], name="main", session_id="abc", listener=self.events.put)
        command = driver.command_line()
        self.assertEqual(command[0], str(self.script))
        for flag in ("-p", "--input-format", "--output-format", "--verbose", "--permission-prompt-tool", "--session-id", "--name"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--permission-prompt-tool") + 1], "stdio")
        self.assertEqual(command[command.index("--session-id") + 1], "abc")
        self.assertEqual(command[-2:], ["--model", "opus"])

        resumed = Driver(self.config, 1, cwd=self.home, args=[], name="main", session_id="abc", resume=True, listener=self.events.put)
        command = resumed.command_line()
        self.assertIn("--resume", command)
        self.assertNotIn("--session-id", command)
        self.assertNotIn("--name", command)

    def test_a_turn_round_trips(self) -> None:
        driver = self._start()
        init = self._next("init")
        self.assertEqual(init.data["session_id"], driver.session_id)
        self.assertEqual(driver.model, "fake-1")
        self.assertIn("AskUserQuestion", driver.tools)

        driver.send_user("hello")
        self.assertEqual(self._next("text").data["text"], "echo: hello")
        result = self._next("result")
        self.assertEqual(result.data["text"], "echo: hello")
        self.assertFalse(driver.turn_active)

        deadline_commands = [entry["name"] for entry in driver.commands]
        self.assertEqual(deadline_commands, ["compact"])

    def test_questions_arrive_as_ask_events_and_answers_go_back(self) -> None:
        driver = self._start()
        self._next("init")
        driver.send_user("ask")
        tool_use = self._next("tool_use")
        self.assertEqual(tool_use.data["name"], "AskUserQuestion")
        asked = self._next("ask")
        self.assertEqual(asked.data["request_id"], "req_q")
        self.assertTrue(asked.data["interactive"])

        driver.respond("req_q", result={"behavior": "allow", "updatedInput": {**asked.data["input"], "answers": {"Colour?": "Blue"}}})
        result_block = self._next("tool_result")
        self.assertIn("Blue", result_block.data["content"])
        self.assertEqual(self._next("result").data["cost"], 0.01)

    def test_rate_limit_is_reported_from_both_signals(self) -> None:
        driver = self._start()
        self._next("init")
        driver.send_user("wall")
        wall = self._next("rate_limit")
        self.assertEqual((wall.data["status"], wall.data["window"]), ("rejected", "five_hour"))
        result = self._next("result")
        self.assertEqual(result.data["api_error_status"], 429)

    def test_control_requests_and_exit(self) -> None:
        driver = self._start()
        self._next("init")
        driver.interrupt()  # answered by the fake, must not raise or hang
        self.assertEqual(driver.control({"subtype": "initialize", "hooks": None})["commands"][0]["name"], "compact")

        driver.send_user("quit")
        finished = self._next("exit")
        self.assertEqual(finished.data["code"], 3)
        self.assertFalse(driver.alive())
        with self.assertRaises(DriverError):
            driver.send_user("too late")

    def test_close_ends_a_quiet_session(self) -> None:
        driver = self._start()
        self._next("init")
        driver.close()
        self.assertFalse(driver.alive())
