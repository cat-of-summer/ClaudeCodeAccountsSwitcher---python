from __future__ import annotations

from unittest import mock

from app import wrapper
from core import store
from core.store import Accounts, Slot
from tests.base import TempHome


class TestArgumentParsing(TempHome):
    def _accounts(self) -> Accounts:
        accounts = Accounts()
        accounts.slots[1] = Slot(number=1, alias="personal", email="a@example.com")
        accounts.slots[2] = Slot(number=2, alias="work", email="b@example.com")
        return accounts

    def test_bare_digit_selects_slot(self) -> None:
        slot, rest, explicit = wrapper.resolve_slot(["2", "-p", "hi"], self._accounts())
        self.assertEqual(slot, 2)
        self.assertEqual(rest, ["-p", "hi"])
        self.assertTrue(explicit)

    def test_at_prefix_resolves_alias(self) -> None:
        slot, rest, explicit = wrapper.resolve_slot(["@work"], self._accounts())
        self.assertEqual(slot, 2)
        self.assertEqual(rest, [])
        self.assertTrue(explicit)

    def test_unknown_alias_is_an_error(self) -> None:
        with self.assertRaises(wrapper.WrapperError):
            wrapper.resolve_slot(["@nope"], self._accounts())

    def test_bare_word_stays_a_prompt(self) -> None:
        slot, rest, explicit = wrapper.resolve_slot(["work"], self._accounts())
        self.assertIsNone(slot)
        self.assertEqual(rest, ["work"])
        self.assertFalse(explicit)

    def test_double_dash_escapes_a_numeric_prompt(self) -> None:
        slot, rest, explicit = wrapper.resolve_slot(["--", "2"], self._accounts())
        self.assertIsNone(slot)
        self.assertEqual(rest, ["2"])
        self.assertFalse(explicit)

    def test_subcommand_passes_through(self) -> None:
        slot, rest, _ = wrapper.resolve_slot(["mcp", "list"], self._accounts())
        self.assertIsNone(slot)
        self.assertEqual(rest, ["mcp", "list"])

    def test_empty_args(self) -> None:
        slot, rest, explicit = wrapper.resolve_slot([], self._accounts())
        self.assertIsNone(slot)
        self.assertEqual(rest, [])
        self.assertFalse(explicit)


class TestDefaultArgs(TempHome):
    def test_default_is_injected(self) -> None:
        merged = wrapper.merge_default_args(
            ["--dangerously-skip-permissions"], ["-p", "hi"]
        )
        self.assertEqual(merged, ["--dangerously-skip-permissions", "-p", "hi"])

    def test_not_injected_twice(self) -> None:
        merged = wrapper.merge_default_args(
            ["--dangerously-skip-permissions"], ["--dangerously-skip-permissions"]
        )
        self.assertEqual(merged, ["--dangerously-skip-permissions"])

    def test_allow_variant_suppresses_the_default(self) -> None:
        merged = wrapper.merge_default_args(
            ["--dangerously-skip-permissions"], ["--allow-dangerously-skip-permissions"]
        )
        self.assertEqual(merged, ["--allow-dangerously-skip-permissions"])

    def test_subcommands_get_no_defaults(self) -> None:
        merged = wrapper.merge_default_args(
            ["--dangerously-skip-permissions"], ["mcp", "list"]
        )
        self.assertEqual(merged, ["mcp", "list"])


class TestBareInvocation(TempHome):
    def test_bare_claude_never_prompts(self) -> None:
        """A bare `claude` resumes the last account instead of opening a picker."""
        self.assertFalse(hasattr(wrapper, "wants_menu"))


class TestStartFromCcas(TempHome):
    """`ccas run 2`, a bare `2` at the prompt and Enter in the menu all land here."""

    def setUp(self) -> None:
        super().setUp()
        from core.store import Config

        Config(real_claude_path="/bin/claude").save()
        self.write_credentials(store.creds_file(2))
        accounts = Accounts()
        accounts.slots[2] = Slot(number=2, alias="work", email="b@example.com")
        accounts.save()

    def test_run_hands_the_slot_to_the_wrapper(self) -> None:
        import argparse

        from app import cli

        with mock.patch.object(wrapper, "run_slot", return_value=0) as run:
            cli.cmd_run(argparse.Namespace(target="@work", claude_args=["--verbose"]))

        run.assert_called_once()
        _, slot_number, args = run.call_args.args
        self.assertEqual(slot_number, 2)
        self.assertEqual(args, ["--verbose"])
        self.assertEqual(Accounts.load().active, 2)

    def test_a_bare_number_at_the_prompt_is_a_run(self) -> None:
        from app import cli

        parser = cli.build_parser()
        with mock.patch.object(wrapper, "run_slot", return_value=0) as run:
            cli._dispatch(parser, ["run", "2"])
        self.assertEqual(run.call_args.args[1], 2)

    def test_a_slot_nobody_signed_into_is_not_started(self) -> None:
        import argparse

        from app import cli

        Accounts.load()  # slot 3 exists in no store and has no credentials
        with mock.patch.object(wrapper, "run_slot") as run, self.assertRaises(SystemExit):
            cli.cmd_run(argparse.Namespace(target="3", claude_args=[]))
        run.assert_not_called()
