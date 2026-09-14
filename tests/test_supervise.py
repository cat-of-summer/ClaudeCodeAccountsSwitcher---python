from __future__ import annotations

import json
import sys
import time
from unittest import mock

from app import autoswitch, wrapper
from core import store
from core.store import Accounts, Config, Slot
from tests.base import TempHome
from ui import usage

# A stand-in for claude: records how it was invoked, optionally writes the
# rate-limit line into a transcript, then waits to be killed.
FAKE_CLAUDE = '''
import json, os, pathlib, sys, time
from datetime import datetime, timezone

args = sys.argv[1:]
with open(os.environ["FAKE_PIDS"], "a", encoding="utf-8") as handle:
    handle.write(str(os.getpid()) + "\\n")

log = pathlib.Path(os.environ["FAKE_LOG"])
previous = log.read_text().splitlines() if log.exists() else []
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args, ensure_ascii=False) + "\\n")

session = ""
for flag in ("--session-id", "--resume"):
    if flag in args:
        session = args[args.index(flag) + 1]
        break

if previous:
    # Second launch: the switch worked, nothing left to prove.
    sys.exit(0)

directory = pathlib.Path(os.environ["FAKE_PROJECTS"]) / "project"
directory.mkdir(parents=True, exist_ok=True)
transcript = directory / (session + ".jsonl")

with transcript.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"type": "permission-mode", "permissionMode": "plan"}) + "\\n")
    handle.write(json.dumps({
        "type": "assistant",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": {"model": "<synthetic>",
                    "content": [{"type": "text", "text": "You've hit your session limit"}]},
        "error": "rate_limit",
        "isApiErrorMessage": True,
        "apiErrorStatus": 429,
        "sessionId": session,
    }) + "\\n")

time.sleep(float(os.environ.get("FAKE_LINGER", "30")))
'''


