from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from unittest import mock

from app import autoswitch
from core import store
from core.store import Accounts, Config, Slot
from tests.base import TempHome
from ui import usage

SESSION = "11111111-2222-4333-8444-555555555555"


def iso(offset: float = 0.0) -> str:
    return datetime.fromtimestamp(time.time() + offset, timezone.utc).isoformat()


def _at(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).isoformat()


def limit_record(*, at: str, text: str = "You've hit your session limit") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": at,
            "message": {"model": "<synthetic>", "content": [{"type": "text", "text": text}]},
            "error": "rate_limit",
            "isApiErrorMessage": True,
            "apiErrorStatus": 429,
            "sessionId": SESSION,
        }
    )


def enabled_config(**auto: object) -> Config:
    config = Config()
    config.auto_switch = {**config.auto_switch, "enabled": True, **auto}
    return config


class TestPlanSession(TempHome):
    def test_a_bare_launch_gets_a_session_id(self) -> None:
        plan = autoswitch.plan_session(enabled_config(), [], interactive=True)
        self.assertTrue(plan.supervise)
        self.assertIsNotNone(plan.session_id)
        self.assertEqual(plan.args[0], "--session-id")
        self.assertEqual(plan.args[1], plan.session_id)

    def test_disabled_by_default(self) -> None:
        plan = autoswitch.plan_session(Config(), [], interactive=True)
        self.assertFalse(plan.supervise)
        self.assertEqual(plan.args, [])

    def test_non_interactive_is_left_alone(self) -> None:
        plan = autoswitch.plan_session(enabled_config(), [], interactive=False)
        self.assertFalse(plan.supervise)

    def test_print_mode_opts_out(self) -> None:
        for args in (["-p", "hi"], ["--print"], ["--output-format", "json"]):
            plan = autoswitch.plan_session(enabled_config(), args, interactive=True)
            self.assertFalse(plan.supervise, args)
            self.assertEqual(plan.args, args)

    def test_background_and_cloud_opt_out(self) -> None:
        for args in (["--bg"], ["--cloud"], ["--no-session-persistence"], ["--fork-session"]):
            self.assertFalse(
                autoswitch.plan_session(enabled_config(), args, interactive=True).supervise,
                args,
            )

    def test_subcommands_are_left_alone(self) -> None:
        plan = autoswitch.plan_session(enabled_config(), ["mcp", "list"], interactive=True)
        self.assertFalse(plan.supervise)

    def test_explicit_session_id_is_adopted(self) -> None:
        plan = autoswitch.plan_session(
            enabled_config(), ["--session-id", SESSION], interactive=True
        )
        self.assertTrue(plan.supervise)
        self.assertEqual(plan.session_id, SESSION)
        self.assertEqual(plan.args, ["--session-id", SESSION])

    def test_resume_with_an_id_is_adopted(self) -> None:
        plan = autoswitch.plan_session(enabled_config(), ["-r", SESSION], interactive=True)
        self.assertEqual(plan.session_id, SESSION)
        self.assertTrue(plan.supervise)

    def test_continue_is_supervised_without_an_id(self) -> None:
        plan = autoswitch.plan_session(enabled_config(), ["-c"], interactive=True)
        self.assertTrue(plan.supervise)
        self.assertIsNone(plan.session_id)
        self.assertEqual(plan.args, ["-c"])

    def test_bare_resume_opens_a_picker_so_no_id_is_invented(self) -> None:
        plan = autoswitch.plan_session(enabled_config(), ["-r"], interactive=True)
        self.assertTrue(plan.supervise)
        self.assertIsNone(plan.session_id)

    def test_a_prompt_is_still_supervised(self) -> None:
        plan = autoswitch.plan_session(enabled_config(), ["do the thing"], interactive=True)
        self.assertTrue(plan.supervise)
        self.assertEqual(plan.args[2], "do the thing")


