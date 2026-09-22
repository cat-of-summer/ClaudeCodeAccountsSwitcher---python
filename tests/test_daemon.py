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

    def profile(self, alias: str, **fields: Any) -> profiles_module.Profile:
        return profiles_module.add(profiles_module.Profile(alias=alias, **fields))

    def daemon(self) -> daemon_module.Daemon:
        daemon = daemon_module.Daemon(Config.load())
        daemon.bot = self.bot  # type: ignore[assignment]
        return daemon

    def _route(self, daemon: daemon_module.Daemon, profile: profiles_module.Profile, *, pid: int = 1000, chat: int = 0) -> daemon_module.Route:
        route = daemon.register(
            {"pid": pid, "profile": profile.id, "alias": profile.alias, "chat": chat, "cwd": str(self.home), "slot": 1}
        )
        assert route is not None
        return route


class NothingIsListenedToByDefault(DaemonBase):
    def test_a_plain_line_with_no_profile_is_ignored(self) -> None:
        daemon = self.daemon()
        daemon.deliver(incoming("hello there"))
        daemon.deliver(incoming("/rik hello there"))
        daemon.deliver(incoming("/claude"))  # no profile to start: nothing to say either
        self.assertEqual(self.launched, [])
        self.assertEqual(self.bot.sent, [])

    def test_an_explicit_claude_is_honoured_even_with_the_daemon_flag_off(self) -> None:
        rik = self.profile("rik", cwd=str(self.home))
        daemon = self.daemon()
        daemon.deliver(incoming("/claude fix the footer"))
        self.assertEqual(len(self.launched), 1)
        command, cwd, env = self.launched[0]
        self.assertTrue(command[0].endswith(store.shim_name()))
        self.assertEqual(command[1:], ["-t", "telegram", "-P", str(rik.id), "fix", "the", "footer"])
        self.assertEqual(cwd, self.home.resolve())
        self.assertEqual(env["CCAS_TELEGRAM_PROFILE"], str(rik.id))
        self.assertEqual(env["CCAS_TELEGRAM_FEED"], "1")

    def test_a_profile_is_reached_by_its_alias_only(self) -> None:
        self.profile("rikroot", cwd=str(self.home))
        daemon = self.daemon()
        daemon.deliver(incoming("/rikroot are you there"))
        self.assertIn("/rikroot", self.bot.texts()[-1])  # not running, says how to start
        self.assertEqual(self.launched, [])

        daemon.deliver(incoming("plain line"))
        daemon.deliver(incoming("rikroot without the slash"))
        self.assertEqual(len(self.bot.sent), 1)  # nobody answers unaddressed talk


