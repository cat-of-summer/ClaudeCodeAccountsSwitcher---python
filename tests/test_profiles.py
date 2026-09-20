from __future__ import annotations

from pathlib import Path

from app.transport import profiles as profiles_module
from app.transport.profiles import Profile
from core import store
from core.store import Config
from core.version import SCHEMA_VERSION
from tests.base import TempHome


class Defaults(TempHome):
    def test_the_default_profile_exists_without_being_written(self) -> None:
        known = profiles_module.load(Config())
        self.assertEqual(list(known), ["default"])
        default = known["default"]
        self.assertTrue(default.is_default)
        self.assertEqual(default.alias, "")
        self.assertEqual(default.chats, ())
        self.assertFalse(default.daemon)
        self.assertFalse(default.multi)

    def test_a_profile_round_trips_through_the_config(self) -> None:
        Config().save()
        profiles_module.save(
            Profile(
                name="rikroot",
                chats=((-5595440781, 0), (-100500, 7)),
                cwd=str(self.home),
                slot=3,
                daemon=True,
                multi=True,
                users=(7,),
                args=("--model", "opus"),
            )
        )
        loaded = profiles_module.load(Config.load())["rikroot"]
        self.assertEqual(loaded.chats, ((-5595440781, 0), (-100500, 7)))
        self.assertEqual((loaded.slot, loaded.daemon, loaded.multi), (3, True, True))
        self.assertEqual(loaded.args, ("--model", "opus"))
        self.assertEqual(loaded.users, (7,))
        raw = store.read_json(store.config_path())["telegram"]["profiles"]["rikroot"]
        self.assertEqual(raw["chats"], [-5595440781, "-100500:7"])

    def test_removing_resets_the_default_and_drops_the_rest(self) -> None:
        Config().save()
        profiles_module.save(Profile(name="default", chats=((-1, 0),), daemon=True))
        profiles_module.save(Profile(name="gone", cwd=str(self.home)))
        profiles_module.remove("gone")
        profiles_module.remove("default")
        known = profiles_module.load(Config.load())
        self.assertEqual(list(known), ["default"])
        self.assertEqual(known["default"].chats, ())
        self.assertFalse(known["default"].daemon)


class Matching(TempHome):
    def test_claims_and_open_to(self) -> None:
        anywhere = Profile(name="default")
        self.assertFalse(anywhere.claims(-1))
        self.assertTrue(anywhere.open_to(-1))

        pinned = Profile(name="rikroot", chats=((-5, 0), (-6, 7)))
        self.assertTrue(pinned.claims(-5))
        self.assertTrue(pinned.claims(-5, 99))  # no topic named: the whole chat
        self.assertTrue(pinned.claims(-6, 7))
        self.assertFalse(pinned.claims(-6, 8))
        self.assertFalse(pinned.open_to(-7))

    def test_users_narrow_the_global_list_and_never_widen_it(self) -> None:
        anyone = Profile(name="default")
        self.assertTrue(anyone.allows(7))
        self.assertTrue(anyone.allows(7, (7, 8)))
        self.assertFalse(anyone.allows(9, (7, 8)))

        narrow = Profile(name="rikroot", users=(8,))
        self.assertTrue(narrow.allows(8, (7, 8)))
        self.assertFalse(narrow.allows(7, (7, 8)))
        self.assertFalse(narrow.allows(8, (7,)))

    def test_chat_entries_are_read_leniently(self) -> None:
        parse = profiles_module.parse_chat
        self.assertEqual(parse(-100), (-100, 0))
        self.assertEqual(parse("-100:7"), (-100, 7))
        self.assertEqual(parse(" -100 "), (-100, 0))
        self.assertIsNone(parse("nonsense"))
        self.assertIsNone(parse(""))
        self.assertIsNone(parse(True))

    def test_the_directory_falls_back_through_the_chain(self) -> None:
        config = Config()
        project = self.home / "proj"
        project.mkdir()
        self.assertEqual(Profile(name="x", cwd=str(project)).resolve_cwd(config), project)

        config.telegram = {**config.telegram, "workdir": str(self.home)}
        self.assertEqual(Profile(name="x").resolve_cwd(config), self.home)
        self.assertEqual(Profile(name="x", cwd="/nowhere").resolve_cwd(config), self.home)

        config.telegram = {**config.telegram, "workdir": ""}
        self.assertEqual(Profile(name="x").resolve_cwd(config), Path.home())

    def test_daemon_wanted_is_derived_from_the_profiles(self) -> None:
        Config().save()
        self.assertFalse(profiles_module.daemon_wanted(Config.load()))
        profiles_module.save(Profile(name="rikroot", daemon=True))
        self.assertTrue(profiles_module.daemon_wanted(Config.load()))


class Migration(TempHome):
    def test_schema_four_settings_become_the_default_profile(self) -> None:
        store.write_json_atomic(
            store.config_path(),
            {
                "schema": 4,
                "realClaudePath": "/bin/claude",
                "telegram": {
                    "token": "1:abc",
                    "chat": -100500,
                    "thread": 7,
                    "daemon": True,
                    "sessions": {"rikroot": {"chat": -5595440781, "cwd": "/tmp", "slot": 2}},
                },
            },
            harden=False,
        )
        self.assertTrue(store.migrate_config())

        config = Config.load()
        self.assertEqual(config.schema, SCHEMA_VERSION)
        self.assertNotIn("chat", config.telegram)
        self.assertNotIn("sessions", config.telegram)
        self.assertNotIn("daemon", config.telegram)

        known = profiles_module.load(config)
        self.assertEqual(known["default"].chats, ((-100500, 7),))
        self.assertTrue(known["default"].daemon)
        self.assertEqual(known["rikroot"].chats, ((-5595440781, 0),))
        self.assertEqual(known["rikroot"].slot, 2)
        self.assertEqual(known["rikroot"].cwd, "/tmp")
        self.assertFalse(known["rikroot"].daemon)

    def test_migration_is_idempotent(self) -> None:
        store.write_json_atomic(
            store.config_path(), {"schema": 4, "telegram": {"chat": -1}}, harden=False
        )
        store.migrate_config()
        first = Config.load().telegram
        self.assertFalse(store.migrate_config())
        self.assertEqual(Config.load().telegram, first)
