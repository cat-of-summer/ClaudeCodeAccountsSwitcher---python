from __future__ import annotations

import unittest

from app.transport import routing
from app.transport.profiles import Profile


class LaunchOptions(unittest.TestCase):
    def test_ccas_flags_are_lifted_and_claude_flags_stay(self) -> None:
        options, rest = routing.split_launch_options(
            ["-t", "telegram", "--tg-chat", "-100123", "--tg-thread=7", "-C", "O:\\Projects\\x",
             "-n", "main", "--model", "opus", "Привет", "--tg-token", "1:abc", "-P", "2"]
        )
        self.assertEqual(options.transport, "telegram")
        self.assertEqual(options.chat, -100123)
        self.assertEqual(options.thread, 7)
        self.assertEqual(options.cwd, "O:\\Projects\\x")
        self.assertEqual(options.name, "main")
        self.assertEqual(options.token, "1:abc")
        self.assertEqual(options.profile, 2)
        self.assertEqual(rest, ["-n", "main", "--model", "opus", "Привет"])
        self.assertTrue(options.wants_transport)

    def test_no_flags_means_no_transport(self) -> None:
        options, rest = routing.split_launch_options(["--resume", "abc"])
        self.assertFalse(options.wants_transport)
        self.assertEqual(options.profile, 0)
        self.assertEqual(rest, ["--resume", "abc"])

    def test_long_forms(self) -> None:
        options, rest = routing.split_launch_options(["--transport=telegram", "--name=work", "--profile=3"])
        self.assertEqual(options.transport, "telegram")
        self.assertEqual(options.name, "work")
        self.assertEqual(options.profile, 3)
        self.assertEqual(rest, ["--name=work"])


class Tokenizer(unittest.TestCase):
    def test_quotes_group_and_backslashes_survive(self) -> None:
        self.assertEqual(
            routing.tokenize('-C "C:\\Users\\me\\my project" -n main say "hello there" \'\''),
            ["-C", "C:\\Users\\me\\my project", "-n", "main", "say", "hello there", ""],
        )
        self.assertEqual(routing.tokenize("   "), [])


