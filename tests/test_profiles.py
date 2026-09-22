from __future__ import annotations

from pathlib import Path

from app.transport import profiles as profiles_module
from app.transport.profiles import Profile
from core import store
from core.store import Config
from core.version import SCHEMA_VERSION
from tests.base import TempHome


class Storage(TempHome):
    def test_nothing_exists_until_something_is_added(self) -> None:
        self.assertEqual(profiles_module.load(Config()), {})

    def test_adding_hands_out_ids_and_a_profile_round_trips(self) -> None:
        Config().save()
        first = profiles_module.add(
            Profile(
                alias="rikroot",
                chats=((-5595440781, 0), (-100500, 7)),
                cwd=str(self.home),
                slot=3,
                daemon=True,
                multi=True,
                debug=True,
                users=(7,),
                args=("--model", "opus"),
            )
        )
        second = profiles_module.add(Profile(alias="rikroot"))
        self.assertEqual((first.id, second.id), (1, 2))

        loaded = profiles_module.load(Config.load())[1]
        self.assertEqual(loaded.alias, "rikroot")
        self.assertEqual(loaded.chats, ((-5595440781, 0), (-100500, 7)))
        self.assertEqual((loaded.slot, loaded.daemon, loaded.multi, loaded.debug), (3, True, True, True))
        self.assertEqual(loaded.args, ("--model", "opus"))
        self.assertEqual(loaded.users, (7,))
        self.assertEqual(loaded.label, "rikroot#1")
        self.assertEqual(loaded.command, "/rikroot")
        raw = store.read_json(store.config_path())["telegram"]["profiles"]["1"]
        self.assertEqual(raw["chats"], [-5595440781, "-100500:7"])
        self.assertEqual(raw["alias"], "rikroot")

    def test_ids_are_never_reused_while_a_higher_one_exists(self) -> None:
        Config().save()
        profiles_module.add(Profile(alias="a"))
        profiles_module.add(Profile(alias="b"))
        profiles_module.remove(1)
        third = profiles_module.add(Profile(alias="c"))
        self.assertEqual(third.id, 3)
        self.assertEqual(list(profiles_module.load(Config.load())), [2, 3])

    def test_save_writes_by_id_and_remove_drops(self) -> None:
        Config().save()
        added = profiles_module.add(Profile(alias="rik", cwd=str(self.home)))
        profiles_module.save(Profile(id=added.id, alias="rik2", daemon=True))
        known = profiles_module.load(Config.load())
        self.assertEqual(known[added.id].alias, "rik2")
        self.assertTrue(known[added.id].daemon)
        profiles_module.remove(added.id)
        self.assertEqual(profiles_module.load(Config.load()), {})


