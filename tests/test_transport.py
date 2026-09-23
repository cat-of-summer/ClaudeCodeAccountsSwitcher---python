from __future__ import annotations

import time
from typing import Any

from app.transport import poller as poller_module
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
        config = Config(real_claude_path=str(self.fake_claude(FAKE_CLAUDE)), default_args=[])
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
            args=fields.pop("args", []),
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
        transport = self.transport(Profile(id=1, alias="bot"))
        transport._on_incoming(incoming("/bot hello", user=7))
        transport._on_incoming(incoming("/bot hello", user=8))

        self.assertEqual(len(transport.conversations), 2)
        self.assertEqual(sorted(transport.conversations), [(CHAT, 0, 7), (CHAT, 0, 8)])
        # The conversation takes its id from a claude that actually started,
        # so waiting on the same field the assertion reads turns a failed
        # launch into a timeout here instead of a puzzling count below.
        self.wait_for(lambda: all(c.session_id for c in transport.conversations.values()))
        sessions = {c.session_id for c in transport.conversations.values()}
        self.assertEqual(len(sessions), 2)

        # The same person keeps talking to the same claude.
        transport._on_incoming(incoming("/bot again", user=7))
        self.assertEqual(len(transport.conversations), 2)

    def test_multi_puts_the_whole_chat_in_one_session(self) -> None:
        transport = self.transport(Profile(id=1, alias="team", multi=True))
        transport._on_incoming(incoming("/team hello", user=7))
        transport._on_incoming(incoming("/team hello", user=8))
        self.assertEqual(list(transport.conversations), [(CHAT, 0, 0)])

    def test_an_idle_conversation_is_closed_and_its_memory_given_back(self) -> None:
        transport = self.transport(Profile(id=1, alias="bot"))
        transport.idle_seconds = 0.05
        transport._on_incoming(incoming("/bot hello", user=7))
        conversation = next(iter(transport.conversations.values()))
        self.wait_for(lambda: conversation.session_id != "")

        time.sleep(0.1)
        transport._retire_idle()
        self.wait_for(lambda: not transport.conversations)
        self.assertFalse(conversation.driver.alive())

    def test_a_conversation_waiting_out_a_limit_is_not_idle(self) -> None:
        """The delay is the agent's, not the person's: the session stays."""
        transport = self.transport(Profile(id=1, alias="bot"))
        transport.idle_seconds = 0.05
        transport._on_incoming(incoming("/bot hello", user=7))
        conversation = next(iter(transport.conversations.values()))
        self.wait_for(lambda: conversation.session_id != "")
        conversation._limit_rechecked = time.time()  # no usage requests from a test
        conversation._plan_relaunch(slot=2, not_before=time.time() + 600)

        time.sleep(0.1)
        transport._retire_idle()
        self.assertIn(conversation.key, transport.conversations)

    def test_a_topic_is_a_conversation_of_its_own(self) -> None:
        transport = self.transport(Profile(id=1, alias="bot"))
        transport._on_incoming(incoming("/bot hello", user=7))
        threaded = telegram.Incoming(
            update_id=2, chat_id=CHAT, thread_id=4, user_id=7, text="/bot hello", message_id=2
        )
        transport._on_incoming(threaded)
        self.assertEqual(sorted(transport.conversations), [(CHAT, 0, 7), (CHAT, 4, 7)])

    def test_the_limit_refuses_once_and_then_stays_quiet(self) -> None:
        config = Config.load()
        config.telegram = {**config.telegram, "maxSessions": 1}
        config.save()
        self.config = Config.load()

        transport = self.transport(Profile(id=1, alias="bot"))
        transport._on_incoming(incoming("/bot hello", user=7))
        transport._on_incoming(incoming("/bot hello", user=8))
        transport._on_incoming(incoming("/bot hello again", user=8))

        self.assertEqual(len(transport.conversations), 1)
        refusals = [text for text in self.bot.texts() if "1" in text]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(self.bot.sent[-1]["reply_to"], 1)


class WhatTheTransportServes(TransportBase):
    def test_it_keeps_to_the_chats_its_profile_named(self) -> None:
        transport = self.transport(Profile(id=2, alias="rikroot", chats=((CHAT, 0),)))
        self.assertTrue(transport.serves(CHAT, 0))
        self.assertFalse(transport.serves(-777, 0))
        transport._on_incoming(incoming("/bot hello", user=7, chat=-777))
        self.assertEqual(transport.conversations, {})

    def test_a_transport_pinned_to_one_chat_ignores_the_others(self) -> None:
        transport = self.transport(Profile(id=1, alias="bot"), chat=CHAT)
        self.assertFalse(transport.serves(-777, 0))
        transport._on_incoming(incoming("/bot hello", user=7, chat=-777))
        self.assertEqual(transport.conversations, {})

    def test_the_users_it_is_allowed_to_hear(self) -> None:
        config = Config.load()
        config.telegram = {**config.telegram, "users": [7]}
        config.save()
        self.config = Config.load()
        transport = self.transport(Profile(id=1, alias="bot"))
        transport._on_incoming(incoming("/bot hello", user=8))
        self.assertEqual(transport.conversations, {})
        transport._on_incoming(incoming("/bot hello", user=7))
        self.assertEqual(len(transport.conversations), 1)

    def test_a_profile_only_hears_its_alias(self) -> None:
        transport = self.transport(Profile(id=2, alias="rikroot", chats=((CHAT, 0),)))

        stripped = transport._strip(incoming("/rikroot build it", user=7))
        assert stripped is not None
        self.assertEqual((stripped.text, stripped.urgent), ("build it", False))
        urgent = transport._strip(incoming("/RikRoot! build it", user=7))
        assert urgent is not None
        self.assertEqual((urgent.text, urgent.urgent), ("build it", True))
        self.assertIsNone(transport._strip(incoming("rikroot build it", user=7)))
        self.assertIsNone(transport._strip(incoming("/other build it", user=7)))
        self.assertIsNone(transport._strip(incoming("build it", user=7)))


