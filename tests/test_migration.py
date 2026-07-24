from __future__ import annotations

import json
from pathlib import Path

from app import installer
from core import claudecfg, store
from core.store import Accounts, Slot
from tests.base import TempHome


class LegacyStoreMixin(TempHome):
    def legacy_slot(self, number: int, email: str) -> Path:
        legacy = self.home / ".claude-accounts"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / f"account-{number}.credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": f"tok{number}"}}),
            encoding="utf-8",
        )
        (legacy / f"account-{number}.identity.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": email,
                        "accountUuid": f"uuid-{number}",
                    },
                    "userID": f"user-{number}",
                }
            ),
            encoding="utf-8",
        )
        return legacy

    def write_shared_login(self, email: str, user_id: str = "uid") -> None:
        self.write_root_config(email, user_id=user_id)
        store.write_json_atomic(
            claudecfg.shared_credentials_path(),
            {"claudeAiOauth": {"accessToken": "live"}},
        )


class TestLegacyMigration(LegacyStoreMixin):
    def test_imports_slots_and_active_marker(self) -> None:
        legacy = self.legacy_slot(1, "one@example.com")
        self.legacy_slot(2, "two@example.com")
        (legacy / ".active").write_text("2", encoding="utf-8")

        accounts = Accounts()
        imported = installer.migrate_legacy_store(accounts)

        self.assertEqual(sorted(imported), [1, 2])
        self.assertEqual(accounts.active, 2)
        self.assertEqual(accounts.get(1).email, "one@example.com")
        self.assertEqual(accounts.get(2).account_uuid, "uuid-2")
        self.assertTrue(store.creds_file(1).exists())
        self.assertTrue(store.identity_file(2).exists())

    def test_migration_does_not_clobber_existing_slots(self) -> None:
        self.legacy_slot(1, "one@example.com")
        store.ensure_slot_dir(1)
        store.write_json_atomic(
            store.creds_file(1), {"claudeAiOauth": {"accessToken": "current"}}
        )

        accounts = Accounts()
        self.assertEqual(installer.migrate_legacy_store(accounts), [])
        data = json.loads(store.creds_file(1).read_text(encoding="utf-8"))
        self.assertEqual(data["claudeAiOauth"]["accessToken"], "current")

    def test_legacy_directory_is_left_in_place(self) -> None:
        legacy = self.legacy_slot(1, "one@example.com")
        installer.migrate_legacy_store(Accounts())
        self.assertTrue(legacy.exists())

    def test_no_legacy_store_is_not_an_error(self) -> None:
        self.assertEqual(installer.migrate_legacy_store(Accounts()), [])


class TestAdoptCurrentLogin(LegacyStoreMixin):
    def test_existing_session_becomes_slot_one(self) -> None:
        self.write_shared_login("live@example.com", user_id="live-uid")

        accounts = Accounts()
        self.assertEqual(installer.adopt_current_login(accounts), 1)
        self.assertEqual(accounts.active, 1)
        self.assertEqual(accounts.get(1).email, "live@example.com")
        self.assertEqual(accounts.get(1).user_id, "live-uid")
        self.assertTrue(store.creds_file(1).exists())

    def test_skipped_when_the_same_account_is_already_stored(self) -> None:
        self.write_shared_login("live@example.com")
        accounts = Accounts()
        accounts.slots[1] = Slot(
            number=1, email="live@example.com", account_uuid="uuid-live@example.com"
        )
        self.assertIsNone(installer.adopt_current_login(accounts))

    def test_nothing_to_adopt_without_credentials(self) -> None:
        self.assertIsNone(installer.adopt_current_login(Accounts()))

    def test_live_login_survives_a_legacy_migration(self) -> None:
        self.legacy_slot(2, "second@example.com")
        self.write_shared_login("live@example.com")

        accounts = Accounts()
        installer.migrate_legacy_store(accounts)
        adopted = installer.adopt_current_login(accounts)

        self.assertIsNotNone(adopted)
        self.assertEqual(accounts.get(adopted).email, "live@example.com")
        self.assertEqual(sorted(accounts.slots), [1, 2])