class Matching(TempHome):
    def test_claims_and_open_to(self) -> None:
        anywhere = Profile(id=1, alias="x")
        self.assertFalse(anywhere.claims(-1))
        self.assertTrue(anywhere.open_to(-1))

        pinned = Profile(id=2, alias="rikroot", chats=((-5, 0), (-6, 7)))
        self.assertTrue(pinned.claims(-5))
        self.assertTrue(pinned.claims(-5, 99))  # no topic named: the whole chat
        self.assertTrue(pinned.claims(-6, 7))
        self.assertFalse(pinned.claims(-6, 8))
        self.assertFalse(pinned.open_to(-7))

    def test_users_narrow_the_global_list_and_never_widen_it(self) -> None:
        anyone = Profile(id=1, alias="x")
        self.assertTrue(anyone.allows(7))
        self.assertTrue(anyone.allows(7, (7, 8)))
        self.assertFalse(anyone.allows(9, (7, 8)))

        narrow = Profile(id=2, alias="rikroot", users=(8,))
        self.assertTrue(narrow.allows(8, (7, 8)))
        self.assertFalse(narrow.allows(7, (7, 8)))
        self.assertFalse(narrow.allows(8, (7,)))

    def test_aliases_match_without_regard_to_case(self) -> None:
        self.assertTrue(Profile(id=1, alias="Rik").answers_to("rik"))
        self.assertFalse(Profile(id=1, alias="").answers_to(""))

    def test_the_chat_tells_two_profiles_with_one_alias_apart(self) -> None:
        known = {
            1: Profile(id=1, alias="rik", chats=((-5, 0),)),
            2: Profile(id=2, alias="rik", chats=((-6, 0),)),
            3: Profile(id=3, alias="rik"),
            4: Profile(id=4, alias="other"),
        }
        pick = profiles_module.candidates
        self.assertEqual([p.id for p in pick(known, "rik", -5)], [1])
        self.assertEqual([p.id for p in pick(known, "rik", -6)], [2])
        # A chat nobody claims: only the profile open to every chat.
        self.assertEqual([p.id for p in pick(known, "rik", -7)], [3])
        self.assertEqual(pick(known, "nobody", -5), [])
        # Two claiming the same chat is the ambiguity the caller reports.
        known[2] = Profile(id=2, alias="rik", chats=((-5, 0),))
        self.assertEqual([p.id for p in pick(known, "rik", -5)], [1, 2])

    def test_pick_by_id_or_unique_alias(self) -> None:
        known = {1: Profile(id=1, alias="rik"), 2: Profile(id=2, alias="rik"), 3: Profile(id=3, alias="solo")}
        self.assertEqual(profiles_module.pick(known, "3").id, 3)
        self.assertEqual(profiles_module.pick(known, "solo").id, 3)
        self.assertEqual([p.id for p in profiles_module.pick(known, "rik")], [1, 2])
        self.assertEqual(profiles_module.pick(known, "9"), [])
        self.assertEqual(profiles_module.pick(known, "nobody"), [])

    def test_chat_entries_are_read_leniently(self) -> None:
        parse = profiles_module.parse_chat
        self.assertEqual(parse(-100), (-100, 0))
        self.assertEqual(parse("-100:7"), (-100, 7))
        self.assertEqual(parse(" -100 "), (-100, 0))
        self.assertIsNone(parse("nonsense"))
        self.assertIsNone(parse(""))
        self.assertIsNone(parse(True))

    def test_the_directory_is_the_profiles_or_home(self) -> None:
        project = self.home / "proj"
        project.mkdir()
        self.assertEqual(Profile(id=1, alias="x", cwd=str(project)).resolve_cwd(), project)
        self.assertEqual(Profile(id=1, alias="x").resolve_cwd(), Path.home())
        self.assertEqual(Profile(id=1, alias="x", cwd="/nowhere").resolve_cwd(), Path.home())

    def test_daemon_wanted_is_derived_from_the_profiles(self) -> None:
        Config().save()
        self.assertFalse(profiles_module.daemon_wanted(Config.load()))
        profiles_module.add(Profile(alias="rikroot", daemon=True))
        self.assertTrue(profiles_module.daemon_wanted(Config.load()))


