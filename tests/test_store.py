from __future__ import annotations

from core import store
from core.store import Accounts, Config, Slot
from tests.base import TempHome


class TestAccountsStore(TempHome):
    def test_round_trip(self) -> None:
        accounts = Accounts()
        slot = accounts.ensure(3)
        slot.alias = "work"
        slot.email = "w@example.com"
        accounts.active = 3
        accounts.save()

        reloaded = Accounts.load()
        self.assertEqual(reloaded.active, 3)
        self.assertEqual(reloaded.get(3).alias, "work")

    def test_alias_and_email_lookup(self) -> None:
        accounts = Accounts()
        accounts.slots[1] = Slot(number=1, alias="work", email="w@example.com")
        self.assertEqual(accounts.by_alias("WORK").number, 1)
        self.assertEqual(len(accounts.by_email("w@")), 1)
        self.assertTrue(accounts.alias_taken("work"))
        self.assertFalse(accounts.alias_taken("work", excluding=1))

    def test_next_free_number_fills_gaps(self) -> None:
        accounts = Accounts()
        accounts.slots[1] = Slot(number=1)
        accounts.slots[3] = Slot(number=3)
        self.assertEqual(accounts.next_free_number(), 2)

    def test_removing_active_slot_clears_active(self) -> None:
        accounts = Accounts()
        accounts.slots[1] = Slot(number=1)
        accounts.active = 1
        accounts.remove(1)
        self.assertEqual(accounts.active, 0)

    def test_config_round_trip(self) -> None:
        Config(real_claude_path="/x/claude", cred_mode="copy", language="ru").save()
        reloaded = Config.load()
        self.assertEqual(reloaded.cred_mode, "copy")
        self.assertEqual(reloaded.real_claude_path, "/x/claude")
        self.assertEqual(reloaded.language, "ru")


class TestAtomicWrite(TempHome):
    def test_no_temp_files_left_behind(self) -> None:
        target = self.home / "out.json"
        store.write_json_atomic(target, {"a": 1}, harden=False)
        leftovers = [p for p in self.home.iterdir() if p.name.startswith("out.json.")]
        self.assertEqual(leftovers, [])

    def test_non_ascii_is_not_escaped(self) -> None:
        target = self.home / "out.json"
        store.write_json_atomic(target, {"k": "привет"}, harden=False)
        self.assertIn("привет", target.read_text(encoding="utf-8"))

    def test_written_json_has_no_bom(self) -> None:
        target = self.home / "out.json"
        store.write_json_atomic(target, {"a": 1}, harden=False)
        self.assertFalse(target.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_bom_is_tolerated_on_read(self) -> None:
        target = self.home / "bom.json"
        target.write_bytes(b"\xef\xbb\xbf" + b'{"a": 1}')
        self.assertEqual(store.read_json(target), {"a": 1})

    def test_backup_rotation_keeps_limit(self) -> None:
        target = self.home / "big.json"
        store.write_json_atomic(target, {"a": 1}, harden=False)
        for _ in range(store.BACKUP_KEEP + 5):
            store.backup_file(target, keep=3)
        backups = list(store.backups_dir().glob("big.json.*"))
        self.assertLessEqual(len(backups), 3)