class TestRelaunchArgs(TempHome):
    def test_session_id_becomes_resume(self) -> None:
        args = autoswitch.relaunch_args(
            ["--session-id", SESSION, "--model", "opus"],
            session_id=SESSION,
            config=Config(),
        )
        self.assertEqual(args[:2], ["--resume", SESSION])
        self.assertIn("--model", args)
        self.assertIn("opus", args)
        self.assertNotIn("--session-id", args)

    def test_without_an_id_it_continues(self) -> None:
        args = autoswitch.relaunch_args(["-c"], session_id=None, config=Config())
        self.assertEqual(args, ["--continue"])

    def test_the_leading_prompt_is_dropped(self) -> None:
        """It was already delivered; resending it restarts the task."""
        args = autoswitch.relaunch_args(
            ["--session-id", SESSION, "do the thing"],
            session_id=SESSION,
            config=Config(),
        )
        self.assertNotIn("do the thing", args)

    def test_permission_mode_is_restored(self) -> None:
        args = autoswitch.relaunch_args(
            ["--session-id", SESSION], session_id=SESSION, mode="plan", config=Config()
        )
        self.assertIn("--permission-mode", args)
        self.assertIn("plan", args)

    def test_bypass_default_is_downgraded_so_the_mode_survives(self) -> None:
        config = Config()
        config.default_args = ["--dangerously-skip-permissions"]
        args = autoswitch.relaunch_args([], session_id=SESSION, mode="plan", config=config)
        self.assertIn(autoswitch.ALLOW_BYPASS_FLAG, args)

        # merge_default_args must then suppress the bypass flag entirely.
        from app.wrapper import merge_default_args

        merged = merge_default_args(config.default_args, args)
        self.assertNotIn(autoswitch.BYPASS_FLAG, merged)

    def test_bypass_default_is_kept_when_that_was_the_mode(self) -> None:
        config = Config()
        config.default_args = ["--dangerously-skip-permissions"]
        args = autoswitch.relaunch_args(
            [], session_id=SESSION, mode="bypassPermissions", config=config
        )
        self.assertNotIn(autoswitch.ALLOW_BYPASS_FLAG, args)

    def test_default_mode_is_not_passed_through(self) -> None:
        """`default` shows up in transcripts but the CLI rejects it."""
        args = autoswitch.relaunch_args(
            [], session_id=SESSION, mode="default", config=Config()
        )
        self.assertNotIn("--permission-mode", args)

    def test_restore_mode_can_be_turned_off(self) -> None:
        args = autoswitch.relaunch_args(
            [], session_id=SESSION, mode="plan", config=enabled_config(restoreMode=False)
        )
        self.assertNotIn("--permission-mode", args)

    def test_resume_prompt_is_appended_last(self) -> None:
        args = autoswitch.relaunch_args(
            ["--model", "opus"],
            session_id=SESSION,
            config=enabled_config(resumePrompt="Продолжай"),
        )
        self.assertEqual(args[-1], "Продолжай")

    def test_no_prompt_is_sent_by_default(self) -> None:
        args = autoswitch.relaunch_args([], session_id=SESSION, config=Config())
        self.assertEqual(args, ["--resume", SESSION])


