from __future__ import annotations

from unittest import mock

from tests.base import TempHome
from ui import i18n, usage

FIVE_HOUR_RESET = "2026-07-24T13:50:00.360619+00:00"
SEVEN_DAY_RESET = "2026-07-31T09:00:00+00:00"


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
        moment = usage._parse_reset(FIVE_HOUR_RESET)
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
        moment = usage._parse_reset(FIVE_HOUR_RESET)
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

    def test_token_state_labels_exist(self) -> None:
        for state in ("expired", "stale", "missing"):
            self.assertTrue(usage.format_token_state(state))
        self.assertEqual(usage.format_token_state("ok"), "")

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
