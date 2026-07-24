from __future__ import annotations

import os

from tests.base import TempHome


class TestPosixPathEntry(TempHome):
    def setUp(self) -> None:
        super().setUp()
        if os.name == "nt":
            self.skipTest("posix rc files are not used on Windows")
        from system import pathenv_posix

        self.pathenv = pathenv_posix
        self.shim = self.home / ".claude-switcher" / "bin"

    def test_existing_rc_file_is_extended(self) -> None:
        rc = self.home / ".bashrc"
        rc.write_text("export EDITOR=vim\n", encoding="utf-8")

        changed = self.pathenv.ensure_first(self.shim)

        self.assertIn("bash", changed)
        content = rc.read_text(encoding="utf-8")
        self.assertIn("export EDITOR=vim", content)
        self.assertIn(str(self.shim), content)
        self.assertTrue(self.pathenv.is_on_path(self.shim))

    def test_bare_environment_falls_back_to_profile(self) -> None:
        os.environ.pop("SHELL", None)

        changed = self.pathenv.ensure_first(self.shim)

        self.assertEqual(changed, ["profile"])
        profile = self.home / ".profile"
        self.assertTrue(profile.is_file())
        self.assertIn(str(self.shim), profile.read_text(encoding="utf-8"))
        self.assertTrue(self.pathenv.is_on_path(self.shim))

    def test_second_run_is_idempotent(self) -> None:
        rc = self.home / ".zshrc"
        rc.write_text("alias ll='ls -la'\n", encoding="utf-8")

        self.pathenv.ensure_first(self.shim)
        first = rc.read_text(encoding="utf-8")
        self.pathenv.ensure_first(self.shim)
        second = rc.read_text(encoding="utf-8")

        self.assertEqual(first, second)
        self.assertEqual(second.count(self.pathenv.BEGIN_MARKER), 1)

    def test_remove_restores_the_original_file(self) -> None:
        rc = self.home / ".bashrc"
        original = "export EDITOR=vim\n"
        rc.write_text(original, encoding="utf-8")

        self.pathenv.ensure_first(self.shim)
        self.pathenv.remove(self.shim)

        self.assertEqual(rc.read_text(encoding="utf-8"), original)
        self.assertFalse(self.pathenv.is_on_path(self.shim))

    def test_remove_cleans_the_profile_fallback(self) -> None:
        os.environ.pop("SHELL", None)
        self.pathenv.ensure_first(self.shim)

        self.assertIn("profile", self.pathenv.remove(self.shim))
        self.assertFalse(self.pathenv.is_on_path(self.shim))

    def test_fish_uses_its_own_syntax(self) -> None:
        config = self.home / ".config" / "fish" / "config.fish"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("", encoding="utf-8")

        self.pathenv.ensure_first(self.shim)

        content = config.read_text(encoding="utf-8")
        self.assertIn("fish_add_path", content)
        self.assertNotIn("export PATH", content)