class TestTranscriptWatcher(TempHome):
    def _transcript(self, name: str = SESSION):
        directory = self.home / ".claude" / "projects" / "some-project"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{name}.jsonl"

    def _watcher(self, signal, *, since: float | None = None):
        return autoswitch.TranscriptWatcher(
            SESSION, since=time.time() - 5 if since is None else since, signal=signal
        )

    def test_a_fresh_limit_fires(self) -> None:
        path = self._transcript()
        path.write_text(limit_record(at=iso()) + "\n", encoding="utf-8")

        signal = autoswitch.LimitSignal()
        self._watcher(signal).tick()
        self.assertTrue(signal.fired)
        self.assertEqual(signal.window, autoswitch.FIVE_HOUR)

    def test_the_weekly_wording_is_recognised(self) -> None:
        path = self._transcript()
        path.write_text(
            limit_record(at=iso(), text="You've hit your weekly limit") + "\n",
            encoding="utf-8",
        )
        signal = autoswitch.LimitSignal()
        self._watcher(signal).tick()
        self.assertEqual(signal.window, autoswitch.SEVEN_DAY)

    def test_an_older_marker_is_ignored(self) -> None:
        """A resumed transcript replays history; only this run counts."""
        path = self._transcript()
        path.write_text(limit_record(at=iso(-3600)) + "\n", encoding="utf-8")

        signal = autoswitch.LimitSignal()
        self._watcher(signal).tick()
        self.assertFalse(signal.fired)

    def test_ordinary_records_do_not_fire(self) -> None:
        path = self._transcript()
        path.write_text(
            json.dumps({"type": "assistant", "timestamp": iso()}) + "\n", encoding="utf-8"
        )
        signal = autoswitch.LimitSignal()
        self._watcher(signal).tick()
        self.assertFalse(signal.fired)

    def test_a_half_written_line_is_held_back(self) -> None:
        path = self._transcript()
        record = limit_record(at=iso())
        path.write_text(record[:40], encoding="utf-8")

        signal = autoswitch.LimitSignal()
        watcher = self._watcher(signal)
        watcher.tick()
        self.assertFalse(signal.fired)

        with path.open("a", encoding="utf-8") as handle:
            handle.write(record[40:] + "\n")
        watcher.tick()
        self.assertTrue(signal.fired)

    def test_truncation_is_survived(self) -> None:
        path = self._transcript()
        path.write_text(
            (json.dumps({"type": "x", "pad": "y" * 200}) + "\n") * 20, encoding="utf-8"
        )

        signal = autoswitch.LimitSignal()
        watcher = self._watcher(signal)
        watcher.tick()

        # Shorter than what we had already consumed: reading from the old
        # offset would land past the end and see nothing ever again.
        shorter = limit_record(at=iso()) + "\n"
        self.assertLess(len(shorter), path.stat().st_size)
        path.write_text(shorter, encoding="utf-8")

        watcher.tick()
        self.assertTrue(signal.fired)

    def test_a_transcript_created_later_is_picked_up(self) -> None:
        signal = autoswitch.LimitSignal()
        watcher = self._watcher(signal)
        watcher.tick()  # nothing exists yet

        self._transcript().write_text(limit_record(at=iso()) + "\n", encoding="utf-8")
        watcher.tick()
        self.assertTrue(signal.fired)

    def test_permission_mode_is_remembered(self) -> None:
        path = self._transcript()
        path.write_text(
            json.dumps({"type": "permission-mode", "permissionMode": "plan"}) + "\n"
            + json.dumps(
                {"type": "user", "timestamp": iso(), "permissionMode": "bypassPermissions"}
            )
            + "\n",
            encoding="utf-8",
        )

        signal = autoswitch.LimitSignal()
        self._watcher(signal).tick()
        self.assertEqual(signal.permission_mode, "bypassPermissions")
        self.assertFalse(signal.fired)

    def test_only_the_first_limit_fires(self) -> None:
        """claude retries a 429 many times; one wall is one switch."""
        path = self._transcript()
        path.write_text((limit_record(at=iso()) + "\n") * 5, encoding="utf-8")

        signal = autoswitch.LimitSignal()
        with mock.patch.object(autoswitch.LimitSignal, "fire", autospec=True) as fire:
            fire.side_effect = lambda self, window, **_kw: self._event.set()
            self._watcher(signal).tick()
        self.assertEqual(fire.call_count, 1)

    def test_slug_matches_what_claude_writes(self) -> None:
        from pathlib import PureWindowsPath

        self.assertEqual(
            autoswitch.project_slug(PureWindowsPath(r"O:\Projects\Some---thing")),
            "O--Projects-Some---thing",
        )


