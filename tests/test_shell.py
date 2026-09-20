from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from system import shell


class Decoding(unittest.TestCase):
    def test_utf8_is_read_as_utf8(self) -> None:
        self.assertEqual(shell.decode("привет".encode(), fallback="cp866"), "привет")

    def test_oem_bytes_fall_back_to_the_oem_code_page(self) -> None:
        # What cmd.exe writes on a Russian Windows.
        data = "не является внутренней или внешней командой".encode("cp866")
        self.assertEqual(shell.decode(data, fallback="cp866"), "не является внутренней или внешней командой")

    def test_garbage_never_raises(self) -> None:
        self.assertIn("\ufffd", shell.decode(b"\xff\xfe\xfd", fallback="nonsense"))


class TheShellItPicks(unittest.TestCase):
    def test_windows_gets_powershell_with_utf8_output(self) -> None:
        with mock.patch.object(shell, "IS_WINDOWS", True), mock.patch.object(
            shell.shutil, "which", lambda name: r"C:\ps\pwsh.exe" if name == "pwsh" else None
        ):
            argv = shell.shell_argv("pwd")
        assert argv is not None
        self.assertEqual(argv[0], r"C:\ps\pwsh.exe")
        self.assertIn("-NonInteractive", argv)
        self.assertTrue(argv[-1].endswith("pwd"))
        self.assertIn("UTF8", argv[-1])

    def test_posix_gets_bash(self) -> None:
        with mock.patch.object(shell, "IS_WINDOWS", False), mock.patch.object(
            shell.shutil, "which", lambda name: "/bin/bash" if name == "bash" else None
        ):
            self.assertEqual(shell.shell_argv("pwd"), ["/bin/bash", "-c", "pwd"])

    def test_without_a_known_shell_the_os_default_is_left(self) -> None:
        with mock.patch.object(shell.shutil, "which", lambda name: None):
            self.assertIsNone(shell.shell_argv("pwd"))


class Running(unittest.TestCase):
    def test_pwd_answers_with_the_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            # PowerShell prints `pwd` as a table with a header; the bare path
            # needs asking for.
            command = "(Get-Location).Path" if shell.IS_WINDOWS else "pwd"
            completed = shell.run(command, cwd=cwd, timeout=30)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(os.path.normcase(completed.stdout.strip()), os.path.normcase(str(cwd)))

    def test_output_is_utf8_even_for_non_ascii(self) -> None:
        completed = shell.run("echo привет", cwd=Path.cwd(), timeout=30)
        self.assertEqual(completed.stdout.strip(), "привет")


if __name__ == "__main__":
    unittest.main()
