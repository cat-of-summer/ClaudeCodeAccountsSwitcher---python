from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest import mock

from core import store
from core.store import Accounts, Slot, creds_file
from tests.base import TempHome
from ui import i18n, usage

FIVE_HOUR_RESET = "2036-07-24T13:50:00.360619+00:00"
PAST_RESET = "2026-07-24T13:50:00.360619+00:00"
SEVEN_DAY_RESET = "2036-07-31T09:00:00+00:00"


def reset_mark() -> str:
    return i18n.t("usage.reset_at", time="").strip()


class TestUsageFormatting(TempHome):
    def test_reset_time_and_percent(self) -> None:
        text = usage.format_usage(
            {
                "fetchedAtMs": None,
                "five_hour": {"utilization": 46, "resets_at": FIVE_HOUR_RESET},
            }
        )
        self.assertIn("46%", text)
        self.assertIn(reset_mark(), text)

    def test_reset_shows_date_and_time(self) -> None:
        # The expected value cannot be a literal: it depends on the zone of the
        # machine running the tests, which is exactly what the render converts to.
        moment = usage.parse_iso(FIVE_HOUR_RESET)
        assert moment is not None
        expected = moment.strftime(i18n.t("usage.reset_format"))

        self.assertIn(expected, usage.format_reset(FIVE_HOUR_RESET))
        self.assertNotEqual(expected, moment.strftime("%H:%M"))

    def test_both_windows_carry_their_own_reset(self) -> None:
        text = usage.format_usage(
            {
                "five_hour": {"utilization": 46, "resets_at": FIVE_HOUR_RESET},
                "seven_day": {"utilization": 96, "resets_at": SEVEN_DAY_RESET},
            }
        )
        self.assertEqual(text.count(reset_mark()), 2)


    def test_a_window_past_its_reset_reads_as_empty(self) -> None:
        """The recorded 100 % describes a window that has since rolled over."""
        text = usage.format_usage(
            {
                "five_hour": {"utilization": 100, "resets_at": PAST_RESET},
                "seven_day": {"utilization": 96, "resets_at": SEVEN_DAY_RESET},
            }
        )
        self.assertIn("5h 0%", text)
        self.assertIn("96%", text)
        # The stale reset moment is not printed next to the zero it contradicts.
        self.assertEqual(text.count(reset_mark()), 1)

    def test_effective_utilisation_honours_the_reset_moment(self) -> None:
        payload = {"five_hour": {"utilization": 100, "resets_at": PAST_RESET}}
        self.assertEqual(usage.effective_utilisation(payload, usage.FIVE_HOUR), 0.0)

        live = {"five_hour": {"utilization": 100, "resets_at": FIVE_HOUR_RESET}}
        self.assertEqual(usage.effective_utilisation(live, usage.FIVE_HOUR), 100.0)

        # No reset moment at all: nothing to reason from, the number stands.
        bare = {"five_hour": {"utilization": 100, "resets_at": None}}
        self.assertEqual(usage.effective_utilisation(bare, usage.FIVE_HOUR), 100.0)
        self.assertIsNone(usage.effective_utilisation(bare, usage.SEVEN_DAY))

    def test_window_without_reset_is_clean(self) -> None:
        text = usage.format_usage(
            {"five_hour": {"utilization": 46}, "seven_day": {"utilization": 96}}
        )
        self.assertIn("46%", text)
        self.assertNotIn(reset_mark(), text)
        self.assertNotIn("  ", text)

    def test_reset_format_falls_back(self) -> None:
        catalog = i18n._catalogs[i18n.current_language()]
        with mock.patch.dict(catalog):
            catalog.pop("usage.reset_format", None)
            text = usage.format_reset(FIVE_HOUR_RESET)

        self.assertNotIn("usage.reset_format", text)
        moment = usage.parse_iso(FIVE_HOUR_RESET)
        assert moment is not None
        self.assertIn(moment.strftime(usage.DEFAULT_RESET_FORMAT), text)

    def test_unparsable_reset_renders_nothing(self) -> None:
        self.assertEqual(usage.format_reset(None), "")
        self.assertEqual(usage.format_reset("not a timestamp"), "")

    def test_missing_data(self) -> None:
        self.assertEqual(usage.format_usage(None), i18n.t("usage.unknown"))
        self.assertTrue(usage.is_unknown(usage.format_usage(None)))

    def test_malformed_block_is_not_fatal(self) -> None:
        self.assertTrue(usage.is_unknown(usage.format_usage({"five_hour": "nonsense"})))

    def test_percent_may_be_absent(self) -> None:
        text = usage.format_usage({"five_hour": {"resets_at": None}})
        self.assertEqual(text, i18n.t("usage.five_hour_unknown"))

    def test_age_buckets(self) -> None:
        self.assertEqual(usage.age_text(None), "")
        self.assertEqual(usage.age_text(0), "")

    def test_weekly_window_is_shown(self) -> None:
        text = usage.format_usage(
            {
                "five_hour": {"utilization": 12},
                "seven_day": {"utilization": 96},
            }
        )
        self.assertIn("12%", text)
        self.assertIn("96%", text)

    def test_weekly_window_may_be_absent(self) -> None:
        text = usage.format_usage({"five_hour": {"utilization": 12}, "seven_day": None})
        self.assertIn("12%", text)


