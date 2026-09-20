from __future__ import annotations

from unittest import mock

from app import installer
from app.transport import profiles as profiles_module
from app.transport.profiles import Profile
from core import detect
from core.store import Config
from tests.base import TempHome

TOKEN = "123456:ABCDEFghijklmnopqrstuvwxyz0123456789"


class ReinstallKeepsWhatWasConfigured(TempHome):
    """A reinstall is about the binary and the interception.

    It used to rebuild `config.json` from scratch, which quietly took the bot
    token, every profile and every hook handler with it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.claude = self.home / "claude.exe"
        self.claude.write_text("", encoding="utf-8")
        # Neither probing the real binary nor touching PATH belongs in this
        # test; both are covered where they are implemented.
        patcher = mock.patch.object(
            detect, "probe_cred_mode", return_value=(detect.CRED_MODE_ENV, {"supported": True})
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("install_shims", "ensure_path_entry"):
            patcher = mock.patch.object(installer, name, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _install(self) -> Config:
        installer.install(claude_path=str(self.claude), reporter=lambda *_: None)
        return Config.load()

    def test_the_token_profiles_and_hooks_are_carried_over(self) -> None:
        self._install()

        config = Config.load()
        config.telegram = {**config.telegram, "token": TOKEN, "prefix": "cc:"}
        config.hooks = [{"event": "Stop", "command": "say done"}]
        config.hooks_bus = False
        config.save()
        profiles_module.save(
            Profile(name="rik", chats=((-5595440781, 0),), cwd=str(self.home), tech=True)
        )

        after = self._install()
        self.assertEqual(after.telegram["token"], TOKEN)
        self.assertEqual(after.telegram["prefix"], "cc:")
        self.assertEqual(after.hooks, [{"event": "Stop", "command": "say done"}])
        self.assertFalse(after.hooks_bus)

        rik = profiles_module.load(after)["rik"]
        self.assertEqual(rik.chats, ((-5595440781, 0),))
        self.assertEqual(rik.cwd, str(self.home))
        self.assertTrue(rik.tech)

    def test_a_first_install_starts_from_the_defaults(self) -> None:
        config = self._install()
        self.assertEqual(config.telegram["token"], "")
        self.assertEqual(list(profiles_module.load(config)), ["default"])