class TestPickTarget(TempHome):
    def _slot(self, number: int, five: float, seven: float, *, fresh: bool = True) -> Slot:
        self.write_credentials(store.creds_file(number))
        age = 0 if fresh else (usage.USAGE_TTL_RANKING_SECONDS + 600) * 1000
        return Slot(
            number=number,
            usage={
                "fetchedAtMs": time.time() * 1000 - age,
                "five_hour": {"utilization": five},
                "seven_day": {"utilization": seven},
            },
        )

    def _accounts(self, *slots: Slot) -> Accounts:
        accounts = Accounts()
        for slot in slots:
            accounts.slots[slot.number] = slot
        return accounts

    def test_the_least_loaded_wins(self) -> None:
        accounts = self._accounts(
            self._slot(1, 100, 90), self._slot(2, 10, 40), self._slot(3, 5, 60)
        )
        target, reason, _ = autoswitch.pick_target(
            accounts, current=1, tried={1}, threshold=95
        )
        self.assertEqual(target, 2)  # weekly is the first key, not five_hour
        self.assertEqual(reason, "limits")

    def test_an_exhausted_weekly_window_is_excluded(self) -> None:
        accounts = self._accounts(self._slot(1, 100, 100), self._slot(2, 0, 100))
        target, _, _ = autoswitch.pick_target(accounts, current=1, tried={1}, threshold=95)
        self.assertIsNone(target)

    def test_the_threshold_excludes_a_nearly_spent_slot(self) -> None:
        accounts = self._accounts(self._slot(1, 100, 10), self._slot(2, 96, 10))
        target, reason, _ = autoswitch.pick_target(
            accounts, current=1, tried={1}, threshold=95
        )
        self.assertIsNone(target)
        self.assertEqual(reason, "all_exhausted")

    def test_a_tie_goes_to_the_lower_slot(self) -> None:
        accounts = self._accounts(
            self._slot(1, 100, 90), self._slot(3, 10, 10), self._slot(2, 10, 10)
        )
        target, _, _ = autoswitch.pick_target(accounts, current=1, tried={1}, threshold=95)
        self.assertEqual(target, 2)

    def test_stale_data_falls_back_to_slot_order(self) -> None:
        accounts = self._accounts(
            self._slot(1, 100, 90),
            self._slot(2, 0, 0, fresh=False),
            self._slot(4, 0, 0, fresh=False),
        )
        target, reason, _ = autoswitch.pick_target(
            accounts, current=2, tried={2}, threshold=95
        )
        self.assertEqual(target, 4)
        self.assertEqual(reason, "order")

    def test_round_robin_wraps(self) -> None:
        accounts = self._accounts(
            self._slot(1, 0, 0, fresh=False), self._slot(5, 0, 0, fresh=False)
        )
        target, _, _ = autoswitch.pick_target(accounts, current=5, tried={5}, threshold=95)
        self.assertEqual(target, 1)

    def test_already_tried_slots_are_skipped(self) -> None:
        accounts = self._accounts(
            self._slot(1, 100, 90), self._slot(2, 1, 1), self._slot(3, 2, 2)
        )
        target, _, _ = autoswitch.pick_target(
            accounts, current=1, tried={1, 2}, threshold=95
        )
        self.assertEqual(target, 3)

    def test_a_slot_without_credentials_is_not_a_candidate(self) -> None:
        accounts = self._accounts(self._slot(1, 100, 90))
        accounts.slots[2] = Slot(number=2)  # never signed in
        target, reason, _ = autoswitch.pick_target(
            accounts, current=1, tried={1}, threshold=95
        )
        self.assertIsNone(target)
        self.assertEqual(reason, "no_candidates")

    def test_order_strategy_ignores_limits(self) -> None:
        accounts = self._accounts(self._slot(1, 100, 90), self._slot(2, 99, 99))
        target, reason, _ = autoswitch.pick_target(
            accounts, current=1, tried={1}, threshold=95, strategy="order"
        )
        self.assertEqual(target, 2)
        self.assertEqual(reason, "order")


