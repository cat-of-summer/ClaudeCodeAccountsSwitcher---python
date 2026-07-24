from __future__ import annotations

import json

from core import claudecfg, store
from tests.base import TempHome


class TestConfigTargets(TempHome):
    def test_root_config_is_used_by_default(self) -> None:
        self.write_root_config("a@example.com")
        self.assertEqual(claudecfg.active_config_target(), self.home / ".claude.json")

    def test_nested_config_json_wins(self) -> None:
        self.write_root_config("a@example.com")
        nested = self.home / ".claude" / ".config.json"
        store.write_json_atomic(nested, {"oauthAccount": None}, harden=False)
        self.assertEqual(claudecfg.active_config_target(), nested)

    def test_existing_targets_include_the_inert_nested_file(self) -> None:
        self.write_root_config("a@example.com")
        inert = self.home / ".claude" / ".claude.json"
        store.write_json_atomic(inert, {"oauthAccount": None}, harden=False)
        targets = claudecfg.existing_config_targets()
        self.assertIn(self.home / ".claude.json", targets)
        self.assertIn(inert, targets)


class TestIdentityPatching(TempHome):
    def test_patch_writes_every_existing_target(self) -> None:
        root = self.write_root_config("old@example.com")
        inert = self.home / ".claude" / ".claude.json"
        store.write_json_atomic(
            inert, {"oauthAccount": {"emailAddress": "other@example.com"}}, harden=False
        )

        written = claudecfg.patch_identity(self.identity("new@example.com"))
        self.assertEqual(len(written), 2)

        for path in (root, inert):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["oauthAccount"]["emailAddress"], "new@example.com")

    def test_unrelated_keys_survive(self) -> None:
        root = self.write_root_config("old@example.com")
        claudecfg.patch_identity(self.identity("new@example.com"))
        data = json.loads(root.read_text(encoding="utf-8"))
        self.assertEqual(data["numStartups"], 7)

    def test_matching_identity_is_not_rewritten(self) -> None:
        self.write_root_config("same@example.com")
        identity = self.identity("same@example.com")
        claudecfg.patch_identity(identity)
        self.assertEqual(claudecfg.patch_identity(identity), [])

    def test_write_has_no_bom(self) -> None:
        root = self.write_root_config("old@example.com")
        claudecfg.patch_identity(self.identity("new@example.com"))
        self.assertFalse(root.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_clear_identity_blanks_oauth_account(self) -> None:
        root = self.write_root_config("old@example.com")
        claudecfg.clear_identity()
        data = json.loads(root.read_text(encoding="utf-8"))
        self.assertIsNone(data["oauthAccount"])