class Addressing(unittest.TestCase):
    def test_a_slash_alias_opens_the_line(self) -> None:
        self.assertEqual(routing.parse_address("/rik build it"), routing.Address("rik", "build it"))
        self.assertEqual(routing.parse_address("  /rik   build it "), routing.Address("rik", "build it"))
        self.assertEqual(routing.parse_address("/rik"), routing.Address("rik", ""))
        self.assertEqual(routing.parse_address("/rik\nline two"), routing.Address("rik", "line two"))

    def test_the_bang_means_now(self) -> None:
        self.assertEqual(routing.parse_address("/rik! stop"), routing.Address("rik", "stop", urgent=True))
        self.assertEqual(routing.parse_address("/rik!"), routing.Address("rik", "", urgent=True))

    def test_telegram_bot_suffix_is_ignored(self) -> None:
        self.assertEqual(routing.parse_address("/rik@ccas_bot hi"), routing.Address("rik", "hi"))
        self.assertEqual(routing.parse_address("/rik!@ccas_bot hi"), routing.Address("rik", "hi", urgent=True))

    def test_anything_else_is_not_an_address(self) -> None:
        self.assertIsNone(routing.parse_address("rik build it"))
        self.assertIsNone(routing.parse_address("hello /rik"))
        self.assertIsNone(routing.parse_address(""))
        self.assertIsNone(routing.parse_address("/"))
        self.assertIsNone(routing.parse_address("/-bad"))

    def test_claude_command(self) -> None:
        command = routing.parse_claude_command("/claude -n main -C O:\\x -P 2 Привет мир")
        assert command is not None
        self.assertEqual(command.options.name, "main")
        self.assertEqual(command.options.cwd, "O:\\x")
        self.assertEqual(command.options.profile, 2)
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

    def profiles(self, *spec: tuple[str, tuple]) -> dict[int, Profile]:
        return {
            number: Profile(id=number, alias=alias, chats=chats)
            for number, (alias, chats) in enumerate(spec, start=1)
        }

    def test_the_alias_at_the_front_wins(self) -> None:
        known = self.profiles(("rikroot", ((-5, 0),)))
        found = routing.route("/rikroot build it", profiles=known, chat=-5)
        assert found is not None
        self.assertEqual((found.profile, found.body, found.urgent), (1, "build it", False))

        alone = routing.route("/rikroot", profiles=known, chat=-5)
        assert alone is not None
        self.assertEqual((alone.profile, alone.body), (1, ""))

        urgent = routing.route("/rikroot! stop", profiles=known, chat=-5)
        assert urgent is not None
        self.assertEqual((urgent.profile, urgent.body, urgent.urgent), (1, "stop", True))

    def test_a_profile_is_deaf_to_lines_that_do_not_say_its_name(self) -> None:
        """Its own chat included -- that is how a conversation is ended."""
        known = self.profiles(("rikroot", ((-5, 0),)))
        self.assertIsNone(routing.route("build it", profiles=known, chat=-5))
        self.assertIsNone(routing.route("rikroot build it", profiles=known, chat=-5))
        self.assertIsNone(routing.route("/claude", profiles=known, chat=-5))

    def test_a_profile_is_not_reached_from_a_chat_it_does_not_work_in(self) -> None:
        known = self.profiles(("rikroot", ((-5, 0),)))
        self.assertIsNone(routing.route("/rikroot hi", profiles=known, chat=-9))

    def test_a_profile_with_no_chats_answers_anywhere(self) -> None:
        known = self.profiles(("rikroot", ()))
        found = routing.route("/rikroot hi", profiles=known, chat=-9)
        assert found is not None
        self.assertEqual(found.profile, 1)

    def test_the_chat_picks_between_profiles_sharing_an_alias(self) -> None:
        known = self.profiles(("rik", ((-5, 0),)), ("rik", ((-6, 0),)), ("rik", ()))
        self.assertEqual(routing.route("/rik hi", profiles=known, chat=-5).profile, 1)  # type: ignore[union-attr]
        self.assertEqual(routing.route("/rik hi", profiles=known, chat=-6).profile, 2)  # type: ignore[union-attr]
        self.assertEqual(routing.route("/rik hi", profiles=known, chat=-7).profile, 3)  # type: ignore[union-attr]

    def test_two_claiming_one_chat_is_reported_not_guessed(self) -> None:
        known = self.profiles(("rik", ((-5, 0),)), ("rik", ((-5, 0),)))
        found = routing.route("/rik hi", profiles=known, chat=-5)
        assert found is not None
        self.assertEqual(found.profile, 0)
        self.assertEqual(found.ambiguous, (1, 2))
        self.assertEqual(found.body, "hi")

    def test_topics_narrow_a_chat(self) -> None:
        known = self.profiles(("rikroot", ((-5, 7),)))
        self.assertIsNotNone(routing.route("/rikroot hello", profiles=known, chat=-5, thread=7))
        self.assertIsNone(routing.route("/rikroot hello", profiles=known, chat=-5, thread=8))

    def test_alias_case_does_not_matter(self) -> None:
        known = self.profiles(("Rik", ()))
        self.assertEqual(routing.route("/rik hi", profiles=known, chat=-1).profile, 1)  # type: ignore[union-attr]


class OwnCommands(unittest.TestCase):
    def test_several_commands_then_the_prompt(self) -> None:
        commands, rest = routing.split_commands("/clear /plan Давай сделаем\nвторая строка")
        self.assertEqual(commands, [("/clear", ""), ("/plan", "")])
        self.assertEqual(rest, "Давай сделаем\nвторая строка")

    def test_commands_alone(self) -> None:
        self.assertEqual(routing.split_commands("/clear /plan "), ([("/clear", ""), ("/plan", "")], ""))
        self.assertEqual(routing.split_commands("/status"), ([("/status", "")], ""))
        self.assertEqual(routing.split_commands(""), ([], ""))

    def test_one_argument_commands_take_the_next_word(self) -> None:
        commands, rest = routing.split_commands('/cd "O:\\my project" /switch @work /plan go')
        self.assertEqual(commands, [("/cd", "O:\\my project"), ("/switch", "@work"), ("/plan", "")])
        self.assertEqual(rest, "go")
        self.assertEqual(routing.split_commands("/cd")[0], [("/cd", "")])

    def test_an_unknown_slash_command_is_the_prompt(self) -> None:
        self.assertEqual(routing.split_commands("/compact keep the tests"), ([], "/compact keep the tests"))
        commands, rest = routing.split_commands("/plan /compact now")
        self.assertEqual(commands, [("/plan", "")])
        self.assertEqual(rest, "/compact now")

    def test_plain_text_is_the_prompt(self) -> None:
        self.assertEqual(routing.split_commands("hello /plan"), ([], "hello /plan"))
        self.assertEqual(routing.split_commands("!ls -la"), ([], "!ls -la"))

    def test_bot_suffix_and_case(self) -> None:
        self.assertEqual(routing.split_commands("/Plan@ccas_bot go"), ([("/plan", "")], "go"))