class TestElection(TempHome):
    def _accounts(self) -> Accounts:
        accounts = Accounts()
        for number, five in ((1, 100.0), (2, 10.0), (3, 20.0)):
            self.write_credentials(store.creds_file(number))
            accounts.slots[number] = Slot(
                number=number,
                usage={
                    "fetchedAtMs": time.time() * 1000,
                    "five_hour": {"utilization": five},
                    "seven_day": {"utilization": five / 2},
                },
            )
        return accounts

    def test_the_second_session_follows_the_first(self) -> None:
        """Two windows on one account must not scatter to two accounts."""
        accounts = self._accounts()
        first = autoswitch.elect_target(accounts, current=1, tried={1}, threshold=95)
        second = autoswitch.elect_target(accounts, current=1, tried={1}, threshold=95)

        self.assertEqual(first.target, second.target)
        self.assertFalse(first.followed)
        self.assertTrue(second.followed)

    def test_a_session_on_another_slot_decides_for_itself(self) -> None:
        accounts = self._accounts()
        autoswitch.elect_target(accounts, current=1, tried={1}, threshold=95)
        other = autoswitch.elect_target(accounts, current=3, tried={3}, threshold=95)

        self.assertEqual(other.target, 2)
        self.assertFalse(other.followed)

    def test_an_expired_plan_is_ignored(self) -> None:
        accounts = self._accounts()
        autoswitch.elect_target(accounts, current=1, tried={1}, threshold=95)

        later = autoswitch.elect_target(
            accounts,
            current=1,
            tried={1, 2},
            threshold=95,
            now=time.time() + autoswitch.PLAN_TTL_SECONDS + 1,
        )
        self.assertEqual(later.target, 3)
        self.assertFalse(later.followed)

    def test_no_target_writes_no_plan(self) -> None:
        accounts = Accounts()
        accounts.slots[1] = Slot(number=1)
        election = autoswitch.elect_target(accounts, current=1, tried={1}, threshold=95)
        self.assertIsNone(election.target)
        self.assertFalse(autoswitch.switch_plan_path().exists())

    def test_a_plan_whose_target_ran_dry_is_not_followed(self) -> None:
        """The plan outlives its wait; the target may not have."""
        accounts = self._accounts()
        first = autoswitch.elect_target(accounts, current=1, tried={1}, threshold=95)
        self.assertEqual(first.target, 2)

        # Fresh numbers arrive: slot 2 is now spent with no reset in sight.
        accounts.slots[2].usage = {
            "fetchedAtMs": time.time() * 1000,
            "five_hour": {"utilization": 100.0},
            "seven_day": {"utilization": 5.0},
        }
        second = autoswitch.elect_target(accounts, current=1, tried={1}, threshold=95)

        self.assertFalse(second.followed)
        self.assertEqual(second.target, 3)

    def test_a_plan_whose_target_was_already_tried_is_not_followed(self) -> None:
        accounts = self._accounts()
        autoswitch.elect_target(accounts, current=1, tried={1}, threshold=95)

        again = autoswitch.elect_target(accounts, current=1, tried={1, 2}, threshold=95)
        self.assertFalse(again.followed)
        self.assertEqual(again.target, 3)

    def test_a_waiting_plan_takes_the_fresh_reset_moment(self) -> None:
        """A plan that waits names a spent slot on purpose; only the moment moves."""
        accounts = self._accounts()
        soon = time.time() + 600
        later = time.time() + 1200
        for number in (2, 3):
            accounts.slots[number].usage = {
                "fetchedAtMs": time.time() * 1000,
                "five_hour": {
                    "utilization": 100.0,
                    "resets_at": _at(soon if number == 2 else later),
                },
                "seven_day": {"utilization": 5.0},
            }
        first = autoswitch.elect_target(accounts, current=1, tried={1}, threshold=95)
        self.assertEqual(first.target, 2)
        self.assertAlmostEqual(first.available_at, soon, delta=1.0)

        # Slot 2 pushed its reset out by ten minutes since the plan was written.
        moved = soon + 600
        accounts.slots[2].usage["five_hour"]["resets_at"] = _at(moved)
        second = autoswitch.elect_target(accounts, current=1, tried={1}, threshold=95)

        self.assertTrue(second.followed)
        self.assertEqual(second.target, 2)
        self.assertAlmostEqual(second.available_at, moved, delta=1.0)

    def test_the_verdict_names_every_slot(self) -> None:
        accounts = self._accounts()
        accounts.slots[4] = Slot(number=4)  # never signed in
        accounts.slots[5] = Slot(number=5, usage={"fetchedAtMs": 1})  # ancient data
        self.write_credentials(store.creds_file(5))

        verdicts = autoswitch.explain(accounts, current=1, tried={1, 3}, threshold=95)
        self.assertEqual(
            verdicts,
            # Slot 3 was used earlier and has quota left: "tried-free" says it
            # is out of the ranking but still somewhere to go back to.
            {1: "current", 2: "free", 3: "tried-free", 4: "no-credentials", 5: "no-data"},
        )

        election = autoswitch.elect_target(accounts, current=1, tried={1, 3}, threshold=95)
        self.assertEqual(election.detail, verdicts)