class ProfileSavedFromTheChat(TransportBase):
    def test_save_pins_the_chat_to_the_profile(self) -> None:
        profiles_module.add(Profile(alias="first"))
        stored = profiles_module.add(Profile(alias="rikroot", debug=True, mode="plan"))
        transport = self.transport(stored)
        transport._on_incoming(incoming("/rikroot hello", user=7))
        conversation = next(iter(transport.conversations.values()))
        conversation._save_profile()

        saved = profiles_module.load(Config.load())[stored.id]
        self.assertEqual(saved.chats, ((CHAT, 0),))
        self.assertEqual(saved.cwd, str(self.home))
        # Everything else the profile had stays as it was.
        self.assertEqual((saved.debug, saved.mode, saved.alias), (True, "plan", "rikroot"))


class NothingIsSaidUnprompted(TransportBase):
    def test_a_profile_coming_up_says_nothing_in_the_chat(self) -> None:
        # The mode used to be greeted about; it rides on every answer now,
        # and a chat that nobody wrote in stays untouched.
        transport = self.transport(Profile(id=2, alias="rikroot", chats=((CHAT, 0),), mode="plan"))
        transport._tick()
        self.assertEqual(self.bot.sent, [])
        self.assertFalse(hasattr(transport, "_greet"))

    def test_an_idle_close_is_announced(self) -> None:
        transport = self.transport(Profile(id=1, alias="bot"))
        transport.idle_seconds = 0.05
        transport._on_incoming(incoming("/bot hello", user=7))
        conversation = next(iter(transport.conversations.values()))
        self.wait_for(lambda: conversation.session_id != "")
        time.sleep(0.1)
        transport._retire_idle()
        self.wait_for(lambda: not transport.conversations)
        self.assertIn("💤", self.bot.texts()[-1])


def part(message_id: int, file_id: str, *, text: str = "", group: str = "g1", chat: int = CHAT) -> telegram.Incoming:
    return telegram.Incoming(
        update_id=message_id,
        chat_id=chat,
        thread_id=0,
        user_id=7,
        text=text,
        message_id=message_id,
        files=(telegram.Attachment(file_id, f"{file_id}.jpg"),),
        media_group=group,
    )


class AlbumsArriveAsOneMessage(TempHome):
    def test_the_parts_wait_and_then_leave_under_the_caption(self) -> None:
        albums = poller_module.Albums(settle=1.5)
        self.assertTrue(albums.add(part(11, "a"), now=100.0))
        self.assertTrue(albums.add(part(12, "b", text="/rik look at these"), now=100.2))
        self.assertEqual(albums.ready(now=101.0), [])
        self.assertTrue(albums.add(part(13, "c"), now=101.0))
        self.assertEqual(albums.ready(now=102.0), [])

        (merged,) = albums.ready(now=102.6)
        self.assertEqual(merged.text, "/rik look at these")
        self.assertEqual(merged.message_id, 12)
        self.assertEqual([item.file_id for item in merged.files], ["a", "b", "c"])
        self.assertFalse(albums)

    def test_a_lone_message_and_other_albums_are_not_mixed_in(self) -> None:
        albums = poller_module.Albums(settle=1.0)
        lone = telegram.Incoming(update_id=1, chat_id=CHAT, thread_id=0, user_id=7, text="/rik hi")
        self.assertFalse(albums.add(lone))
        albums.add(part(1, "a", text="/rik one"), now=10.0)
        albums.add(part(2, "b", group="g2", text="/rik two"), now=10.0)
        albums.add(part(3, "c", chat=CHAT + 1), now=10.0)
        merged = albums.ready(now=20.0)
        self.assertEqual(sorted(len(item.files) for item in merged), [1, 1, 1])

    def test_an_album_nobody_captioned_is_still_one_message(self) -> None:
        albums = poller_module.Albums()
        albums.add(part(1, "a"), now=0.0)
        albums.add(part(2, "b"), now=0.0)
        (merged,) = albums.ready(everything=True)
        self.assertEqual(merged.text, "")
        self.assertEqual(len(merged.files), 2)
