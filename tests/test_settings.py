from __future__ import annotations

import argparse
import contextlib
import io
import json
import os

from app import cli
from core import settings, store
from core.store import Config
from core.version import SCHEMA_VERSION
from tests.base import TempHome
from ui import i18n


class TestSkipPermissions(TempHome):
    def test_round_trip(self) -> None:
        setting = settings.find("skip-permissions")
        config = Config()
        config.save()

        self.assertTrue(setting.get(Config.load()))
        settings.apply(setting, False)
        self.assertFalse(setting.get(Config.load()))
        settings.apply(setting, True)
        self.assertTrue(setting.get(Config.load()))

    def test_other_default_args_are_preserved(self) -> None:
        """It is a view over default_args, so a hand-added flag must survive."""
        config = Config()
        config.default_args = ["--dangerously-skip-permissions", "--model", "opus"]
        config.save()

        setting = settings.find("skip-permissions")
        settings.apply(setting, False)
        self.assertEqual(Config.load().default_args, ["--model", "opus"])

        settings.apply(setting, True)
        self.assertEqual(
            Config.load().default_args,
            ["--dangerously-skip-permissions", "--model", "opus"],
        )

    def test_the_flag_is_not_duplicated(self) -> None:
        config = Config()
        config.default_args = ["--dangerously-skip-permissions"]
        config.save()
        settings.apply(settings.find("skip-permissions"), True)
        self.assertEqual(
            Config.load().default_args.count("--dangerously-skip-permissions"), 1
        )


class TestParsing(TempHome):
    def test_unknown_key_is_reported(self) -> None:
        with self.assertRaises(settings.SettingError) as caught:
            settings.find("no-such-setting")
        self.assertIn("no-such-setting", str(caught.exception))

    def test_bool_accepts_words_and_switches(self) -> None:
        setting = settings.find("auto-switch")
        for raw in ("on", "true", "1", "yes", "y"):
            self.assertTrue(settings.parse(setting, raw), raw)
        for raw in ("off", "false", "0", "no", "n"):
            self.assertFalse(settings.parse(setting, raw), raw)

    def test_bool_accepts_the_localised_answers(self) -> None:
        # CCAS_LANG wins over set_language(), and the base fixture pins it to en.
        os.environ["CCAS_LANG"] = "ru"
        i18n.set_language("ru")

        setting = settings.find("auto-switch")
        self.assertTrue(settings.parse(setting, "да"))
        self.assertFalse(settings.parse(setting, "нет"))

    def test_nonsense_bool_is_rejected(self) -> None:
        with self.assertRaises(settings.SettingError):
            settings.parse(settings.find("auto-switch"), "maybe")

    def test_threshold_range(self) -> None:
        setting = settings.find("auto-switch-threshold")
        self.assertEqual(settings.parse(setting, "95"), 95)
        for raw in ("0", "500", "-1"):
            with self.assertRaises(settings.SettingError, msg=raw):
                settings.parse(setting, raw)

    def test_non_numeric_int_is_rejected(self) -> None:
        with self.assertRaises(settings.SettingError):
            settings.parse(settings.find("auto-switch-max"), "many")

    def test_choice_is_validated(self) -> None:
        setting = settings.find("auto-switch-strategy")
        self.assertEqual(settings.parse(setting, "notify"), "notify")
        with self.assertRaises(settings.SettingError):
            settings.parse(setting, "whatever")

    def test_claude_path_must_exist(self) -> None:
        with self.assertRaises(settings.SettingError):
            settings.apply(settings.find("claude-path"), "/definitely/not/here")


class TestCatalogCoverage(TempHome):
    def test_every_setting_is_labelled_in_every_language(self) -> None:
        for code in i18n.available_languages():
            i18n.set_language(code)
            for setting in settings.SETTINGS:
                self.assertNotEqual(
                    setting.label, f"config.label_{setting.key}", f"{code}/{setting.key}"
                )
                self.assertNotEqual(
                    setting.help, f"config.help_{setting.key}", f"{code}/{setting.key}"
                )
        i18n.set_language("en")

    def test_strategy_labels_exist(self) -> None:
        setting = settings.find("auto-switch-strategy")
        for choice in setting.choices:
            settings.apply(setting, choice)
            self.assertNotIn("config.strategy_", setting.display(Config.load()))