class TestExhaustion(TempHome):
    def test_five_hour_over_the_threshold_counts(self) -> None:
        self.assertTrue(
            autoswitch.is_exhausted({"five_hour": {"utilization": 97}}, threshold=95)
        )

    def test_weekly_only_counts_at_a_hundred(self) -> None:
        self.assertFalse(
            autoswitch.is_exhausted({"seven_day": {"utilization": 97}}, threshold=95)
        )
        self.assertTrue(
            autoswitch.is_exhausted({"seven_day": {"utilization": 100}}, threshold=95)
        )

    def test_unreachable_api_reports_unknown_not_false(self) -> None:
        """Refusing to switch and claiming "quota is fine" are different answers."""
        with mock.patch.object(usage, "refresh_slots", return_value={}):
            verdict = autoswitch.confirm_exhausted(Slot(number=1), threshold=95, delay=0)
        self.assertIsNone(verdict)

    def test_confirmation_stores_what_it_learned(self) -> None:
        payload = {"fetchedAtMs": time.time() * 1000, "five_hour": {"utilization": 99}}
        slot = Slot(number=1)
        with mock.patch.object(usage, "refresh_slots", return_value={1: payload}):
            verdict = autoswitch.confirm_exhausted(slot, threshold=95, delay=0)

        self.assertTrue(verdict)
        self.assertEqual(slot.usage, payload)


class TestQuotaLimits(TempHome):
    """Current claude builds say which window and when, in `quotaLimits`."""

    def _record(self, **quota: object) -> dict:
        return {
            "message": {"content": [{"type": "text", "text": "something unfamiliar"}]},
            "quotaLimits": {"status": "rejected", **quota},
        }

    def test_the_window_comes_from_the_field_not_the_wording(self) -> None:
        record = self._record(rateLimitType="seven_day")
        self.assertEqual(autoswitch._window_of(record), autoswitch.SEVEN_DAY)

    def test_the_wording_is_only_a_fallback(self) -> None:
        record = {"message": {"content": [{"type": "text", "text": "hit your weekly limit"}]}}
        self.assertEqual(autoswitch._window_of(record), autoswitch.SEVEN_DAY)

    def test_the_reset_moment_is_lifted(self) -> None:
        now = 1789300000.0
        self.assertEqual(
            autoswitch._reset_of(self._record(resetsAt=1789317000), now=now), 1789317000.0
        )
        self.assertEqual(autoswitch._reset_of(self._record(), now=now), 0.0)
        self.assertEqual(autoswitch._reset_of({}, now=now), 0.0)

    def test_milliseconds_are_recognised_by_their_size(self) -> None:
        now = 1789300000.0
        self.assertEqual(
            autoswitch._reset_of(self._record(resetsAt=1789317000000), now=now),
            1789317000.0,
        )

    def test_a_reset_beyond_the_horizon_is_not_trusted(self) -> None:
        """An absurd reset would be waited for, or declined, in silence."""
        now = 1789300000.0
        far = now + autoswitch.RESET_HORIZON_SECONDS + 1
        self.assertEqual(autoswitch._reset_of(self._record(resetsAt=far), now=now), 0.0)
        near = now + autoswitch.RESET_HORIZON_SECONDS - 1
        self.assertEqual(autoswitch._reset_of(self._record(resetsAt=near), now=now), near)

    def test_the_signal_carries_the_reset(self) -> None:
        signal = autoswitch.LimitSignal()
        signal.fire(autoswitch.FIVE_HOUR, resets_at=123.0)
        self.assertEqual(signal.resets_at, 123.0)

        signal.rearm()
        self.assertFalse(signal.fired)
        self.assertEqual(signal.window, "")
        self.assertEqual(signal.resets_at, 0.0)

        signal.fire(autoswitch.SEVEN_DAY)
        self.assertTrue(signal.fired)
        self.assertEqual(signal.window, autoswitch.SEVEN_DAY)


