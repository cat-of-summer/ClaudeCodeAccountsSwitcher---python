from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any
from unittest import mock

from app import daemon as daemon_module
from app.transport import daemonlink
from app.transport import profiles as profiles_module
from core import store, telegram
from core.store import Config
from tests.base import TempHome

TOKEN = "123456:ABCDEFghijklmnopqrstuvwxyz0123456789"
HOME_CHAT = -100500
PROJECT_CHAT = -5595440781


class FakeBot:
    id = 123456

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[tuple[int, Any]] = []
        self.answered: list[tuple[str, str]] = []

    def send_message(self, chat_id: int, text: str, *, thread_id: int = 0, reply_markup: Any = None, **_: Any) -> int:
        self.sent.append({"chat": chat_id, "text": text, "thread": thread_id, "markup": reply_markup})
        return len(self.sent)

    def edit_markup(self, chat_id: int, message_id: int, reply_markup: Any) -> None:
        self.edited.append((message_id, reply_markup))

    def edit_text(self, chat_id: int, message_id: int, text: str, **_: Any) -> None:
        self.edited.append((message_id, text))

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answered.append((callback_id, text))

    def texts(self) -> list[str]:
        return [entry["text"] for entry in self.sent]


def incoming(text: str, *, chat: int = HOME_CHAT, user: int = 7, thread: int = 0, update_id: int = 1) -> telegram.Incoming:
    return telegram.Incoming(update_id=update_id, chat_id=chat, thread_id=thread, user_id=user, text=text, message_id=9)


def press(data: str, *, chat: int = HOME_CHAT) -> telegram.Incoming:
    return telegram.Incoming(update_id=2, chat_id=chat, thread_id=0, user_id=7, text="", message_id=9, callback_id="cb", callback_data=data)


