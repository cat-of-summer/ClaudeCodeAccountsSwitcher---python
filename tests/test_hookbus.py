from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

from app import wrapper
from core import hookbus, sessions
from core.store import Config
from tests.base import TempHome


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _event(name: str, **fields: object) -> dict:
    return {"hook_event_name": name, "session_id": "abcdef12-0000", **fields}


class HookBusOverHttp(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.bus = hookbus.HookBus(journal=False)
        self.bus.start()

    def tearDown(self) -> None:
        self.bus.stop()
        super().tearDown()

    def test_events_reach_subscribers_and_answer_goes_back(self) -> None:
        seen: list[hookbus.HookEvent] = []

        def _watch(event: hookbus.HookEvent) -> dict | None:
            seen.append(event)
            if event.name == "PreToolUse":
                return {"hookSpecificOutput": {"permissionDecision": "deny"}}
            return None

        self.bus.subscribe(_watch)
        answer = _post(self.bus.url, _event("PreToolUse", tool_name="Bash"))
        quiet = _post(self.bus.url, _event("Stop"))

        self.assertEqual(answer["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(quiet, {})
        self.assertEqual([event.name for event in seen], ["PreToolUse", "Stop"])
        self.assertEqual(seen[0].tool_name, "Bash")
        self.assertEqual(self.bus.events_seen, 2)

    def test_first_answer_wins_and_a_broken_subscriber_is_skipped(self) -> None:
        def _broken(event: hookbus.HookEvent) -> dict | None:
            raise RuntimeError("boom")

        self.bus.subscribe(_broken)
        self.bus.subscribe(lambda event: {"first": True})
        self.bus.subscribe(lambda event: {"second": True})

        self.assertEqual(_post(self.bus.url, _event("Notification")), {"first": True})

    def test_garbage_is_answered_with_nothing(self) -> None:
        request = urllib.request.Request(
            self.bus.url, data=b"not json", method="POST"
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(json.loads(response.read()), {})
        self.assertEqual(_post(self.bus.url, {"no": "event name"}), {})


class ExternalHandlers(TempHome):
    def _script(self, name: str, body: str) -> str:
        path = self.home / name
        path.write_text(body, encoding="utf-8")
        return f'"{sys.executable}" "{path}"'

    def test_sync_handler_output_is_the_answer(self) -> None:
        command = self._script(
            "deny.py",
            "import json, sys\n"
            "payload = json.load(sys.stdin)\n"
            "print(json.dumps({'hookSpecificOutput': {'permissionDecision': 'deny',"
            " 'permissionDecisionReason': payload['tool_name']}}))\n",
        )
        bus = hookbus.HookBus(
            [hookbus.Handler("PreToolUse", command, matcher="Bash", sync=True)],
            journal=False,
        )
        answer = bus.dispatch(hookbus.HookEvent("PreToolUse", _event("PreToolUse", tool_name="Bash")))
        self.assertEqual(answer["hookSpecificOutput"]["permissionDecisionReason"], "Bash")

        other = bus.dispatch(hookbus.HookEvent("PreToolUse", _event("PreToolUse", tool_name="Edit")))
        self.assertEqual(other, {})

    def test_exit_two_blocks_the_way_claude_does(self) -> None:
        command = self._script(
            "block.py", "import sys\nsys.stderr.write('not today\\n')\nsys.exit(2)\n"
        )
        bus = hookbus.HookBus(
            [hookbus.Handler("PreToolUse", command, sync=True)], journal=False
        )
        answer = bus.dispatch(hookbus.HookEvent("PreToolUse", _event("PreToolUse", tool_name="Bash")))
        self.assertEqual(answer["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(answer["hookSpecificOutput"]["permissionDecisionReason"], "not today")

    def test_async_handler_runs_but_cannot_answer(self) -> None:
        marker = self.home / "seen.txt"
        command = self._script(
            "note.py",
            "import json, os, sys\n"
            "payload = json.load(sys.stdin)\n"
            f"open({str(marker)!r}, 'w').write(payload['hook_event_name'] + ' ' + os.environ['CCAS_SLOT'])\n"
            "print(json.dumps({'ignored': True}))\n",
        )
        bus = hookbus.HookBus(
            [hookbus.Handler("*", command)], slot=7, journal=False
        )
        answer = bus.dispatch(hookbus.HookEvent("Stop", _event("Stop")))
        self.assertEqual(answer, {})

        deadline = time.time() + 10
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(marker.read_text(), "Stop 7")

    def test_handlers_are_parsed_leniently(self) -> None:
        handlers = hookbus.parse_handlers(
            [
                {"event": "Stop", "command": "echo"},
                {"event": "", "command": "echo"},
                {"command": "echo"},
                "nonsense",
                {"event": "PreToolUse|PostToolUse", "command": "x", "sync": True, "timeout": 3},
            ]
        )
        self.assertEqual(len(handlers), 2)
        self.assertEqual(handlers[1].timeout, 3)
        self.assertTrue(handlers[1].wants(hookbus.HookEvent("PostToolUse", {})))
        self.assertFalse(handlers[1].wants(hookbus.HookEvent("Stop", {})))
        self.assertEqual(handlers[1].to_dict()["event"], "PreToolUse|PostToolUse")


class SettingsForClaude(TempHome):
    def test_every_event_points_at_the_bus(self) -> None:
        bus = hookbus.HookBus(journal=False)
        bus.start()
        try:
            document = bus.settings()
        finally:
            bus.stop()
        hooks = document["hooks"]
        self.assertEqual(set(hooks), set(hookbus.HOOK_EVENTS))
        entry = hooks["PreToolUse"][0]["hooks"][0]
        self.assertEqual(entry["type"], "http")
        self.assertTrue(entry["url"].startswith("http://127.0.0.1:"))
        self.assertEqual(entry["timeout"], hookbus.DECISION_TIMEOUT_SECONDS)
        self.assertEqual(hooks["Stop"][0]["hooks"][0]["timeout"], hookbus.QUICK_TIMEOUT_SECONDS)
        self.assertNotIn("timeout", hooks["SessionEnd"][0]["hooks"][0])

    def test_the_users_settings_are_merged_not_replaced(self) -> None:
        theirs = {
            "model": "opus",
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]},
        }
        merged = hookbus.merge_settings(
            {"hooks": {"Stop": [{"hooks": [{"type": "http", "url": "u"}]}]}}, theirs
        )
        self.assertEqual(merged["model"], "opus")
        self.assertEqual(len(merged["hooks"]["Stop"]), 2)
        self.assertEqual(merged["hooks"]["Stop"][0]["hooks"][0]["type"], "http")

    def test_settings_flag_is_lifted_from_the_arguments(self) -> None:
        self.assertEqual(
            hookbus.split_settings_flag(["--model", "x", "--settings", "a.json", "hi"]),
            (["--model", "x", "hi"], "a.json"),
        )
        self.assertEqual(
            hookbus.split_settings_flag(["--settings={\"a\":1}"]), ([], '{"a":1}')
        )
        self.assertEqual(hookbus.split_settings_flag(["--resume"]), (["--resume"], None))

    def test_settings_argument_may_be_json_or_a_file(self) -> None:
        path = self.home / "mine.json"
        path.write_text('{"env": {"A": "1"}}', encoding="utf-8")
        self.assertEqual(hookbus.load_settings_argument(str(path)), {"env": {"A": "1"}})
        self.assertEqual(hookbus.load_settings_argument('{"b": 2}'), {"b": 2})
        self.assertIsNone(hookbus.load_settings_argument("/nowhere.json"))
        self.assertIsNone(hookbus.load_settings_argument("{broken"))

    def test_prepare_args_writes_the_file_and_keeps_the_rest(self) -> None:
        bus = hookbus.HookBus(journal=False)
        bus.start()
        target = self.home / "s" / "1.settings.json"
        try:
            args = hookbus.prepare_args(bus, ["--settings", '{"model":"m"}', "-c"], target)
        finally:
            bus.stop()
        self.assertEqual(args, ["--settings", str(target), "-c"])
        written = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(written["model"], "m")
        self.assertIn("PreToolUse", written["hooks"])


class WrapperIntegration(TempHome):
    def test_bus_is_skipped_where_hooks_cannot_fire(self) -> None:
        config = Config()
        self.assertTrue(wrapper.wants_hook_bus(config, ["--resume"]))
        self.assertFalse(wrapper.wants_hook_bus(config, ["mcp", "list"]))
        self.assertFalse(wrapper.wants_hook_bus(config, ["--bare", "-p", "hi"]))
        config.hooks_bus = False
        self.assertFalse(wrapper.wants_hook_bus(config, []))

    def test_launch_hands_claude_the_settings_and_cleans_up(self) -> None:
        recorder = self.home / "args.json"
        fake = self.home / "fake_claude.py"
        fake.write_text(
            "import json, sys\n"
            f"open({str(recorder)!r}, 'w').write(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        # The interpreter is "claude" and the script rides in as a default
        # argument, the way the supervise tests do it.
        config = Config(real_claude_path=sys.executable, default_args=[str(fake)])
        sessions.register_session(1)
        try:
            code = wrapper.launch(config, 1, ["--verbose"])
        finally:
            sessions.unregister_session()

        self.assertEqual(code, 0)
        argv = json.loads(recorder.read_text())
        self.assertEqual(argv[0], "--settings")
        self.assertEqual(argv[2:], ["--verbose"])
        # The settings file lived only as long as the session did.
        self.assertFalse(Path(argv[1]).exists())
        self.assertFalse(wrapper.settings_file().exists())


def _tool(name: str, tool_input: dict) -> hookbus.HookEvent:
    return hookbus.HookEvent("PreToolUse", _event("PreToolUse", tool_name=name, tool_input=tool_input))


class ArtifactsStayLocal(TempHome):
    def test_publishing_is_refused_with_a_reason_for_the_model(self) -> None:
        for tool_input in ({"file_path": "a.html"}, {"action": "publish", "file_path": "a.html", "url": "u"}):
            answer = hookbus.block_artifact_publish(_tool("Artifact", tool_input))
            assert answer is not None
            output = answer["hookSpecificOutput"]
            self.assertEqual(output["permissionDecision"], "deny")
            self.assertIn("local", output["permissionDecisionReason"])

    def test_everything_else_is_left_alone(self) -> None:
        for event in (
            _tool("Artifact", {"action": "read", "url": "u"}),
            _tool("Artifact", {"action": "list"}),
            _tool("Write", {"file_path": "a.html"}),
            hookbus.HookEvent("PostToolUse", _event("PostToolUse", tool_name="Artifact", tool_input={})),
        ):
            self.assertIsNone(hookbus.block_artifact_publish(event))

    def test_the_wrappers_bus_refuses_unless_publishing_is_allowed(self) -> None:
        payload = _event("PreToolUse", tool_name="Artifact", tool_input={"file_path": "a.html"})
        bus = wrapper.open_hook_bus(Config(), 1)
        try:
            answer = _post(bus.url, payload)
        finally:
            wrapper.close_hook_bus(bus)
        self.assertEqual(answer["hookSpecificOutput"]["permissionDecision"], "deny")

        bus = wrapper.open_hook_bus(Config(artifact_publish=True), 1)
        try:
            self.assertEqual(_post(bus.url, payload), {})
        finally:
            wrapper.close_hook_bus(bus)

    def test_the_switch_is_kept_in_the_config(self) -> None:
        self.assertFalse(Config().artifact_publish)
        Config(artifact_publish=True).save()
        self.assertTrue(Config.load().artifact_publish)