class Migration(TempHome):
    def test_schema_four_settings_become_profiles_with_ids(self) -> None:
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
        for gone in ("chat", "sessions", "daemon", "prefix", "workdir"):
            self.assertNotIn(gone, config.telegram)

        known = profiles_module.load(config)
        by_alias = {profile.alias: profile for profile in known.values()}
        self.assertEqual(set(by_alias), {"rikroot", "default"})
        self.assertEqual(by_alias["rikroot"].chats, ((-5595440781, 0),))
        self.assertEqual(by_alias["rikroot"].slot, 2)
        self.assertEqual(by_alias["rikroot"].cwd, "/tmp")
        self.assertFalse(by_alias["rikroot"].daemon)
        # The old default had a chat and the daemon flag: it lives on as an
        # ordinary profile with `default` as its alias.
        self.assertEqual(by_alias["default"].chats, ((-100500, 7),))
        self.assertTrue(by_alias["default"].daemon)

    def test_schema_six_profiles_get_ids_and_an_untouched_default_is_dropped(self) -> None:
        store.write_json_atomic(
            store.config_path(),
            {
                "schema": 6,
                "telegram": {
                    "prefix": "!",
                    "workdir": "/tmp",
                    "idleHours": 6,
                    "profiles": {
                        "default": dict(store.DEFAULT_TELEGRAM_PROFILE, tech=False),
                        "rik": {"chats": [-5], "cwd": "/tmp", "tech": True},
                    },
                },
            },
            harden=False,
        )
        self.assertTrue(store.migrate_config())
        telegram = Config.load().telegram
        self.assertNotIn("prefix", telegram)
        self.assertNotIn("workdir", telegram)
        self.assertEqual(telegram["idleHours"], 1)
        self.assertEqual(list(telegram["profiles"]), ["1"])
        rik = telegram["profiles"]["1"]
        self.assertEqual(rik["alias"], "rik")
        self.assertTrue(rik["debug"])
        self.assertNotIn("tech", rik)
        self.assertEqual(profiles_module.load(Config.load())[1].label, "rik#1")

    def test_an_idle_limit_the_user_chose_survives(self) -> None:
        store.write_json_atomic(
            store.config_path(), {"schema": 6, "telegram": {"idleHours": 12, "profiles": {}}}, harden=False
        )
        store.migrate_config()
        self.assertEqual(Config.load().telegram["idleHours"], 12)

    def test_migration_is_idempotent(self) -> None:
        store.write_json_atomic(
            store.config_path(), {"schema": 4, "telegram": {"chat": -1}}, harden=False
        )
        store.migrate_config()
        first = Config.load().telegram
        self.assertFalse(store.migrate_config())
        self.assertEqual(Config.load().telegram, first)


class ShapeOfTheChat(TempHome):
    def test_new_flags_default_to_the_quiet_shape(self) -> None:
        profile = Profile.from_dict(1, {"alias": "x"})
        self.assertFalse(profile.debug)     # answers only
        self.assertFalse(profile.expanded)  # one message per turn
        self.assertEqual(profile.mode, "")  # whatever claude would start in

    def test_the_mode_is_worked_out_without_starting_claude(self) -> None:
        config = Config(default_args=[])
        self.assertEqual(Profile(id=1, alias="x").resolve_mode(config), "")
        self.assertEqual(Profile(id=1, alias="x", mode="plan").resolve_mode(config), "plan")

        # The skip flag ccas passes *is* bypass, whoever put it there.
        self.assertEqual(
            Profile(id=1, alias="x", args=("--dangerously-skip-permissions",)).resolve_mode(config),
            "bypassPermissions",
        )
        self.assertEqual(
            Profile(id=1, alias="x").resolve_mode(Config(default_args=["--dangerously-skip-permissions"])),
            "bypassPermissions",
        )
        self.assertEqual(
            Profile(id=1, alias="x").resolve_mode(config, cli_args=["--dangerously-skip-permissions"]),
            "bypassPermissions",
        )

        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text('{"permissions": {"defaultMode": "acceptEdits"}}', encoding="utf-8")
        self.assertEqual(Profile(id=1, alias="x").resolve_mode(config), "acceptEdits")
        # A profile with its own mode does not care what the settings say.
        self.assertEqual(Profile(id=1, alias="x", mode="plan").resolve_mode(config), "plan")

    def test_bypass_is_available_only_when_enabled_at_launch(self) -> None:
        plain = Config(default_args=[])
        self.assertFalse(Profile(id=1, alias="x").bypass_available(plain))
        self.assertTrue(Profile(id=1, alias="x").bypass_available(Config()))  # defaultArgs carry the flag
        self.assertTrue(Profile(id=1, alias="x", mode="bypassPermissions").bypass_available(plain))
        self.assertTrue(Profile(id=1, alias="x", args=("--allow-dangerously-skip-permissions",)).bypass_available(plain))
        # A plan profile started with the flag may still come back to bypass.
        self.assertTrue(Profile(id=1, alias="x", mode="plan").bypass_available(Config()))

    def test_launch_args_merge_profile_launch_and_defaults(self) -> None:
        config = Config(default_args=["--dangerously-skip-permissions"])
        profile = Profile(id=1, alias="x", args=("--model", "opus"))
        self.assertEqual(
            profile.launch_args(config, ["-n", "x"]),
            ["--dangerously-skip-permissions", "--model", "opus", "-n", "x"],
        )