class TestUsageFreshness(TempHome):
    def _now_ms(self) -> float:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).timestamp() * 1000

    def test_absent_usage_is_stale(self) -> None:
        self.assertTrue(usage.is_stale(None))
        self.assertTrue(usage.is_stale({}))

    def test_missing_timestamp_is_stale(self) -> None:
        self.assertTrue(usage.is_stale({"five_hour": {"utilization": 1}}))

    def test_recent_usage_is_fresh(self) -> None:
        self.assertFalse(usage.is_stale({"fetchedAtMs": self._now_ms()}))

    def test_usage_past_the_ttl_is_stale(self) -> None:
        old = self._now_ms() - (usage.USAGE_TTL_SECONDS + 60) * 1000
        self.assertTrue(usage.is_stale({"fetchedAtMs": old}))


class TestDescribe(TempHome):
    """The listing column. An expired token used to wipe out the numbers."""

    def _slot(self, number: int = 1) -> Slot:
        return Slot(
            number=number,
            usage={
                "fetchedAtMs": datetime.now(timezone.utc).timestamp() * 1000,
                "five_hour": {"utilization": 46},
                "seven_day": {"utilization": 96},
            },
        )

    def test_expired_token_keeps_the_numbers(self) -> None:
        text = usage.describe(self._slot(), state="expired")
        self.assertIn("46%", text)
        self.assertIn("96%", text)
        self.assertIn(i18n.t("menu.token_self_refresh"), text)

    def test_healthy_token_is_just_the_numbers(self) -> None:
        text = usage.describe(self._slot(), state="ok")
        self.assertIn("96%", text)
        self.assertNotIn(i18n.t("menu.token_self_refresh"), text)

    def test_a_slot_needing_a_relogin_says_so(self) -> None:
        text = usage.describe(self._slot(), state="stale")
        self.assertEqual(text, i18n.t("menu.slot_relogin"))

    def test_a_slot_without_a_login_says_so(self) -> None:
        text = usage.describe(self._slot(), state="missing")
        self.assertEqual(text, i18n.t("menu.slot_no_login"))

    def test_colour_is_opt_in(self) -> None:
        plain = usage.describe(self._slot(), state="expired")
        painted = usage.describe(self._slot(), state="expired", colour=True)
        self.assertNotIn("\033", plain)
        self.assertIn("\033", painted)

    def test_state_is_read_from_disk_when_not_given(self) -> None:
        self.write_credentials(creds_file(1))
        self.assertIn("46%", usage.describe(self._slot()))


class TestRefreshOne(TempHome):
    def test_an_expired_token_is_exchanged_before_asking(self) -> None:
        self.write_credentials(creds_file(1), expiresAt=1)
        with mock.patch.object(usage.oauth, "refresh_slot") as refresh, mock.patch.object(
            usage, "fetch_live_ex", return_value=({"five_hour": {"utilization": 1}}, 200)
        ):
            usage._refresh_one(1, busy=set(), timeout=1, refresh_timeout=1)
        refresh.assert_called_once()

    def test_a_401_costs_exactly_one_forced_exchange(self) -> None:
        self.write_credentials(creds_file(1))
        answers = [(None, 401), ({"five_hour": {"utilization": 5}}, 200)]

        with mock.patch.object(
            usage.oauth, "refresh_slot", return_value=usage.oauth.REFRESHED
        ) as refresh, mock.patch.object(
            usage, "fetch_live_ex", side_effect=answers
        ) as fetch:
            result = usage._refresh_one(1, busy=set(), timeout=1, refresh_timeout=1)

        refresh.assert_called_once()
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result["five_hour"], {"utilization": 5})

    def test_a_slot_needing_a_relogin_is_not_asked_at_all(self) -> None:
        self.write_credentials(creds_file(1), expiresAt=1, refreshTokenExpiresAt=1)
        with mock.patch.object(usage, "fetch_live_ex") as fetch:
            self.assertIsNone(
                usage._refresh_one(1, busy=set(), timeout=1, refresh_timeout=1)
            )
        fetch.assert_not_called()

    def test_refreshable_now_includes_expired_tokens(self) -> None:
        self.write_credentials(creds_file(1), expiresAt=1)  # expired, refreshable
        self.write_credentials(creds_file(2), expiresAt=1, refreshTokenExpiresAt=1)
        slots = [Slot(number=1), Slot(number=2), Slot(number=3)]
        self.assertEqual([s.number for s in usage.refreshable(slots)], [1])


