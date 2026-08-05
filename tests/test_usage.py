from __future__ import annotations

from tests.base import TempHome
from ui import i18n, usage


class TestUsageFormatting(TempHome):
    def test_reset_time_and_percent(self) -> None:
        text = usage.format_usage(
            {
                "fetchedAtMs": None,
                "five_hour": {
                    "utilization": 46,
                    "resets_at": "2026-07-24T13:50:00.360619+00:00",
                },
            }
        )
        self.assertIn("46%", text)
        self.assertIn(i18n.t("usage.reset_at", time="").strip(), text)

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
