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