class ProfileOwnsItsChat(DaemonBase):
    def setUp(self) -> None:
        super().setUp()
        self.project = self.home / "RIKROOT"
        self.project.mkdir()

    def test_a_profile_hears_only_its_alias(self) -> None:
        rik = self.profile("rikroot", chats=((PROJECT_CHAT, 0),), cwd=str(self.project))
        daemon = self.daemon()
        route = self._route(daemon, rik)

        daemon.deliver(incoming("/rikroot build it", chat=PROJECT_CHAT))
        self.assertEqual([entry["text"] for entry in route.take(0)], ["/rikroot build it"])

        # Plain talk in its own chat is not for it -- that is how a person
        # stops the conversation -- and neither is the other chat.
        daemon.deliver(incoming("and then we had lunch", chat=PROJECT_CHAT))
        daemon.deliver(incoming("/rikroot build it", chat=HOME_CHAT))
        self.assertEqual(route.take(0), [])
        self.assertEqual(self.bot.sent, [])

    def test_two_profiles_with_one_alias_are_told_apart_by_the_chat(self) -> None:
        home = self.profile("rik", chats=((HOME_CHAT, 0),))
        project = self.profile("rik", chats=((PROJECT_CHAT, 0),), cwd=str(self.project))
        daemon = self.daemon()
        home_route = self._route(daemon, home, pid=1)
        project_route = self._route(daemon, project, pid=2)

        daemon.deliver(incoming("/rik for home", chat=HOME_CHAT))
        daemon.deliver(incoming("/rik for the project", chat=PROJECT_CHAT))
        self.assertEqual([entry["text"] for entry in home_route.take(0)], ["/rik for home"])
        self.assertEqual([entry["text"] for entry in project_route.take(0)], ["/rik for the project"])

        # A chat neither of them named is nobody's.
        daemon.deliver(incoming("/rik elsewhere", chat=-777))
        self.assertEqual(home_route.take(0), [])
        self.assertEqual(project_route.take(0), [])
        self.assertEqual(self.bot.sent, [])

    def test_two_profiles_claiming_one_chat_are_named_back(self) -> None:
        self.profile("rik", chats=((PROJECT_CHAT, 0),))
        self.profile("rik", chats=((PROJECT_CHAT, 0),))
        daemon = self.daemon()
        daemon.deliver(incoming("/rik hi", chat=PROJECT_CHAT))
        self.assertEqual(self.launched, [])
        self.assertIn("rik#1", self.bot.texts()[-1])
        self.assertIn("rik#2", self.bot.texts()[-1])

    def test_the_daemon_flag_raises_the_profile_and_keeps_the_message(self) -> None:
        rik = self.profile("rikroot", chats=((PROJECT_CHAT, 0),), cwd=str(self.project), daemon=True, slot=3, args=("--model", "opus"))
        daemon = self.daemon()

        daemon.deliver(incoming("/rikroot wake up and build", chat=PROJECT_CHAT))
        self.assertEqual(len(self.launched), 1)
        command, cwd, env = self.launched[0]
        self.assertEqual(cwd, self.project.resolve())
        self.assertEqual(command[1], "3")
        # The profile's own arguments are merged by the transport, not here.
        self.assertEqual(command[2:], ["-t", "telegram", "-P", str(rik.id)])
        self.assertEqual(env["CCAS_TELEGRAM_PROFILE"], str(rik.id))
        self.assertEqual(self.bot.sent, [])  # raising itself is not worth a message

        # What was said while the window came up is delivered on registration.
        route = self._route(daemon, rik)
        self.assertEqual([entry["text"] for entry in route.take(0)], ["/rikroot wake up and build"])

    def test_a_profile_may_not_be_started_from_a_chat_it_does_not_serve(self) -> None:
        self.profile("home", chats=((HOME_CHAT, 0),))
        rik = self.profile("rikroot", chats=((PROJECT_CHAT, 0),), cwd=str(self.project))
        daemon = self.daemon()
        daemon.deliver(incoming(f"/claude -P {rik.id}", chat=HOME_CHAT))
        self.assertEqual(self.launched, [])
        self.assertIn(str(PROJECT_CHAT), self.bot.texts()[-1])

    def test_a_chat_no_profile_works_in_hears_nothing(self) -> None:
        self.profile("rikroot", chats=((PROJECT_CHAT, 0),))
        daemon = self.daemon()
        daemon.deliver(incoming("/claude", chat=-777))
        daemon.deliver(incoming("/projects", chat=-777))
        self.assertEqual(self.launched, [])
        self.assertEqual(self.bot.sent, [])

    def test_an_unknown_name_is_named_back(self) -> None:
        self.profile("rik")
        daemon = self.daemon()
        daemon.deliver(incoming("/claude -n nope"))
        daemon.deliver(incoming("/claude -P 9"))
        self.assertEqual(self.launched, [])
        self.assertIn("nope", self.bot.texts()[-2])
        self.assertIn("9", self.bot.texts()[-1])

    def test_a_bare_claude_needs_one_obvious_profile(self) -> None:
        self.profile("a")
        self.profile("b")
        daemon = self.daemon()
        daemon.deliver(incoming("/claude"))
        self.assertEqual(self.launched, [])
        self.assertIn("/a", self.bot.texts()[-1])
        self.assertIn("/b", self.bot.texts()[-1])

        # The one that claims this chat wins over the ones open to every chat.
        pinned = self.profile("c", chats=((HOME_CHAT, 0),))
        daemon.deliver(incoming("/claude"))
        self.assertEqual(self.launched[-1][0][3:5], ["-P", str(pinned.id)])

    def test_the_alias_may_carry_the_claude_command(self) -> None:
        rik = self.profile("rik", cwd=str(self.home))
        daemon = self.daemon()
        daemon.deliver(incoming("/rik /claude --model opus"))
        self.assertEqual(self.launched[-1][0][1:], ["-t", "telegram", "-P", str(rik.id), "--model", "opus"])

    def test_a_running_profile_is_not_started_twice(self) -> None:
        rik = self.profile("rik")
        daemon = self.daemon()
        self._route(daemon, rik)
        daemon.deliver(incoming("/claude"))
        self.assertEqual(self.launched, [])
        self.assertIn("rik#1", self.bot.texts()[-1])