class TestConfigSchema(TempHome):
    def test_auto_switch_round_trips(self) -> None:
        config = Config()
        config.auto_switch = {**config.auto_switch, "enabled": True, "threshold": 80}
        config.save()

        loaded = Config.load()
        self.assertTrue(loaded.auto_switch["enabled"])
        self.assertEqual(loaded.auto_switch["threshold"], 80)

    def test_a_config_missing_the_block_gets_the_defaults(self) -> None:
        store.write_json_atomic(
            store.config_path(), {"schema": 1, "realClaudePath": "/bin/claude"}
        )
        loaded = Config.load()
        self.assertEqual(loaded.auto_switch, store.DEFAULT_AUTO_SWITCH)
        self.assertFalse(loaded.auto_switch["enabled"])

    def test_a_partial_block_is_merged_not_replaced(self) -> None:
        """An older build wrote fewer keys; reading a missing one must not blow up."""
        store.write_json_atomic(
            store.config_path(), {"schema": 2, "autoSwitch": {"enabled": True}}
        )
        loaded = Config.load()
        self.assertTrue(loaded.auto_switch["enabled"])
        self.assertEqual(
            loaded.auto_switch["threshold"], store.DEFAULT_AUTO_SWITCH["threshold"]
        )

    def test_migration_stamps_the_schema_and_keeps_a_backup(self) -> None:
        store.write_json_atomic(
            store.config_path(), {"schema": 1, "realClaudePath": "/bin/claude"}
        )

        self.assertTrue(store.migrate_config())
        raw = json.loads(store.config_path().read_text(encoding="utf-8"))
        self.assertEqual(raw["schema"], SCHEMA_VERSION)
        self.assertEqual(raw["realClaudePath"], "/bin/claude")
        self.assertTrue(list(store.backups_dir().glob("config.json.*")))

    def test_migration_is_idempotent(self) -> None:
        Config().save()
        self.assertFalse(store.migrate_config())

    def test_migration_without_an_installation_does_nothing(self) -> None:
        self.assertFalse(store.migrate_config())


class TestConfigCommand(TempHome):
    def _run(self, **kwargs: object) -> str:
        args = argparse.Namespace(key=None, value=None, list=True)
        for name, value in kwargs.items():
            setattr(args, name, value)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.cmd_config(args)
        return buffer.getvalue()

    def setUp(self) -> None:
        super().setUp()
        Config().save()

    def test_listing_prints_every_key(self) -> None:
        output = self._run()
        for setting in settings.SETTINGS:
            self.assertIn(setting.key, output)

    def test_reading_one_key(self) -> None:
        output = self._run(key="auto-switch")
        self.assertIn("auto-switch", output)

    def test_writing_one_key(self) -> None:
        self._run(key="auto-switch", value="on")
        self.assertTrue(Config.load().auto_switch["enabled"])

    def test_a_bad_value_exits_with_a_translated_message(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self._run(key="auto-switch-threshold", value="500")
        message = str(caught.exception)
        self.assertNotIn("config.out_of_range", message)
        self.assertIn("auto-switch-threshold", message)

    def test_an_unknown_key_exits(self) -> None:
        with self.assertRaises(SystemExit):
            self._run(key="nope", value="1")

    def test_migration_fills_an_empty_resume_prompt(self) -> None:
        """Schema 3: a session resumed after a switch must be told to carry on."""
        store.write_json_atomic(
            store.config_path(),
            {"schema": 2, "autoSwitch": {"enabled": True, "resumePrompt": ""}},
        )
        self.assertTrue(store.migrate_config())
        prompt = Config.load().auto_switch["resumePrompt"]
        self.assertEqual(prompt, i18n.t("autoswitch.default_resume_prompt"))
        self.assertTrue(prompt.strip())

    def test_migration_keeps_a_prompt_the_user_wrote(self) -> None:
        store.write_json_atomic(
            store.config_path(),
            {"schema": 2, "autoSwitch": {"resumePrompt": "carry on, quietly"}},
        )
        store.migrate_config()
        self.assertEqual(Config.load().auto_switch["resumePrompt"], "carry on, quietly")

    def test_the_wait_limit_is_edited_in_minutes(self) -> None:
        Config().save()
        setting = settings.find("auto-switch-max-wait")
        self.assertEqual(setting.get(Config.load()), 120)
        settings.apply(setting, settings.parse(setting, "45"))
        self.assertEqual(Config.load().auto_switch["maxWaitSeconds"], 45 * 60)
        with self.assertRaises(settings.SettingError):
            settings.parse(setting, "100000")
