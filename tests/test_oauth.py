from __future__ import annotations

import subprocess
import sys
import time
from unittest import mock

from core import claudecfg, oauth, store
from core.store import Accounts, Slot
from tests.base import TempHome

HOUR_MS = 3600 * 1000


def _payload(**overrides: object) -> dict:
    body = {
        "access_token": "fresh-access",
        "refresh_token": "fresh-refresh",
        "expires_in": 28800,
        "scope": "user:inference user:profile",
    }
    body.update(overrides)
    return body


class TestMergeTokens(TempHome):
    def test_response_without_a_refresh_token_keeps_the_old_one(self) -> None:
        """Rotation is optional, and overwriting with nothing ends the slot."""
        merged = oauth.merge_tokens(
            {"accessToken": "old", "refreshToken": "keep-me"},
            _payload(refresh_token=None),
        )
        self.assertEqual(merged["refreshToken"], "keep-me")
        self.assertEqual(merged["accessToken"], "fresh-access")

    def test_rotated_refresh_token_is_taken(self) -> None:
        merged = oauth.merge_tokens({"refreshToken": "old"}, _payload())
        self.assertEqual(merged["refreshToken"], "fresh-refresh")

    def test_expiry_is_recomputed_from_expires_in(self) -> None:
        merged = oauth.merge_tokens({}, _payload(expires_in=100), now_ms=1_000_000)
        self.assertEqual(merged["expiresAt"], 1_100_000)

    def test_unknown_keys_survive(self) -> None:
        merged = oauth.merge_tokens(
            {"subscriptionType": "pro", "somethingNew": 7}, _payload()
        )
        self.assertEqual(merged["subscriptionType"], "pro")
        self.assertEqual(merged["somethingNew"], 7)

    def test_scopes_follow_the_response(self) -> None:
        merged = oauth.merge_tokens({"scopes": ["a"]}, _payload())
        self.assertEqual(merged["scopes"], ["user:inference", "user:profile"])


class TestNeedsRefresh(TempHome):
    def test_fresh_token_is_left_alone(self) -> None:
        now = 1_000_000.0
        oauth_block = {
            "refreshToken": "r",
            "expiresAt": now + 10 * HOUR_MS,
            "refreshTokenExpiresAt": now + 100 * HOUR_MS,
        }
        self.assertFalse(oauth.needs_refresh(oauth_block, now_ms=now))

    def test_expired_access_token_with_live_refresh_is_refreshable(self) -> None:
        now = 1_000_000.0
        oauth_block = {
            "refreshToken": "r",
            "expiresAt": now - 1,
            "refreshTokenExpiresAt": now + 100 * HOUR_MS,
        }
        self.assertTrue(oauth.needs_refresh(oauth_block, now_ms=now))

    def test_dead_refresh_token_is_not_refreshable(self) -> None:
        now = 1_000_000.0
        oauth_block = {
            "refreshToken": "r",
            "expiresAt": now - 1,
            "refreshTokenExpiresAt": now - 1,
        }
        self.assertFalse(oauth.needs_refresh(oauth_block, now_ms=now))

    def test_the_skew_refreshes_slightly_early(self) -> None:
        now = 1_000_000.0
        oauth_block = {
            "refreshToken": "r",
            "expiresAt": now + oauth.REFRESH_SKEW_MS / 2,
            "refreshTokenExpiresAt": now + 100 * HOUR_MS,
        }
        self.assertTrue(oauth.needs_refresh(oauth_block, now_ms=now))