class WhoMayTalk(DaemonBase):
    def test_the_global_list_and_the_profile_list_both_apply(self) -> None:
        config = Config.load()
        config.telegram = {**config.telegram, "users": [7, 8]}
        config.save()
        rik = self.profile("rikroot", chats=((PROJECT_CHAT, 0),), users=(8,), cwd=str(self.home))
        daemon = self.daemon()
        route = self._route(daemon, rik)

        daemon.deliver(incoming("/rikroot from nine", chat=PROJECT_CHAT, user=9))  # not global
        daemon.deliver(incoming("/rikroot from seven", chat=PROJECT_CHAT, user=7))  # not on the profile
        self.assertEqual(route.take(0), [])
        daemon.deliver(incoming("/rikroot from eight", chat=PROJECT_CHAT, user=8))
        self.assertEqual([entry["text"] for entry in route.take(0)], ["/rikroot from eight"])

    def test_the_daemons_own_commands_follow_the_global_list(self) -> None:
        config = Config.load()
        config.telegram = {**config.telegram, "users": [7]}
        config.save()
        self.profile("rik")
        daemon = self.daemon()
        daemon.deliver(incoming("/pwd", user=9))
        self.assertEqual(self.bot.sent, [])
        daemon.deliver(incoming("/pwd", user=7))
        self.assertEqual(len(self.bot.sent), 1)


class Buttons(DaemonBase):
    def test_presses_go_to_the_transport_that_drew_them(self) -> None:
        rik = self.profile("rik")
        daemon = self.daemon()
        route = self._route(daemon, rik)
        # The button carries the conversation's tag, which extends the
        # transport's: comparing them whole answered "that session is gone"
        # to every press.
        daemon.deliver(press(f"{route.tag}.2:a:1:0:1"))
        self.assertEqual(len(route.take(0)), 1)
        daemon.deliver(press("t99.1:a:1:0:1"))
        self.assertIn("gone", self.bot.answered[-1][1])


class OneShots(DaemonBase):
    def test_a_subcommand_runs_and_its_output_is_posted(self) -> None:
        self.profile("rik")
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
            rik = self.profile("rikroot", daemon=True)
            self.assertTrue(daemon_module.reconcile_autostart(Config.load()))
            register.assert_called_once()

            profiles_module.save(profiles_module.Profile(id=rik.id, alias="rikroot", daemon=False))
            self.assertFalse(daemon_module.reconcile_autostart(Config.load()))
            unregister.assert_called_once()


class DaemonHttp(TempHome):
    def setUp(self) -> None:
        super().setUp()
        config = Config()
        config.telegram = {**config.telegram, "token": TOKEN}
        config.save()
        profiles_module.add(profiles_module.Profile(alias="rik"))
        self.daemon = daemon_module.Daemon(Config.load())
        self.daemon._serve()
        self.addCleanup(self.daemon._shutdown)

    def test_transports_register_poll_and_leave(self) -> None:
        port = self.daemon.port
        tag = daemonlink.register(port, {"pid": os.getpid(), "profile": 1, "alias": "rik", "cwd": "/", "slot": 1})
        self.assertTrue(tag.startswith("t"))
        self.assertEqual(daemonlink.poll(port, os.getpid(), wait=0), [])

        threading.Timer(0.2, lambda: self.daemon.deliver(incoming("/rik! /pwd"))).start()
        updates = daemonlink.poll(port, os.getpid(), wait=5)
        self.assertEqual([u["text"] for u in updates], ["/rik! /pwd"])
        self.assertEqual(daemonlink.status(port)["routes"][0]["profile"], 1)
        self.assertEqual(daemonlink.status(port)["routes"][0]["label"], "rik#1")

        daemonlink.unregister(port, os.getpid())
        with self.assertRaises(daemonlink.LinkError):
            daemonlink.poll(port, os.getpid(), wait=0)

    def test_stop_over_http(self) -> None:
        daemonlink.stop(self.daemon.port)
        self.assertTrue(self.daemon._stop.is_set())
