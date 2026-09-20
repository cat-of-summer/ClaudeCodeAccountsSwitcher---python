from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any
from unittest import mock

from app import daemon as daemon_module
from app.transport import daemonlink
from core import store, telegram
from core.store import Config
from tests.base import TempHome

TOKEN = "123456:ABCDEFghijklmnopqrstuvwxyz0123456789"
CHAT = -100500


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


def incoming(text: str, *, chat: int = CHAT, user: int = 7, thread: int = 0, update_id: int = 1) -> telegram.Incoming:
    return telegram.Incoming(update_id=update_id, chat_id=chat, thread_id=thread, user_id=user, text=text, message_id=9)


def press(data: str, *, chat: int = CHAT) -> telegram.Incoming:
    return telegram.Incoming(update_id=2, chat_id=chat, thread_id=0, user_id=7, text="", message_id=9, callback_id="cb", callback_data=data)


class DaemonRouting(TempHome):
    def setUp(self) -> None:
        super().setUp()
        config = Config()
        config.telegram = {**config.telegram, "token": TOKEN, "chat": CHAT, "prefix": ""}
        config.save()
        self.config = Config.load()
        self.daemon = daemon_module.Daemon(self.config)
        self.bot = FakeBot()
        self.daemon.bot = self.bot  # type: ignore[assignment]
        self.launched: list[tuple[list[str], Path, dict[str, str]]] = []
        patcher = mock.patch.object(
            daemon_module, "open_window",
            side_effect=lambda command, *, cwd, env, console: self.launched.append((command, cwd, env)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _route(self, alias: str, pid: int = 1000, chat: int = CHAT) -> daemon_module.Route:
        route = self.daemon.register({"pid": pid, "chat": chat, "alias": alias, "cwd": str(self.home), "slot": 1})
        assert route is not None
        return route

    def test_lines_go_to_the_alias_or_the_nameless_session(self) -> None:
        main = self._route("main", pid=1)
        nameless = self._route("", pid=2)
        self.daemon.deliver(incoming("main hello"))
        self.daemon.deliver(incoming("just chatting"))
        self.daemon.deliver(incoming("elsewhere", chat=-1))
        self.assertEqual([entry["text"] for entry in main.take(0)], ["main hello"])
        self.assertEqual([entry["text"] for entry in nameless.take(0)], ["just chatting"])

    def test_buttons_are_routed_by_tag(self) -> None:
        main = self._route("main", pid=1)
        self.daemon.deliver(press(f"{main.tag}:a:1:0:1"))
        self.daemon.deliver(press("t99:a:1:0:1"))
        self.assertEqual(len(main.take(0)), 1)
        self.assertEqual(self.bot.answered[-1][1], "that button belongs to a session that is gone")

    def test_users_filter_applies_to_the_configured_chat(self) -> None:
        self.daemon.users = [7]
        nameless = self._route("", pid=2)
        self.daemon.deliver(incoming("hi", user=8))
        self.daemon.deliver(incoming("hi", user=7))
        self.assertEqual(len(nameless.take(0)), 1)

    def test_claude_opens_a_window_with_the_exact_arguments(self) -> None:
        self.daemon.deliver(incoming("/claude 2 -n work --model opus Привет"))
        self.assertEqual(len(self.launched), 1)
        command, cwd, env = self.launched[0]
        self.assertTrue(command[0].endswith(store.shim_name()))
        self.assertEqual(command[1], "2")
        self.assertEqual(command[2:6], ["-t", "telegram", "--tg-chat", str(CHAT)])
        self.assertEqual(command[6:], ["-n", "work", "--model", "opus", "Привет"])
        self.assertEqual(cwd, Path.home())
        self.assertEqual(env["CCAS_TELEGRAM_FEED"], "1")
        self.assertTrue(env["CCAS_TELEGRAM_TAG"].startswith("t"))
        self.assertIn("work", self.bot.texts()[-1])

    def test_a_second_nameless_session_is_refused(self) -> None:
        self._route("", pid=2)
        self.daemon.deliver(incoming("/claude hi"))
        self.assertEqual(self.launched, [])
        self.assertIn("-n", self.bot.texts()[-1])
        self._route("work", pid=3)
        self.daemon.deliver(incoming("/claude -n work"))
        self.assertEqual(self.launched, [])

    def test_profile_supplies_directory_slot_and_args(self) -> None:
        project = self.home / "proj"
        project.mkdir()
        config = Config.load()
        config.telegram = {**config.telegram, "sessions": {"site": {"chat": CHAT, "cwd": str(project), "slot": 3, "args": ["--effort", "low"]}}}
        config.save()
        self.daemon.deliver(incoming("/claude -n site fix the footer"))
        command, cwd, _ = self.launched[-1]
        self.assertEqual(cwd, project.resolve())
        self.assertEqual(command[1], "3")
        self.assertEqual(command[6:], ["--effort", "low", "-n", "site", "fix", "the", "footer"])

        # Another chat the daemon serves (a session lives there) may not
        # borrow a profile bound to this one.
        self._route("other", pid=5, chat=-77)
        self.daemon.deliver(incoming("/claude -n site", chat=-77))
        self.assertIn(str(CHAT), self.bot.texts()[-1])
        self.assertEqual(len(self.launched), 1)

    def test_cd_and_projects_choose_where_sessions_start(self) -> None:
        project = self.home / "alpha"
        project.mkdir()
        self.daemon.deliver(incoming("/cd nowhere"))
        self.assertIn("nowhere", self.bot.texts()[-1])
        self.daemon.deliver(incoming(f"/cd {project}"))
        self.daemon.deliver(incoming("/pwd"))
        self.assertIn(str(project.resolve()), self.bot.texts()[-1])
        self.daemon.deliver(incoming("/claude"))
        self.assertEqual(self.launched[-1][1], project.resolve())

        other = self.home / "beta"
        other.mkdir()
        self.write_root_config("me@example.com")
        raw = store.read_json(self.home / ".claude.json")
        raw["projects"] = {str(other): {}, str(self.home / "gone"): {}}
        store.write_json_atomic(self.home / ".claude.json", raw, harden=False)
        self.daemon.deliver(incoming("/projects"))
        listing = self.bot.sent[-1]
        self.assertIn("beta", listing["text"])
        self.assertNotIn("gone", listing["text"])
        go = listing["markup"]["inline_keyboard"][0][1]["callback_data"]
        self.assertTrue(go.startswith("d:go:"))
        self.daemon.deliver(press(go))
        self.assertEqual(self.launched[-1][1], other.resolve())

    def test_one_shot_commands_are_run_and_posted(self) -> None:
        with mock.patch.object(daemon_module.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout=b"server-a\n", stderr=b"")
            self.daemon.deliver(incoming("/claude mcp list"))
        self.assertEqual(self.launched, [])
        self.assertIn("server-a", self.bot.texts()[-1])
        self.assertEqual(run.call_args.args[0][1:], ["mcp", "list"])

    def test_unaddressed_lines_without_a_session_get_a_hint_only_for_commands(self) -> None:
        self.daemon.deliver(incoming("random chatter"))
        self.assertEqual(self.bot.sent, [])
        self.daemon.deliver(incoming("/whatever"))
        self.assertIn("/claude", self.bot.texts()[-1])
        self.daemon.deliver(incoming("/help"))
        self.assertIn("/projects", self.bot.texts()[-1])


class DaemonHttp(TempHome):
    def setUp(self) -> None:
        super().setUp()
        config = Config()
        config.telegram = {**config.telegram, "token": TOKEN, "chat": CHAT}
        config.save()
        self.daemon = daemon_module.Daemon(Config.load())
        self.daemon._serve()
        self.addCleanup(self.daemon._shutdown)

    def test_sessions_register_poll_and_leave(self) -> None:
        port = self.daemon.port
        tag = daemonlink.register(port, {"pid": os.getpid(), "chat": CHAT, "alias": "x", "cwd": "/", "slot": 1})
        self.assertTrue(tag.startswith("t"))
        self.assertEqual(daemonlink.poll(port, os.getpid(), wait=0), [])

        def _push() -> None:
            self.daemon.deliver(incoming("x ping"))

        threading.Timer(0.2, _push).start()
        updates = daemonlink.poll(port, os.getpid(), wait=5)
        self.assertEqual([u["text"] for u in updates], ["x ping"])
        self.assertEqual(daemonlink.status(port)["routes"][0]["alias"], "x")

        daemonlink.unregister(port, os.getpid())
        with self.assertRaises(daemonlink.LinkError):
            daemonlink.poll(port, os.getpid(), wait=0)

    def test_stop_over_http(self) -> None:
        daemonlink.stop(self.daemon.port)
        self.assertTrue(self.daemon._stop.is_set())
