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
        config.telegram = {**config.telegram, "token": TOKEN, "roots": ["/srv"]}
        config.hooks = [{"event": "Stop", "command": "say done"}]
        config.hooks_bus = False
        config.save()
        rik = profiles_module.add(
            Profile(alias="rik", chats=((-5595440781, 0),), cwd=str(self.home), debug=True)
        )

        after = self._install()
        self.assertEqual(after.telegram["token"], TOKEN)
        self.assertEqual(after.telegram["roots"], ["/srv"])
        self.assertEqual(after.hooks, [{"event": "Stop", "command": "say done"}])
        self.assertFalse(after.hooks_bus)

        rik = profiles_module.load(after)[rik.id]
        self.assertEqual(rik.chats, ((-5595440781, 0),))
        self.assertEqual(rik.cwd, str(self.home))
        self.assertTrue(rik.debug)

    def test_a_first_install_starts_from_the_defaults(self) -> None:
        config = self._install()
        self.assertEqual(config.telegram["token"], "")
        self.assertEqual(profiles_module.load(config), {})


class InstallClosesWhatBlocksIt(TempHome):
    """An upgrade that cannot overwrite the shims installs nothing new.

    Windows keeps a running binary locked, so the files stay as they were and
    the old build goes on running -- silently. `install` says what holds them
    and closes it.
    """

    def setUp(self) -> None:
        super().setUp()
        self.claude = self.home / "claude.exe"
        self.claude.write_text("", encoding="utf-8")
        patcher = mock.patch.object(
            detect, "probe_cred_mode", return_value=(detect.CRED_MODE_ENV, {"supported": True})
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("install_shims", "ensure_path_entry"):
            patcher = mock.patch.object(installer, name, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.blocking = [
            installer.procs.Process(4242, self.home / "bin" / "claude.exe"),
            installer.procs.Process(4243, self.home / "bin" / "ccas.exe"),
        ]
        self.killed: list[int] = []
        patcher = mock.patch.object(installer.procs, "using", return_value=self.blocking)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            installer.procs, "kill", side_effect=lambda pid, **_: (self.killed.append(pid), True)[1]
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.said: list[str] = []

    def _install(self, ask=None) -> None:
        installer.install(claude_path=str(self.claude), ask=ask, reporter=self.said.append)

    def test_it_warns_by_pid_and_name_before_closing_anything(self) -> None:
        asked: list[str] = []

        def ask(question: str, default: bool) -> bool:
            asked.append(question)
            return True

        self._install(ask=ask)
        warning = "\n".join(self.said)
        self.assertIn("4242", warning)
        self.assertIn("claude.exe", warning)
        self.assertIn("4243", warning)
        # A first install also asks about the defaults; this question is
        # the one that precedes killing anything.
        self.assertIn(installer.t("install.blocking_confirm"), asked)
        self.assertEqual(self.killed, [4242, 4243])

    def test_saying_no_leaves_them_alone_and_says_what_happens_instead(self) -> None:
        self._install(ask=lambda *_: False)
        self.assertEqual(self.killed, [])
        self.assertIn("*.new", "\n".join(self.said))

    def test_an_unattended_install_closes_them_without_asking(self) -> None:
        self._install()
        self.assertEqual(self.killed, [4242, 4243])

    def test_nothing_running_means_nothing_said(self) -> None:
        with mock.patch.object(installer.procs, "using", return_value=[]):
            self._install(ask=lambda *_: True)
        self.assertEqual(self.killed, [])
        self.assertNotIn("4242", "\n".join(self.said))
