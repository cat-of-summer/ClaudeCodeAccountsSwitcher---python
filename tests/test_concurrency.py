from __future__ import annotations

import os
import subprocess
import sys
import time

from app import wrapper
from core import store
from core.store import Accounts, Slot
from tests.base import TempHome

ALICE = "alice@example.com"
BOB = "bob@example.com"


class TestIdentityOwnership(TempHome):
    """Two terminals, two accounts, one shared ~/.claude.json.

    The wrapper reads that shared file when a session ends. Whatever it finds
    there may have been written by a claude still running in another window, so
    adopting it unconditionally used to collapse both slots onto one account.
    """

    def _slot(self, number: int, email: str) -> Slot:
        self.write_credentials(store.creds_file(number))
        return Slot(
            number=number,
            email=email,
            account_uuid=f"uuid-{email}",
            alias=email.split("@")[0],
        )

    def test_foreign_identity_is_not_adopted(self) -> None:
        accounts = Accounts()
        accounts.slots[1] = self._slot(1, ALICE)
        accounts.slots[2] = self._slot(2, BOB)

        # Window B has just patched the shared config with its own account.
        self.write_root_config(BOB)

        # Window A (slot 1) now finishes and does its bookkeeping.
        kept = wrapper.capture_identity(accounts.slots[1], accounts, time.time())

        self.assertIsNone(kept)
        self.assertEqual(accounts.slots[1].email, ALICE)
        self.assertEqual(accounts.slots[1].account_uuid, f"uuid-{ALICE}")
        self.assertFalse(store.identity_file(1).exists())

    def test_own_identity_is_still_captured(self) -> None:
        accounts = Accounts()
        accounts.slots[1] = self._slot(1, ALICE)
        self.write_root_config(ALICE, user_id="uid-alice")

        kept = wrapper.capture_identity(accounts.slots[1], accounts, time.time())

        self.assertIsNotNone(kept)
        self.assertEqual(accounts.slots[1].email, ALICE)
        self.assertEqual(accounts.slots[1].user_id, "uid-alice")
        self.assertTrue(store.identity_file(1).exists())

    def test_unknown_slot_refuses_a_uuid_owned_elsewhere(self) -> None:
        accounts = Accounts()
        accounts.slots[1] = self._slot(1, ALICE)
        accounts.slots[2] = Slot(number=2)  # adopted, identity not learned yet
        self.write_credentials(store.creds_file(2))
        self.write_root_config(ALICE)

        kept = wrapper.capture_identity(accounts.slots[2], accounts, time.time())

        self.assertIsNone(kept)
        self.assertEqual(accounts.slots[2].email, "")

    def test_relogin_with_a_different_account_is_accepted(self) -> None:
        """Signing a slot into another account must still be allowed."""
        accounts = Accounts()
        accounts.slots[1] = self._slot(1, ALICE)
        self.write_root_config(BOB)  # nobody else owns bob, nothing else running

        kept = wrapper.capture_identity(accounts.slots[1], accounts, time.time() - 60)

        self.assertIsNotNone(kept)
        self.assertEqual(accounts.slots[1].email, BOB)
        self.assertEqual(accounts.slots[1].account_uuid, f"uuid-{BOB}")

    def test_unfamiliar_account_is_refused_while_a_neighbour_runs(self) -> None:
        accounts = Accounts()
        accounts.slots[1] = self._slot(1, ALICE)
        self.write_root_config(BOB)

        neighbour = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(neighbour.wait)
        self.addCleanup(neighbour.kill)

        store.write_json_atomic(
            wrapper.sessions_dir() / f"{neighbour.pid}.json",
            {"pid": neighbour.pid, "slot": 2, "at": time.time()},
            harden=False,
        )

        self.assertEqual(len(wrapper.other_live_sessions()), 1)

        kept = wrapper.capture_identity(accounts.slots[1], accounts, time.time() - 60)

        self.assertIsNone(kept)
        self.assertEqual(accounts.slots[1].email, ALICE)

    def test_dead_sessions_are_reaped(self) -> None:
        store.write_json_atomic(
            wrapper.sessions_dir() / "424242.json",
            {"pid": 424242, "slot": 2, "at": time.time()},
            harden=False,
        )
        self.assertEqual(wrapper.other_live_sessions(), [])
        self.assertFalse((wrapper.sessions_dir() / "424242.json").exists())

    def test_own_session_is_not_counted(self) -> None:
        wrapper.register_session(1)
        self.assertEqual(wrapper.other_live_sessions(), [])
        wrapper.unregister_session()
        self.assertFalse((wrapper.sessions_dir() / f"{os.getpid()}.json").exists())

    def test_unknown_slot_needs_its_own_credentials_written(self) -> None:
        accounts = Accounts()
        accounts.slots[1] = Slot(number=1)
        self.write_credentials(store.creds_file(1))
        self.write_root_config(ALICE)

        # Credentials predate the session: claude never signed in on this slot.
        stale = wrapper.capture_identity(accounts.slots[1], accounts, time.time() + 60)
        self.assertIsNone(stale)

        # Written during the session: a genuine fresh login.
        adopted = wrapper.capture_identity(accounts.slots[1], accounts, time.time() - 60)
        self.assertIsNotNone(adopted)
        self.assertEqual(accounts.slots[1].email, ALICE)