class TestWaiting(TempHome):
    def _slot(self, number: int, five: float, seven: float, *, five_reset: float | None = None, seven_reset: float | None = None) -> Slot:
        self.write_credentials(store.creds_file(number))

        def stamp(offset: float | None) -> str | None:
            if offset is None:
                return None
            return datetime.fromtimestamp(time.time() + offset, timezone.utc).isoformat()

        return Slot(
            number=number,
            usage={
                "fetchedAtMs": time.time() * 1000,
                "five_hour": {"utilization": five, "resets_at": stamp(five_reset)},
                "seven_day": {"utilization": seven, "resets_at": stamp(seven_reset)},
            },
        )

    def _accounts(self, *slots: Slot) -> Accounts:
        accounts = Accounts()
        for slot in slots:
            accounts.slots[slot.number] = slot
        return accounts

    def test_a_window_past_its_reset_is_free_despite_the_stored_percent(self) -> None:
        """The very case that made ccas say every account was spent."""
        accounts = self._accounts(
            self._slot(1, 100, 90, five_reset=3600),
            self._slot(3, 100, 31, five_reset=-3600),
        )
        target, reason, opens = autoswitch.pick_target(
            accounts, current=1, tried={1}, threshold=100
        )
        self.assertEqual(target, 3)
        self.assertEqual(reason, "limits")
        self.assertEqual(opens, 0.0)

    def test_when_nobody_is_free_the_earliest_reset_wins(self) -> None:
        accounts = self._accounts(
            self._slot(1, 100, 90, five_reset=1800),
            self._slot(2, 100, 50, five_reset=3600),
            self._slot(3, 100, 20, five_reset=900),
        )
        target, reason, opens = autoswitch.pick_target(
            accounts, current=1, tried={1}, threshold=95
        )
        self.assertEqual(target, 3)
        self.assertEqual(reason, "waiting")
        self.assertAlmostEqual(opens, time.time() + 900, delta=5)

    def test_the_current_slot_may_be_the_one_to_wait_for(self) -> None:
        accounts = self._accounts(
            self._slot(1, 100, 10, five_reset=300),
            self._slot(2, 100, 10, five_reset=3600),
        )
        target, reason, opens = autoswitch.pick_target(
            accounts, current=1, tried={1}, threshold=95
        )
        self.assertEqual(target, 1)
        self.assertEqual(reason, "waiting")
        self.assertAlmostEqual(opens, time.time() + 300, delta=5)

    def test_the_transcript_reset_covers_a_silent_current_slot(self) -> None:
        accounts = self._accounts(
            self._slot(1, 100, 10),  # spent, but no reset moment recorded
            self._slot(2, 100, 10, five_reset=3600),
        )
        soon = time.time() + 120
        target, reason, opens = autoswitch.pick_target(
            accounts, current=1, tried={1}, threshold=95, current_reset=soon
        )
        self.assertEqual(target, 1)
        self.assertEqual(reason, "waiting")
        self.assertEqual(opens, soon)

    def test_both_windows_must_clear(self) -> None:
        payload = self._slot(1, 100, 100, five_reset=600, seven_reset=86400).usage
        opens = autoswitch.available_at(payload, threshold=95)
        self.assertAlmostEqual(opens, time.time() + 86400, delta=5)

    def test_a_spent_window_without_a_reset_cannot_be_waited_for(self) -> None:
        accounts = self._accounts(self._slot(1, 100, 10), self._slot(2, 100, 10))
        target, reason, _ = autoswitch.pick_target(
            accounts, current=1, tried={1}, threshold=95
        )
        self.assertIsNone(target)
        self.assertEqual(reason, "all_exhausted")

    def test_a_tried_slot_that_came_back_beats_waiting_here(self) -> None:
        """The evening's worst outcome: sitting out a reset next to a free slot.

        Slot 2 was used earlier and hit its wall; by now its window has rolled
        over. `candidates` keeps it out of the ranking, but `tried` is a fact
        about this session, not about the account's quota.
        """
        accounts = self._accounts(
            self._slot(1, 100, 10, five_reset=2700),
            self._slot(2, 100, 10, five_reset=-60),
        )
        target, reason, opens = autoswitch.pick_target(
            accounts, current=1, tried={1, 2}, threshold=95
        )
        self.assertEqual(target, 2)
        self.assertEqual(reason, "recovered")
        self.assertEqual(opens, 0.0)

    def test_a_tried_slot_nobody_has_numbers_for_is_not_called_free(self) -> None:
        accounts = self._accounts(
            self._slot(1, 100, 10, five_reset=2700),
            self._slot(2, 100, 10, five_reset=-60),
        )
        accounts.slots[2].usage["fetchedAtMs"] = (
            time.time() - usage.USAGE_TTL_RANKING_SECONDS - 60
        ) * 1000
        target, reason, opens = autoswitch.pick_target(
            accounts, current=1, tried={1, 2}, threshold=95
        )
        self.assertEqual(target, 1)
        self.assertEqual(reason, "waiting")
        self.assertAlmostEqual(opens, time.time() + 2700, delta=5)

    def test_a_current_slot_reading_free_does_not_win_over_a_real_reset(self) -> None:
        """We just got a 429 here; the snapshot saying otherwise is behind."""
        accounts = self._accounts(
            self._slot(1, 10, 10),
            self._slot(2, 100, 10, five_reset=900),
        )
        target, reason, opens = autoswitch.pick_target(
            accounts, current=1, tried={1, 2}, threshold=95
        )
        self.assertEqual(target, 2)
        self.assertEqual(reason, "waiting")
        self.assertAlmostEqual(opens, time.time() + 900, delta=5)

    def test_the_journal_says_a_tried_slot_is_free_again(self) -> None:
        accounts = self._accounts(
            self._slot(1, 100, 10, five_reset=2700),
            self._slot(2, 100, 10, five_reset=-60),
            self._slot(3, 100, 10, five_reset=1800),
        )
        verdicts = autoswitch.explain(accounts, current=1, tried={1, 2, 3}, threshold=95)
        self.assertEqual(verdicts[2], "tried-free")
        self.assertEqual(verdicts[3], "tried")

    def test_the_plan_outlives_the_wait(self) -> None:
        accounts = self._accounts(
            self._slot(1, 100, 10, five_reset=1800),
            self._slot(2, 100, 10, five_reset=900),
        )
        first = autoswitch.elect_target(accounts, current=1, tried={1}, threshold=95)
        self.assertEqual(first.target, 2)
        self.assertTrue(first.must_wait())

        later = autoswitch.elect_target(
            accounts, current=1, tried={1}, threshold=95, now=time.time() + 600
        )
        self.assertTrue(later.followed)
        self.assertEqual(later.available_at, first.available_at)