class DaemonBase(TempHome):
    def setUp(self) -> None:
        super().setUp()
        config = Config()
        config.telegram = {**config.telegram, "token": TOKEN}
        config.save()
        self.bot = FakeBot()
        self.launched: list[tuple[list[str], Path, dict[str, str]]] = []
        patcher = mock.patch.object(
            daemon_module, "open_window",
            side_effect=lambda command, *, cwd, env, console: self.launched.append((command, cwd, env)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def profile(self, name: str, **fields: Any) -> profiles_module.Profile:
        profile = profiles_module.Profile(name=name, **fields)
        profiles_module.save(profile)
        return profile

    def daemon(self) -> daemon_module.Daemon:
        daemon = daemon_module.Daemon(Config.load())
        daemon.bot = self.bot  # type: ignore[assignment]
        return daemon

    def _route(self, daemon: daemon_module.Daemon, profile: str, *, pid: int = 1000, chat: int = 0) -> daemon_module.Route:
        route = daemon.register(
            {"pid": pid, "profile": profile, "alias": "" if profile == "default" else profile, "chat": chat, "cwd": str(self.home), "slot": 1}
        )
        assert route is not None
        return route


class NothingIsListenedToByDefault(DaemonBase):
    def test_a_plain_line_with_no_profile_running_is_ignored(self) -> None:
        daemon = self.daemon()
        daemon.deliver(incoming("hello there"))
        self.assertEqual(self.launched, [])
        self.assertEqual(self.bot.sent, [])

    def test_an_explicit_claude_is_honoured_even_with_the_daemon_flag_off(self) -> None:
        daemon = self.daemon()
        daemon.deliver(incoming("/claude fix the footer"))
        self.assertEqual(len(self.launched), 1)
        command, cwd, env = self.launched[0]
        self.assertTrue(command[0].endswith(store.shim_name()))
        self.assertEqual(command[1:], ["-t", "telegram", "fix", "the", "footer"])
        self.assertEqual(cwd, Path.home())
        self.assertEqual(env["CCAS_TELEGRAM_PROFILE"], "default")
        self.assertEqual(env["CCAS_TELEGRAM_FEED"], "1")

    def test_a_named_profile_is_reached_by_its_name_only(self) -> None:
        self.profile("rikroot", cwd=str(self.home))
        daemon = self.daemon()
        daemon.deliver(incoming("rikroot are you there"))
        self.assertIn("rikroot", self.bot.texts()[-1])  # not running, says how to start
        self.assertEqual(self.launched, [])

        daemon.deliver(incoming("plain line"))
        self.assertEqual(len(self.bot.sent), 1)  # the default profile stays silent


class ProfileOwnsItsChat(DaemonBase):
    def setUp(self) -> None:
        super().setUp()
        self.project = self.home / "RIKROOT"
        self.project.mkdir()

    def test_the_chat_it_claims_needs_no_alias(self) -> None:
        self.profile("rikroot", chats=((PROJECT_CHAT, 0),), cwd=str(self.project))
        daemon = self.daemon()
        route = self._route(daemon, "rikroot")

        daemon.deliver(incoming("build it", chat=PROJECT_CHAT))
        self.assertEqual([entry["text"] for entry in route.take(0)], ["build it"])

        # The same line in another chat belongs to the default profile, which
        # is not running, so nothing happens.
        daemon.deliver(incoming("build it", chat=HOME_CHAT))
        self.assertEqual(route.take(0), [])
        self.assertEqual(self.bot.sent, [])

    def test_default_and_a_named_profile_each_own_a_chat(self) -> None:
        self.profile("default", chats=((HOME_CHAT, 0),))
        self.profile("rikroot", chats=((PROJECT_CHAT, 0),), cwd=str(self.project))
        daemon = self.daemon()
        home = self._route(daemon, "default", pid=1)
        project = self._route(daemon, "rikroot", pid=2)

        daemon.deliver(incoming("for home", chat=HOME_CHAT))
        daemon.deliver(incoming("for the project", chat=PROJECT_CHAT))
        self.assertEqual([entry["text"] for entry in home.take(0)], ["for home"])
        self.assertEqual([entry["text"] for entry in project.take(0)], ["for the project"])

        # A chat neither of them named is nobody's now that default has one.
        daemon.deliver(incoming("elsewhere", chat=-777))
        self.assertEqual(home.take(0), [])
        self.assertEqual(project.take(0), [])

    def test_the_daemon_flag_raises_the_profile_and_keeps_the_message(self) -> None:
        self.profile("rikroot", chats=((PROJECT_CHAT, 0),), cwd=str(self.project), daemon=True, slot=3)
        daemon = self.daemon()

        daemon.deliver(incoming("wake up and build", chat=PROJECT_CHAT))
        self.assertEqual(len(self.launched), 1)
        command, cwd, env = self.launched[0]
        self.assertEqual(cwd, self.project.resolve())
        self.assertEqual(command[1], "3")
        self.assertEqual(command[2:], ["-t", "telegram", "-n", "rikroot"])
        self.assertEqual(env["CCAS_TELEGRAM_PROFILE"], "rikroot")
        self.assertEqual(self.bot.sent, [])  # raising itself is not worth a message

        # What was said while the window came up is delivered on registration.
        route = self._route(daemon, "rikroot")
        self.assertEqual([entry["text"] for entry in route.take(0)], ["wake up and build"])

    def test_a_profile_may_not_be_started_from_a_chat_it_does_not_serve(self) -> None:
        self.profile("rikroot", chats=((PROJECT_CHAT, 0),), cwd=str(self.project))
        daemon = self.daemon()
        daemon.deliver(incoming("/claude -n rikroot", chat=HOME_CHAT))
        self.assertEqual(self.launched, [])
        self.assertIn(str(PROJECT_CHAT), self.bot.texts()[-1])

    def test_an_unknown_name_is_named_back(self) -> None:
        daemon = self.daemon()
        daemon.deliver(incoming("/claude -n nope"))
        self.assertEqual(self.launched, [])
        self.assertIn("nope", self.bot.texts()[-1])

    def test_a_running_profile_is_not_started_twice(self) -> None:
        daemon = self.daemon()
        self._route(daemon, "default")
        daemon.deliver(incoming("/claude"))
        self.assertEqual(self.launched, [])
        self.assertIn("default", self.bot.texts()[-1])


class WhoMayTalk(DaemonBase):
    def test_the_global_list_and_the_profile_list_both_apply(self) -> None:
        config = Config.load()
        config.telegram = {**config.telegram, "users": [7, 8]}
        config.save()
        self.profile("rikroot", chats=((PROJECT_CHAT, 0),), users=(8,), cwd=str(self.home))
        daemon = self.daemon()
        route = self._route(daemon, "rikroot")

        daemon.deliver(incoming("from nine", chat=PROJECT_CHAT, user=9))  # not global
        daemon.deliver(incoming("from seven", chat=PROJECT_CHAT, user=7))  # not on the profile
        self.assertEqual(route.take(0), [])
        daemon.deliver(incoming("from eight", chat=PROJECT_CHAT, user=8))
        self.assertEqual([entry["text"] for entry in route.take(0)], ["from eight"])


class Buttons(DaemonBase):
    def test_presses_go_to_the_transport_that_drew_them(self) -> None:
        daemon = self.daemon()
        route = self._route(daemon, "default")
        daemon.deliver(press(f"{route.tag}:a:1:0:1"))
        self.assertEqual(len(route.take(0)), 1)
        daemon.deliver(press("t99:a:1:0:1"))
        self.assertIn("gone", self.bot.answered[-1][1])


class OneShots(DaemonBase):
    def test_a_subcommand_runs_and_its_output_is_posted(self) -> None:
        daemon = self.daemon()
        with mock.patch.object(daemon_module.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout=b"server-a\n", stderr=b"")
            daemon.deliver(incoming("/claude mcp list"))
        self.assertEqual(self.launched, [])
        self.assertIn("server-a", self.bot.texts()[-1])
        self.assertEqual(run.call_args.args[0][1:], ["mcp", "list"])


class Autostart(DaemonBase):
    def test_it_follows_the_profiles(self) -> None:
        with mock.patch.object(daemon_module, "ensure_running", return_value=False), \
             mock.patch("system.autostart.register") as register, \
             mock.patch("system.autostart.unregister") as unregister, \
             mock.patch("system.autostart.is_registered", side_effect=[False, True]):
            self.profile("rikroot", daemon=True)
            self.assertTrue(daemon_module.reconcile_autostart(Config.load()))
            register.assert_called_once()

            self.profile("rikroot", daemon=False)
            self.assertFalse(daemon_module.reconcile_autostart(Config.load()))
            unregister.assert_called_once()


class DaemonHttp(TempHome):
    def setUp(self) -> None:
        super().setUp()
        config = Config()
        config.telegram = {**config.telegram, "token": TOKEN}
        config.save()
        self.daemon = daemon_module.Daemon(Config.load())
        self.daemon._serve()
        self.addCleanup(self.daemon._shutdown)

    def test_transports_register_poll_and_leave(self) -> None:
        port = self.daemon.port
        tag = daemonlink.register(port, {"pid": os.getpid(), "profile": "default", "cwd": "/", "slot": 1})
        self.assertTrue(tag.startswith("t"))
        self.assertEqual(daemonlink.poll(port, os.getpid(), wait=0), [])

        threading.Timer(0.2, lambda: self.daemon.deliver(incoming("/pwd"))).start()
        updates = daemonlink.poll(port, os.getpid(), wait=5)
        self.assertEqual([u["text"] for u in updates], ["/pwd"])
        self.assertEqual(daemonlink.status(port)["routes"][0]["profile"], "default")

        daemonlink.unregister(port, os.getpid())
        with self.assertRaises(daemonlink.LinkError):
            daemonlink.poll(port, os.getpid(), wait=0)

    def test_stop_over_http(self) -> None:
        daemonlink.stop(self.daemon.port)
        self.assertTrue(self.daemon._stop.is_set())
