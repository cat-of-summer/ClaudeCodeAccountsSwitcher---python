from __future__ import annotations

from app import wrapper
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
