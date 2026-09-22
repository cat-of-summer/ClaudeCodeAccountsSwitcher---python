from __future__ import annotations

import os
import unittest
from unittest import mock

from core.store import Config
from system import autostart
from tests.base import TempHome


@unittest.skipIf(os.name == "nt", "the Windows path is the registry; probed by hand")
class PosixAutostart(TempHome):
    def setUp(self) -> None:
        super().setUp()
        os.environ["XDG_CONFIG_HOME"] = str(self.home / ".config")

    def test_without_systemd_an_xdg_entry_is_written_and_removed(self) -> None:
        with mock.patch.object(autostart, "_has_user_systemd", return_value=False), \
             mock.patch.object(autostart.sys, "platform", "linux"):
            self.assertFalse(autostart.is_registered())
            self.assertTrue(autostart.register())
            entry = autostart._xdg_desktop()
            self.assertTrue(entry.exists())
            self.assertIn("daemon run --hidden", entry.read_text())
            self.assertTrue(autostart.is_registered())
            self.assertTrue(autostart.unregister())
            self.assertFalse(entry.exists())
            self.assertFalse(autostart.unregister())

    def test_with_systemd_a_user_unit_is_written(self) -> None:
        calls: list[list[str]] = []

        def _run(command: list[str], **kwargs: object) -> mock.Mock:
            calls.append(command)
            return mock.Mock(returncode=0)

        with mock.patch.object(autostart, "_has_user_systemd", return_value=True), \
             mock.patch.object(autostart.subprocess, "run", side_effect=_run), \
             mock.patch.object(autostart.shutil, "which", return_value="/bin/systemctl"), \
             mock.patch.object(autostart.sys, "platform", "linux"):
            self.assertTrue(autostart.register())
            unit = autostart._systemd_unit()
            self.assertIn("ExecStart=", unit.read_text())
            self.assertIn(["systemctl", "--user", "enable", "--now", "ccas-daemon.service"], calls)
            self.assertTrue(autostart.unregister())
            self.assertFalse(unit.exists())
            self.assertIn(["systemctl", "--user", "disable", "--now", "ccas-daemon.service"], calls)

    def test_the_profiles_and_the_entry_move_together(self) -> None:
        """There is no switch of its own: a daemon-raised profile is the
        whole reason for the autostart entry."""
        from app import daemon
        from app.transport import profiles as profiles_module
        from app.transport.profiles import Profile

        config = Config()
        # Without a token the daemon cannot run at all, so there would be
        # nothing to register.
        config.telegram = {**config.telegram, "token": "123456:ABCDEFghijklmnopqrstuvwxyz0123456789"}
        config.save()
        with (
            mock.patch.object(daemon, "ensure_running", return_value=False),
            mock.patch.object(autostart, "register", return_value=True) as register,
            mock.patch.object(autostart, "unregister", return_value=True) as unregister,
            mock.patch.object(autostart, "is_registered", side_effect=[False, True]),
        ):
            profiles_module.save(Profile(id=1, alias="rikroot", daemon=True))
            self.assertTrue(daemon.reconcile_autostart(Config.load()))
            register.assert_called_once()

            profiles_module.save(Profile(id=1, alias="rikroot"))
            self.assertFalse(daemon.reconcile_autostart(Config.load()))
            unregister.assert_called_once()