class TestUsageOwnership(TempHome):
    def _cache(self, email: str, percent: int) -> None:
        store.write_json_atomic(
            self.home / ".claude.json",
            {
                "oauthAccount": {"emailAddress": email, "accountUuid": f"uuid-{email}"},
                "userID": "uid",
                "cachedUsageUtilization": {
                    "accountUuid": f"uuid-{email}",
                    "utilization": {"five_hour": {"utilization": percent}},
                },
            },
            harden=False,
        )

    def test_foreign_usage_is_ignored(self) -> None:
        slot = Slot(number=1, email=ALICE, account_uuid=f"uuid-{ALICE}")
        self._cache(BOB, 99)
        wrapper.capture_usage(slot)
        self.assertEqual(slot.usage, {})

    def test_own_usage_is_kept(self) -> None:
        slot = Slot(number=1, email=ALICE, account_uuid=f"uuid-{ALICE}")
        self._cache(ALICE, 42)
        wrapper.capture_usage(slot)
        self.assertEqual(slot.usage["five_hour"], {"utilization": 42})

    def test_unattributable_usage_is_ignored(self) -> None:
        slot = Slot(number=1, email=ALICE)  # no uuid known yet
        self._cache(ALICE, 42)
        wrapper.capture_usage(slot)
        self.assertEqual(slot.usage, {})


class TestStoreUpdates(TempHome):
    def test_stale_snapshot_does_not_roll_back_a_neighbour(self) -> None:
        seed = Accounts()
        seed.slots[1] = Slot(number=1, email=ALICE)
        seed.slots[2] = Slot(number=2, email=BOB)
        seed.save()

        # Window A loads the store and holds it for the life of the session.
        snapshot = Accounts.load()

        # Meanwhile another terminal renames slot 2.
        store.update_accounts(lambda current: setattr(current.ensure(2), "alias", "work"))

        # Window A exits and records its own slot.
        def _bookkeep(current: Accounts) -> None:
            current.ensure(1).last_used_at = 1234.0

        store.update_accounts(_bookkeep)

        reloaded = Accounts.load()
        self.assertEqual(reloaded.get(2).alias, "work")
        self.assertEqual(reloaded.get(1).last_used_at, 1234.0)
        self.assertEqual(snapshot.get(2).alias, "")  # the snapshot was indeed stale

    def test_lock_is_reentrant_across_sequential_updates(self) -> None:
        store.update_accounts(lambda current: current.ensure(1))
        store.update_accounts(lambda current: current.ensure(2))
        self.assertEqual(sorted(Accounts.load().slots), [1, 2])

    def test_lock_is_released_after_a_failing_mutation(self) -> None:
        def _boom(current: Accounts) -> None:
            raise RuntimeError("mutation failed")

        with self.assertRaises(RuntimeError):
            store.update_accounts(_boom)

        # A leaked lock would hang the next writer until the timeout.
        started = time.monotonic()
        store.update_accounts(lambda current: current.ensure(9))
        self.assertLess(time.monotonic() - started, store.LOCK_TIMEOUT)
        self.assertIn(9, Accounts.load().slots)


class TestDefaultSlot(TempHome):
    def test_active_slot_wins(self) -> None:
        accounts = Accounts()
        accounts.slots[1] = Slot(number=1)
        accounts.slots[2] = Slot(number=2)
        self.write_credentials(store.creds_file(1))
        self.write_credentials(store.creds_file(2))
        accounts.active = 2
        self.assertEqual(wrapper.default_slot(accounts), 2)

    def test_falls_back_to_most_recent_when_active_has_no_login(self) -> None:
        accounts = Accounts()
        accounts.slots[1] = Slot(number=1, last_used_at=100.0)
        accounts.slots[2] = Slot(number=2, last_used_at=500.0)
        accounts.slots[3] = Slot(number=3)  # active but never signed in
        self.write_credentials(store.creds_file(1))
        self.write_credentials(store.creds_file(2))
        accounts.active = 3
        self.assertEqual(wrapper.default_slot(accounts), 2)

    def test_empty_store_offers_the_first_slot(self) -> None:
        self.assertEqual(wrapper.default_slot(Accounts()), 1)
