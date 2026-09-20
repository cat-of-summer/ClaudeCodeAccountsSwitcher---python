from __future__ import annotations

import time
from typing import Any

from app.transport import profiles as profiles_module
from app.transport.profiles import Profile
from app.transport.transport import Transport
from core import store, telegram
from core.store import Config
from tests.base import TempHome
from tests.test_driver import FAKE_CLAUDE

CHAT = -100500


class FakeBot:
    id = 123456

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_message(
        self, chat_id: int, text: str, *, thread_id: int = 0, reply_markup: Any = None, reply_to: int = 0, **_: Any
    ) -> int:
        self.sent.append({"chat": chat_id, "text": text, "reply_to": reply_to})
        return len(self.sent)

    def edit_markup(self, *args: Any, **kwargs: Any) -> None:
        return

    def answer_callback(self, *args: Any, **kwargs: Any) -> None:
        return

    def typing(self, *args: Any, **kwargs: Any) -> None:
        return

    def texts(self) -> list[str]:
        return [entry["text"] for entry in self.sent]


def incoming(text: str, *, user: int, chat: int = CHAT, message_id: int = 1) -> telegram.Incoming:
    return telegram.Incoming(
        update_id=message_id, chat_id=chat, thread_id=0, user_id=user, text=text, message_id=message_id
    )


class TransportBase(TempHome):
    def setUp(self) -> None:
        super().setUp()
        source = self.home / "fake_claude.py"
        source.write_text(FAKE_CLAUDE, encoding="utf-8")
        source.chmod(0o755)
        config = Config(real_claude_path=str(source), default_args=[])
        config.telegram = {**config.telegram, "token": "123456:ABCDEFghijklmnopqrstuvwxyz0123456789"}
        config.save()
        self.config = Config.load()
        self.bot = FakeBot()
        self.write_credentials(store.creds_file(1))
        self._transports: list[Transport] = []

    def tearDown(self) -> None:
        # Before TempHome takes the directory away: the conversations are
        # live processes writing into it.
        for transport in self._transports:
            transport._detach()
        super().tearDown()

    def transport(self, profile: Profile, **fields: Any) -> Transport:
        transport = Transport(
            self.config,
            profile,
            bot=self.bot,  # type: ignore[arg-type]
            slot=1,
            cwd=self.home,
            args=[],
            **fields,
        )
        self._transports.append(transport)
        return transport

    def wait_for(self, predicate: Any, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError("timed out waiting for the transport")


class ConversationsPerPerson(TransportBase):
    def test_each_person_in_a_chat_gets_their_own_claude(self) -> None:
        transport = self.transport(Profile(name="default"))
        transport._on_incoming(incoming("hello", user=7))
        transport._on_incoming(incoming("hello", user=8))

        self.assertEqual(len(transport.conversations), 2)
        self.assertEqual(sorted(transport.conversations), [(CHAT, 0, 7), (CHAT, 0, 8)])
        self.wait_for(
            lambda: all(
                c.driver is not None and c.driver.session_id
                for c in transport.conversations.values()
            )
        )
        sessions = {c.session_id for c in transport.conversations.values()}
        self.assertEqual(len(sessions), 2)

        # The same person keeps talking to the same claude.
        transport._on_incoming(incoming("again", user=7))
        self.assertEqual(len(transport.conversations), 2)

    def test_multi_puts_the_whole_chat_in_one_session(self) -> None:
        transport = self.transport(Profile(name="team", multi=True))
        transport._on_incoming(incoming("hello", user=7))
        transport._on_incoming(incoming("hello", user=8))
        self.assertEqual(list(transport.conversations), [(CHAT, 0, 0)])

    def test_a_topic_is_a_conversation_of_its_own(self) -> None:
        transport = self.transport(Profile(name="default"))
        transport._on_incoming(incoming("hello", user=7))
        threaded = telegram.Incoming(
            update_id=2, chat_id=CHAT, thread_id=4, user_id=7, text="hello", message_id=2
        )
        transport._on_incoming(threaded)
        self.assertEqual(sorted(transport.conversations), [(CHAT, 0, 7), (CHAT, 4, 7)])

    def test_the_limit_refuses_once_and_then_stays_quiet(self) -> None:
        config = Config.load()
        config.telegram = {**config.telegram, "maxSessions": 1}
        config.save()
        self.config = Config.load()

        transport = self.transport(Profile(name="default"))
        transport._on_incoming(incoming("hello", user=7))
        transport._on_incoming(incoming("hello", user=8))
        transport._on_incoming(incoming("hello again", user=8))

        self.assertEqual(len(transport.conversations), 1)
        refusals = [text for text in self.bot.texts() if "1" in text]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(self.bot.sent[-1]["reply_to"], 1)


class WhatTheTransportServes(TransportBase):
    def test_it_keeps_to_the_chats_its_profile_named(self) -> None:
        transport = self.transport(Profile(name="rikroot", chats=((CHAT, 0),)))
        self.assertTrue(transport.serves(CHAT, 0))
        self.assertFalse(transport.serves(-777, 0))
        transport._on_incoming(incoming("hello", user=7, chat=-777))
        self.assertEqual(transport.conversations, {})

    def test_a_transport_pinned_to_one_chat_ignores_the_others(self) -> None:
        transport = self.transport(Profile(name="default"), chat=CHAT)
        self.assertFalse(transport.serves(-777, 0))
        transport._on_incoming(incoming("hello", user=7, chat=-777))
        self.assertEqual(transport.conversations, {})

    def test_the_users_it_is_allowed_to_hear(self) -> None:
        config = Config.load()
        config.telegram = {**config.telegram, "users": [7]}
        config.save()
        self.config = Config.load()
        transport = self.transport(Profile(name="default"))
        transport._on_incoming(incoming("hello", user=8))
        self.assertEqual(transport.conversations, {})
        transport._on_incoming(incoming("hello", user=7))
        self.assertEqual(len(transport.conversations), 1)

    def test_the_alias_and_prefix_are_stripped_before_claude_sees_the_line(self) -> None:
        config = Config.load()
        config.telegram = {**config.telegram, "prefix": "cc:"}
        config.save()
        self.config = Config.load()
        transport = self.transport(Profile(name="rikroot", chats=((CHAT, 0),)))

        self.assertEqual(transport._strip("cc: rikroot build it"), "build it")
        self.assertEqual(transport._strip("rikroot build it"), "build it")
        self.assertEqual(transport._strip("cc: build it"), "build it")
        self.assertIsNone(transport._strip("build it"))


class ProfileSavedFromTheChat(TransportBase):
    def test_save_pins_the_chat_to_the_profile(self) -> None:
        transport = self.transport(Profile(name="rikroot"))
        transport._on_incoming(incoming("hello", user=7))
        conversation = next(iter(transport.conversations.values()))
        conversation._save_profile()

        saved = profiles_module.load(Config.load())["rikroot"]
        self.assertEqual(saved.chats, ((CHAT, 0),))
        self.assertEqual(saved.cwd, str(self.home))