class TestFreshnessTiers(TempHome):
    def _aged(self, seconds: float) -> dict:
        now = datetime.now(timezone.utc).timestamp() * 1000
        return {"fetchedAtMs": now - seconds * 1000}

    def test_the_active_slot_goes_stale_sooner(self) -> None:
        recent = Slot(number=1, usage=self._aged(usage.USAGE_TTL_SECONDS + 60))
        self.assertTrue(usage.needs_network(recent, active=True))
        self.assertFalse(usage.needs_network(recent, active=False))

    def test_idle_slots_still_expire(self) -> None:
        old = Slot(number=1, usage=self._aged(usage.USAGE_TTL_IDLE_SECONDS + 60))
        self.assertTrue(usage.needs_network(old, active=False))

    def test_ranking_tolerates_a_much_older_snapshot(self) -> None:
        """The weekly window cannot move fast enough to invalidate it."""
        aged = self._aged(usage.USAGE_TTL_IDLE_SECONDS + 60)
        self.assertTrue(usage.is_stale(aged))
        self.assertTrue(usage.is_usable_for_ranking(aged))

        ancient = self._aged(usage.USAGE_TTL_RANKING_SECONDS + 60)
        self.assertFalse(usage.is_usable_for_ranking(ancient))


class TestPayloadShapes(TempHome):
    def test_top_level_windows(self) -> None:
        """The live endpoint returns the windows at the top level."""
        normalised = usage._normalise(
            {"five_hour": {"utilization": 100.0}, "seven_day": {"utilization": 96.0}}
        )
        self.assertEqual(normalised["five_hour"], {"utilization": 100.0})
        self.assertEqual(normalised["seven_day"], {"utilization": 96.0})

    def test_nested_windows_still_work(self) -> None:
        normalised = usage._normalise(
            {"utilization": {"five_hour": {"utilization": 7.0}, "seven_day": None}}
        )
        self.assertEqual(normalised["five_hour"], {"utilization": 7.0})


class TestNamingSlots(TempHome):
    def test_an_unnamed_slot_gets_its_email_from_the_profile(self) -> None:
        self.write_credentials(creds_file(2))
        accounts = Accounts()
        accounts.slots[2] = Slot(number=2)
        accounts.save()
        slots = [Slot(number=2)]

        profile = {"email": "two@example.com", "account_uuid": "uuid-2"}
        with mock.patch.object(usage.oauth, "fetch_profile", return_value=profile) as fetch:
            self.assertEqual(usage.name_slots(slots), 1)

        fetch.assert_called_once()
        self.assertEqual(slots[0].email, "two@example.com")
        self.assertEqual(Accounts.load().slots[2].email, "two@example.com")
        self.assertEqual(Accounts.load().slots[2].account_uuid, "uuid-2")
        # And written where the offline backfill will find it next time.
        saved = store.read_json(store.identity_file(2))
        self.assertEqual(saved["oauthAccount"]["emailAddress"], "two@example.com")

    def test_named_slots_cost_no_request(self) -> None:
        self.write_credentials(creds_file(1))
        with mock.patch.object(usage.oauth, "fetch_profile") as fetch:
            self.assertEqual(usage.name_slots([Slot(number=1, email="a@b")]), 0)
        fetch.assert_not_called()

    def test_a_slot_without_a_token_is_skipped(self) -> None:
        with mock.patch.object(usage.oauth, "fetch_profile") as fetch:
            self.assertEqual(usage.name_slots([Slot(number=7)]), 0)
        fetch.assert_not_called()


class TestRefreshDeadline(TempHome):
    def test_one_stuck_slot_does_not_hold_the_others_hostage(self) -> None:
        """A name lookup that ignores its timeout must not become our hang."""
        for number in (1, 2):
            self.write_credentials(creds_file(number))

        def _answer(number: int, **_kw: object) -> dict | None:
            if number == 2:
                time.sleep(60)  # never returns within the budget
            return {"fetchedAtMs": 1, "five_hour": {"utilization": 3}, "seven_day": None}

        with mock.patch.object(usage, "_refresh_one", side_effect=_answer), mock.patch.object(
            usage, "_budget", return_value=0.5
        ), mock.patch.object(usage, "name_slots", return_value=0):
            started = time.monotonic()
            result = usage.refresh_slots([Slot(number=1), Slot(number=2)])

        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(set(result), {1})
