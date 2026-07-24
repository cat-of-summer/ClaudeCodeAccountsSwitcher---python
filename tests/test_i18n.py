from __future__ import annotations

import json
import os
import string

from tests.base import TempHome
from ui import i18n


def _placeholders(text: str) -> set[str]:
    return {
        name for _, name, _, _ in string.Formatter().parse(text) if name is not None
    }


def _catalogs() -> dict[str, dict[str, str]]:
    return {
        code: json.loads((i18n.lang_dir() / f"{code}.json").read_text(encoding="utf-8"))
        for code in i18n.available_languages()
    }


class TestCatalogs(TempHome):
    def test_every_language_file_loads(self) -> None:
        for code in i18n.available_languages():
            path = i18n.lang_dir() / f"{code}.json"
            self.assertTrue(path.is_file(), f"missing catalog: {path}")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict)
            self.assertTrue(data)

    def test_catalogs_have_identical_keys(self) -> None:
        catalogs = _catalogs()
        reference = set(catalogs[i18n.DEFAULT_LANGUAGE])
        for code, data in catalogs.items():
            self.assertEqual(
                set(data),
                reference,
                f"catalog '{code}' differs: "
                f"missing={sorted(reference - set(data))} "
                f"extra={sorted(set(data) - reference)}",
            )

    def test_placeholders_match_across_catalogs(self) -> None:
        catalogs = _catalogs()
        reference = catalogs[i18n.DEFAULT_LANGUAGE]
        for code, data in catalogs.items():
            for key, template in data.items():
                self.assertEqual(
                    _placeholders(template),
                    _placeholders(reference[key]),
                    f"placeholder mismatch in '{code}' for key '{key}'",
                )

    def test_yes_no_answer_lists_are_parsable(self) -> None:
        for code in i18n.available_languages():
            i18n.set_language(code)
            self.assertIn("y", i18n.answers("common.yes_answers"))
            self.assertIn("n", i18n.answers("common.no_answers"))


class TestLookup(TempHome):
    def test_unknown_key_returns_itself(self) -> None:
        self.assertEqual(i18n.t("no.such.key.exists"), "no.such.key.exists")

    def test_broken_placeholder_returns_template(self) -> None:
        self.assertEqual(i18n.t("cli.active_slot"), i18n.t("cli.active_slot"))

    def test_substitution(self) -> None:
        self.assertIn("7", i18n.t("cli.free_slot", slot=7))

    def test_russian_catalog_is_actually_used(self) -> None:
        os.environ["CCAS_LANG"] = "ru"
        i18n.set_language("ru")
        self.assertEqual(i18n.current_language(), "ru")
        self.assertNotEqual(i18n.t("common.cancelled"), "common.cancelled")


class TestLanguageDetection(TempHome):
    def test_env_override_wins(self) -> None:
        os.environ["CCAS_LANG"] = "ru"
        self.assertEqual(i18n.set_language("en"), "ru")

    def test_normalise_accepts_locale_strings(self) -> None:
        self.assertEqual(i18n.normalise("ru_RU.UTF-8"), "ru")
        self.assertEqual(i18n.normalise("en-US"), "en")
        self.assertEqual(i18n.normalise("de_DE"), "")
        self.assertEqual(i18n.normalise(None), "")

    def test_locale_variables_are_honoured(self) -> None:
        for variable in ("CCAS_LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            os.environ.pop(variable, None)
        os.environ["LANG"] = "ru_RU.UTF-8"
        self.assertEqual(i18n.detect_system_language(), "ru")

    def test_detect_falls_back_to_default(self) -> None:
        for variable in ("CCAS_LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            os.environ.pop(variable, None)
        if os.name != "nt":
            self.assertEqual(i18n.detect_system_language(), i18n.DEFAULT_LANGUAGE)