class TestSupervisedRun(TempHome):
    def setUp(self) -> None:
        super().setUp()

        self.script = self.home / "fake_claude.py"
        self.script.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.log = self.home / "invocations.log"
        self.pids = self.home / "children.pids"
        self.addCleanup(self._kill_children)

        # real_claude_path is the interpreter; the script rides in as a default
        # argument, which merge_default_args always keeps.
        config = Config(
            real_claude_path=sys.executable,
            cred_mode="env",
            default_args=[str(self.script)],
        )
        config.auto_switch = {
            **config.auto_switch,
            "enabled": True,
            "confirmWithApi": False,
            "minIntervalSeconds": 0,
        }
        config.save()
        self.config = Config.load()

        accounts = Accounts()
        for number, five in ((1, 100.0), (2, 5.0)):
            self.write_credentials(store.creds_file(number))
            accounts.slots[number] = Slot(
                number=number,
                email=f"slot{number}@example.com",
                usage={
                    "fetchedAtMs": time.time() * 1000,
                    "five_hour": {"utilization": five},
                    "seven_day": {"utilization": five / 2},
                },
            )
        accounts.active = 1
        accounts.save()

        self._env = mock.patch.dict(
            "os.environ",
            {
                "FAKE_LOG": str(self.log),
                "FAKE_PIDS": str(self.pids),
                "FAKE_PROJECTS": str(self.home / ".claude" / "projects"),
                "FAKE_LINGER": "20",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

        self._tty = mock.patch.object(wrapper, "_interactive", return_value=True)
        self._tty.start()
        self.addCleanup(self._tty.stop)

        # Keep the watcher responsive and the confirmation instant.
        self._poll = mock.patch.object(autoswitch, "WATCH_POLL_SECONDS", 0.05)
        self._poll.start()
        self.addCleanup(self._poll.stop)

        # Every candidate is now re-fetched before the ranking; the store is
        # the only source of truth these tests want.
        self._network = mock.patch.object(usage, "refresh_slots", return_value={})
        self._network.start()
        self.addCleanup(self._network.stop)

    def _invocations(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def _kill_children(self) -> None:
        """Stop any fake claude the supervisor deliberately left running."""
        import os
        import signal
        import subprocess

        if not self.pids.exists():
            return
        for line in self.pids.read_text(encoding="utf-8").split():
            try:
                pid = int(line)
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/F", "/T"],
                        capture_output=True,
                        check=False,
                    )
                else:
                    os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError):
                continue
        self.pids.unlink()

    def test_a_spent_session_moves_to_another_account_and_resumes(self) -> None:
        code = wrapper.run_slot(self.config, 1, [])

        calls = self._invocations()
        self.assertEqual(len(calls), 2, calls)

        first, second = calls
        self.assertIn("--session-id", first)
        session_id = first[first.index("--session-id") + 1]

        # Same conversation, on the other account, in the mode it was left in.
        self.assertIn("--resume", second)
        self.assertEqual(second[second.index("--resume") + 1], session_id)
        self.assertEqual(second[second.index("--permission-mode") + 1], "plan")
        self.assertEqual(code, 0)

        self.assertEqual(Accounts.load().active, 2)

    def test_the_switch_plan_records_the_slot_that_was_left(self) -> None:
        wrapper.run_slot(self.config, 1, [])

        plan = store.read_json(autoswitch.switch_plan_path())
        self.assertEqual(plan["from"], 1)
        self.assertEqual(plan["to"], 2)

    def test_a_second_terminal_follows_the_same_plan(self) -> None:
        """Two windows on one account must not land on two different accounts."""
        wrapper.run_slot(self.config, 1, [])
        first_target = store.read_json(autoswitch.switch_plan_path())["to"]

        election = autoswitch.elect_target(
            Accounts.load(), current=1, tried={1}, threshold=95
        )
        self.assertTrue(election.followed)
        self.assertEqual(election.target, first_target)

    def _run_until_limit_is_seen(self, config: Config) -> None:
        """Drive a run that must NOT switch, then stop the surviving child.

        When ccas decides to stay put, claude keeps running -- which is the
        whole point -- so the test has to end the session itself.
        """
        thread = _run_in_background(config, 1)
        _wait_for(lambda: self.log.exists() and self._invocations(), timeout=20)
        time.sleep(2)  # long enough for a switch to have happened if it were going to
        self._kill_children()
        thread.join(timeout=30)

    def test_no_free_account_means_no_switch(self) -> None:
        def _spend(accounts: Accounts) -> None:
            accounts.ensure(2).usage = {
                "fetchedAtMs": time.time() * 1000,
                "five_hour": {"utilization": 100.0},
                "seven_day": {"utilization": 100.0},
            }

        store.update_accounts(_spend)
        self._run_until_limit_is_seen(self.config)

        self.assertEqual(len(self._invocations()), 1)
        self.assertFalse(autoswitch.switch_plan_path().exists())

    def test_the_switch_limit_is_respected(self) -> None:
        """maxSwitches 0 means never move -- and 0 must not read as "unset"."""
        config = Config.load()
        config.auto_switch = {**config.auto_switch, "maxSwitches": 0}
        config.save()

        self._run_until_limit_is_seen(Config.load())
        self.assertEqual(len(self._invocations()), 1)

    def test_the_cooldown_is_measured_from_the_previous_switch(self) -> None:
        """A long session must still be allowed to move when its turn comes.

        The cooldown used to be evaluated at launch, moments after the previous
        switch, so any non-zero interval turned maxSwitches into 1 no matter
        how many hours the session then ran.
        """
        config = Config.load()
        config.auto_switch = {**config.auto_switch, "minIntervalSeconds": 3600}
        config.save()

        wrapper.run_slot(Config.load(), 1, [])

        calls = self._invocations()
        self.assertEqual(len(calls), 2, calls)
        self.assertIn("--resume", calls[1])

    def test_a_non_interactive_run_is_never_supervised(self) -> None:
        with mock.patch.object(wrapper, "_interactive", return_value=False):
            wrapper.run_slot(self.config, 2, ["--version"])

        calls = self._invocations()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--session-id", calls[0])


def _run_in_background(config: Config, slot: int):
    import threading

    thread = threading.Thread(target=wrapper.run_slot, args=(config, slot, []), daemon=True)
    thread.start()
    return thread


def _wait_for(predicate, *, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