class TestRefreshSlot(TempHome):
    def _expired_slot(self, number: int = 1, refresh_token: str = "old-refresh") -> None:
        now = time.time() * 1000
        self.write_credentials(
            store.creds_file(number),
            accessToken="stale-access",
            refreshToken=refresh_token,
            expiresAt=now - HOUR_MS,
            refreshTokenExpiresAt=now + 100 * HOUR_MS,
            subscriptionType="pro",
        )

    def test_happy_path_rewrites_the_credentials(self) -> None:
        self._expired_slot()
        self.assertEqual(claudecfg.token_state(store.creds_file(1)), "expired")

        with mock.patch.object(oauth, "_post_refresh", return_value=_payload()):
            outcome = oauth.refresh_slot(1, busy=set())

        self.assertEqual(outcome, oauth.REFRESHED)
        stored = claudecfg.read_credentials(store.creds_file(1))
        self.assertEqual(stored["accessToken"], "fresh-access")
        self.assertEqual(stored["subscriptionType"], "pro")
        self.assertEqual(claudecfg.token_state(store.creds_file(1)), "ok")

    def test_previous_credentials_are_backed_up(self) -> None:
        self._expired_slot()
        with mock.patch.object(oauth, "_post_refresh", return_value=_payload()):
            oauth.refresh_slot(1, busy=set())

        backups = list(store.backups_dir().glob(".credentials.json.*"))
        self.assertTrue(backups)

    def test_rotation_under_us_is_discarded(self) -> None:
        """claude refreshed the same slot while our POST was in flight.

        Writing our answer now would install a token the server has already
        superseded, logging the running session out.
        """
        self._expired_slot(refresh_token="old-refresh")

        def _rotate(*_args: object, **_kwargs: object) -> dict:
            self.write_credentials(
                store.creds_file(1),
                accessToken="written-by-claude",
                refreshToken="rotated-by-claude",
                expiresAt=time.time() * 1000 + HOUR_MS,
                refreshTokenExpiresAt=time.time() * 1000 + 100 * HOUR_MS,
            )
            return _payload()

        with mock.patch.object(oauth, "_post_refresh", side_effect=_rotate):
            outcome = oauth.refresh_slot(1, busy=set())

        self.assertEqual(outcome, oauth.SKIPPED)
        stored = claudecfg.read_credentials(store.creds_file(1))
        self.assertEqual(stored["accessToken"], "written-by-claude")

    def test_invalid_grant_is_terminal(self) -> None:
        self._expired_slot()
        with mock.patch.object(
            oauth, "_post_refresh", side_effect=oauth.OAuthError("dead", "invalid_grant")
        ):
            outcome = oauth.refresh_slot(1, busy=set())

        self.assertEqual(outcome, oauth.DEAD)
        stored = claudecfg.read_credentials(store.creds_file(1))
        self.assertEqual(stored["accessToken"], "stale-access")

    def test_transient_failure_is_retryable(self) -> None:
        self._expired_slot()
        with mock.patch.object(
            oauth, "_post_refresh", side_effect=oauth.OAuthError("transient", "timeout")
        ):
            self.assertEqual(oauth.refresh_slot(1, busy=set()), oauth.FAILED)

    def test_a_running_slot_is_never_touched(self) -> None:
        self._expired_slot()
        with mock.patch.object(oauth, "_post_refresh") as post:
            outcome = oauth.refresh_slot(1, busy={1})

        self.assertEqual(outcome, oauth.BUSY)
        post.assert_not_called()

    def test_busy_slots_are_derived_from_live_sessions(self) -> None:
        self._expired_slot()
        neighbour = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(neighbour.wait)
        self.addCleanup(neighbour.kill)

        store.write_json_atomic(
            store.app_dir() / "sessions" / f"{neighbour.pid}.json",
            {"pid": neighbour.pid, "slot": 1, "at": time.time()},
            harden=False,
        )

        with mock.patch.object(oauth, "_post_refresh") as post:
            outcome = oauth.refresh_slot(1)

        self.assertEqual(outcome, oauth.BUSY)
        post.assert_not_called()

    def test_a_fresh_token_is_not_exchanged(self) -> None:
        self.write_credentials(store.creds_file(1))  # expiry far in the future
        with mock.patch.object(oauth, "_post_refresh") as post:
            self.assertEqual(oauth.refresh_slot(1, busy=set()), oauth.SKIPPED)
        post.assert_not_called()

    def test_force_exchanges_even_a_fresh_token(self) -> None:
        self.write_credentials(store.creds_file(1))
        with mock.patch.object(oauth, "_post_refresh", return_value=_payload()) as post:
            self.assertEqual(
                oauth.refresh_slot(1, busy=set(), force=True), oauth.REFRESHED
            )
        post.assert_called_once()

    def test_slot_without_credentials_is_skipped(self) -> None:
        with mock.patch.object(oauth, "_post_refresh") as post:
            self.assertEqual(oauth.refresh_slot(9, busy=set()), oauth.SKIPPED)
        post.assert_not_called()

    def test_failure_puts_the_slot_on_cooldown(self) -> None:
        self._expired_slot()
        with mock.patch.object(
            oauth, "_post_refresh", side_effect=oauth.OAuthError("transient", "boom")
        ) as post:
            oauth.refresh_slot(1, busy=set())
            oauth.refresh_slot(1, busy=set())

        # The second call must not reach the network: a dead account would
        # otherwise cost a round trip on every single listing.
        post.assert_called_once()

    def test_cooldown_is_recorded_on_the_slot(self) -> None:
        self._expired_slot()
        with mock.patch.object(
            oauth, "_post_refresh", side_effect=oauth.OAuthError("dead", "invalid_grant")
        ):
            oauth.refresh_slot(1, busy=set())

        slot = Accounts.load().get(1)
        assert slot is not None
        self.assertEqual(slot.token.get("error"), oauth.DEAD)

    def test_token_bookkeeping_survives_a_round_trip(self) -> None:
        accounts = Accounts()
        accounts.slots[1] = Slot(number=1, token={"error": "dead"})
        accounts.save()
        self.assertEqual(Accounts.load().get(1).token, {"error": "dead"})
