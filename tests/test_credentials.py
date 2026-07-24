from __future__ import annotations

from core import claudecfg
from tests.base import TempHome


class TestTokenState(TempHome):
    def test_healthy_token(self) -> None:
        path = self.write_credentials(
            self.home / "creds.json", expiresAt=1_001_000, refreshTokenExpiresAt=1_005_000
        )
        self.assertEqual(claudecfg.token_state(path, now_ms=1_000_000), "ok")

    def test_expired_access_token_still_refreshable(self) -> None:
        path = self.write_credentials(
            self.home / "creds.json", expiresAt=999_999, refreshTokenExpiresAt=1_005_000
        )
        self.assertEqual(claudecfg.token_state(path, now_ms=1_000_000), "expired")

    def test_expired_refresh_token_needs_relogin(self) -> None:
        path = self.write_credentials(
            self.home / "creds.json", expiresAt=999_999, refreshTokenExpiresAt=999_999
        )
        self.assertEqual(claudecfg.token_state(path, now_ms=1_000_000), "stale")

    def test_absent_file(self) -> None:
        self.assertEqual(
            claudecfg.token_state(self.home / "absent.json", now_ms=1_000_000), "missing"
        )

    def test_access_token_is_extracted(self) -> None:
        path = self.write_credentials(self.home / "creds.json", accessToken="secret")
        self.assertEqual(claudecfg.access_token(path), "secret")

    def test_access_token_of_absent_file_is_none(self) -> None:
        self.assertIsNone(claudecfg.access_token(self.home / "absent.json"))
