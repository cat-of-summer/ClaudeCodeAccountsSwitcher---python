from __future__ import annotations

import io
import unittest
from unittest import mock

from system import console


class TestModeBits(unittest.TestCase):
    def test_a_sane_input_mode_can_be_typed_into(self) -> None:
        mode = console.sane_input_mode()
        for bit in (
            console.ENABLE_PROCESSED_INPUT,
            console.ENABLE_LINE_INPUT,
            console.ENABLE_ECHO_INPUT,
        ):
            self.assertTrue(mode & bit, hex(bit))

    def test_insert_and_quick_edit_come_with_extended_flags(self) -> None:
        """Without ENABLE_EXTENDED_FLAGS the console ignores both."""
        mode = console.sane_input_mode()
        self.assertTrue(mode & console.ENABLE_INSERT_MODE)
        self.assertTrue(mode & console.ENABLE_QUICK_EDIT_MODE)
        self.assertTrue(mode & console.ENABLE_EXTENDED_FLAGS)

    def test_a_sane_output_mode_understands_escapes(self) -> None:
        mode = console.sane_output_mode()
        self.assertTrue(mode & console.ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        self.assertTrue(mode & console.ENABLE_PROCESSED_OUTPUT)

    def test_raw_mode_as_ink_leaves_it_is_broken(self) -> None:
        # VT input only: no line input, no Ctrl-C processing.
        self.assertTrue(console.is_broken(console.ENABLE_VIRTUAL_TERMINAL_INPUT))
        self.assertTrue(console.is_broken(0))
        # Line input without processed input still swallows Ctrl-C.
        self.assertTrue(console.is_broken(console.ENABLE_LINE_INPUT))

    def test_a_normal_console_is_not_broken(self) -> None:
        self.assertFalse(console.is_broken(0x1F7))
        self.assertFalse(console.is_broken(console.sane_input_mode()))


class TestResetSequences(unittest.TestCase):
    def test_everything_a_killed_ink_leaves_on_is_turned_off(self) -> None:
        joined = "".join(console.RESET_SEQUENCES)
        self.assertIn("\033[?1049l", joined)  # alternate screen
        self.assertIn("\033[?25h", joined)  # cursor
        for mouse in ("1000", "1002", "1003", "1006", "1015"):
            self.assertIn(f"\033[?{mouse}l", joined)
        self.assertIn("\033[?2004l", joined)  # bracketed paste
        self.assertIn("\033[0m", joined)

    def test_nothing_in_the_reset_turns_anything_on(self) -> None:
        for sequence in console.RESET_SEQUENCES:
            if sequence.startswith("\033[?"):
                self.assertTrue(
                    sequence.endswith("l") or sequence in ("\033[?25h", "\033[?7h"),
                    sequence,
                )


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestSanitize(unittest.TestCase):
    def test_writes_the_reset_to_a_tty(self) -> None:
        stream = _Tty()
        with mock.patch.object(console.sys, "stdout", stream):
            console.sanitize()
        self.assertEqual(stream.getvalue(), "".join(console.RESET_SEQUENCES))

    def test_says_nothing_to_a_pipe(self) -> None:
        stream = io.StringIO()
        with mock.patch.object(console.sys, "stdout", stream):
            console.sanitize()
        self.assertEqual(stream.getvalue(), "")


class TestRepair(unittest.TestCase):
    def test_a_healthy_console_is_left_alone(self) -> None:
        healthy = console.State(input_mode=console.sane_input_mode(), output_mode=7)
        with (
            mock.patch.object(console, "IS_WINDOWS", True),
            mock.patch.object(console, "_snapshot_windows", return_value=healthy),
            mock.patch.object(console, "restore") as restore,
            mock.patch.object(console, "sanitize") as sanitize,
        ):
            self.assertFalse(console.repair())
        restore.assert_not_called()
        sanitize.assert_not_called()

    def test_a_raw_console_is_reset_to_defaults(self) -> None:
        raw = console.State(input_mode=console.ENABLE_VIRTUAL_TERMINAL_INPUT, output_mode=7)
        with (
            mock.patch.object(console, "IS_WINDOWS", True),
            mock.patch.object(console, "_snapshot_windows", return_value=raw),
            mock.patch.object(console, "restore") as restore,
            mock.patch.object(console, "sanitize") as sanitize,
        ):
            self.assertTrue(console.repair())
        restore.assert_called_once_with(None)
        sanitize.assert_called_once()

    def test_no_console_at_all_is_not_an_error(self) -> None:
        with (
            mock.patch.object(console, "IS_WINDOWS", True),
            mock.patch.object(console, "_snapshot_windows", return_value=None),
        ):
            self.assertFalse(console.repair())
