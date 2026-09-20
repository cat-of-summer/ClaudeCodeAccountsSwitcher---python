from __future__ import annotations

import unittest

from app.transport import routing


class LaunchOptions(unittest.TestCase):
    def test_ccas_flags_are_lifted_and_claude_flags_stay(self) -> None:
        options, rest = routing.split_launch_options(
            ["-t", "telegram", "--tg-chat", "-100123", "--tg-thread=7", "-C", "O:\\Projects\\x",
             "-n", "main", "--model", "opus", "Привет", "--tg-token", "1:abc"]
        )
        self.assertEqual(options.transport, "telegram")
        self.assertEqual(options.chat, -100123)
        self.assertEqual(options.thread, 7)
        self.assertEqual(options.cwd, "O:\\Projects\\x")
        self.assertEqual(options.name, "main")
        self.assertEqual(options.token, "1:abc")
        self.assertEqual(rest, ["-n", "main", "--model", "opus", "Привет"])
        self.assertTrue(options.wants_transport)

    def test_no_flags_means_no_transport(self) -> None:
        options, rest = routing.split_launch_options(["--resume", "abc"])
        self.assertFalse(options.wants_transport)
        self.assertEqual(rest, ["--resume", "abc"])

    def test_long_forms(self) -> None:
        options, rest = routing.split_launch_options(["--transport=telegram", "--name=work"])
        self.assertEqual(options.transport, "telegram")
        self.assertEqual(options.name, "work")
        self.assertEqual(rest, ["--name=work"])


class Tokenizer(unittest.TestCase):
    def test_quotes_group_and_backslashes_survive(self) -> None:
        self.assertEqual(
            routing.tokenize('-C "C:\\Users\\me\\my project" -n main say "hello there" \'\''),
            ["-C", "C:\\Users\\me\\my project", "-n", "main", "say", "hello there", ""],
        )
        self.assertEqual(routing.tokenize("   "), [])


class Addressing(unittest.TestCase):
    def test_prefix_is_required_when_set(self) -> None:
        self.assertIsNone(routing.address("hello", prefix="cc:", aliases=[]))
        self.assertEqual(routing.address("cc: hello", prefix="cc:", aliases=[]), routing.Addressed("", "hello"))
        # Slash commands are unambiguous, so they pass without the prefix.
        self.assertEqual(routing.address("/claude -n a", prefix="cc:", aliases=[]), routing.Addressed("", "/claude -n a"))

    def test_alias_routes_and_alone_is_a_ping(self) -> None:
        self.assertEqual(routing.address("main /usage", prefix="", aliases=["main"]), routing.Addressed("main", "/usage"))
        self.assertEqual(routing.address("main", prefix="", aliases=["main"]), routing.Addressed("main", ""))
        self.assertEqual(routing.address("mainly fine", prefix="", aliases=["main"]), routing.Addressed("", "mainly fine"))
        self.assertEqual(routing.address("", prefix="", aliases=[]), routing.Addressed("", ""))

    def test_claude_command(self) -> None:
        command = routing.parse_claude_command("/claude -n main -C O:\\x Привет мир")
        assert command is not None
        self.assertEqual(command.options.name, "main")
        self.assertEqual(command.options.cwd, "O:\\x")
        self.assertEqual(command.args, ["-n", "main", "Привет", "мир"])
        self.assertIsNotNone(routing.parse_claude_command("/claude@ccas_bot mcp list"))
        self.assertIsNone(routing.parse_claude_command("/claudex"))
        self.assertIsNone(routing.parse_claude_command("hello"))
        bare = routing.parse_claude_command("/claude")
        assert bare is not None
        self.assertEqual(bare.args, [])

    def test_alias_validity(self) -> None:
        self.assertTrue(routing.valid_alias("main"))
        self.assertTrue(routing.valid_alias("site-2.dev"))
        self.assertFalse(routing.valid_alias("-bad"))
        self.assertFalse(routing.valid_alias("has space"))


class ProfileRouting(unittest.TestCase):
    """Who a chat line belongs to, decided once for the daemon and the transport."""

    def profiles(self, **spec: tuple) -> dict:
        from app.transport.profiles import Profile

        made = {name: Profile(name=name, chats=chats) for name, chats in spec.items()}
        made.setdefault("default", Profile(name="default"))
        return made

    def test_a_name_at_the_front_wins(self) -> None:
        known = self.profiles(rikroot=((-5, 0),))
        found = routing.route("rikroot build it", prefix="", profiles=known, chat=-9)
        assert found is not None
        self.assertEqual((found.profile, found.body, found.addressed), ("rikroot", "build it", True))

        alone = routing.route("rikroot", prefix="", profiles=known, chat=-9)
        assert alone is not None
        self.assertEqual((alone.profile, alone.body), ("rikroot", ""))

    def test_the_one_profile_that_named_the_chat_takes_plain_lines(self) -> None:
        known = self.profiles(rikroot=((-5, 0),))
        found = routing.route("build it", prefix="", profiles=known, chat=-5)
        assert found is not None
        self.assertEqual((found.profile, found.addressed), ("rikroot", False))

    def test_default_takes_what_nobody_claimed(self) -> None:
        known = self.profiles(rikroot=((-5, 0),))
        found = routing.route("hello", prefix="", profiles=known, chat=-9)
        assert found is not None
        self.assertEqual(found.profile, "default")

        # Once default names its own chats it stops taking the others.
        from app.transport.profiles import Profile

        known["default"] = Profile(name="default", chats=((-1, 0),))
        self.assertIsNone(routing.route("hello", prefix="", profiles=known, chat=-9))
        pinned = routing.route("hello", prefix="", profiles=known, chat=-1)
        assert pinned is not None
        self.assertEqual(pinned.profile, "default")

    def test_a_named_profile_with_no_chats_is_reachable_only_by_name(self) -> None:
        known = self.profiles(rikroot=())
        found = routing.route("hello", prefix="", profiles=known, chat=-9)
        assert found is not None
        self.assertEqual(found.profile, "default")

    def test_two_profiles_on_one_chat_need_the_name(self) -> None:
        known = self.profiles(a=((-5, 0),), b=((-5, 0),))
        self.assertIsNone(routing.route("hello", prefix="", profiles=known, chat=-5))
        found = routing.route("b hello", prefix="", profiles=known, chat=-5)
        assert found is not None
        self.assertEqual(found.profile, "b")

    def test_topics_narrow_a_chat(self) -> None:
        known = self.profiles(rikroot=((-5, 7),))
        inside = routing.route("hello", prefix="", profiles=known, chat=-5, thread=7)
        assert inside is not None
        self.assertEqual(inside.profile, "rikroot")
        outside = routing.route("hello", prefix="", profiles=known, chat=-5, thread=8)
        assert outside is not None
        self.assertEqual(outside.profile, "default")

    def test_the_prefix_is_required_but_never_for_names_or_commands(self) -> None:
        known = self.profiles(rikroot=((-5, 0),))
        self.assertIsNone(routing.route("hello", prefix="cc:", profiles=known, chat=-5))
        with_prefix = routing.route("cc: hello", prefix="cc:", profiles=known, chat=-5)
        assert with_prefix is not None
        self.assertEqual((with_prefix.profile, with_prefix.body), ("rikroot", "hello"))

        by_name = routing.route("rikroot hello", prefix="cc:", profiles=known, chat=-9)
        assert by_name is not None
        self.assertEqual(by_name.profile, "rikroot")

        command = routing.route("/claude", prefix="cc:", profiles=known, chat=-5)
        assert command is not None
        self.assertEqual(command.body, "/claude")
