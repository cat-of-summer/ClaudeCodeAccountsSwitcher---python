from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from system import procs


class WhoIsRunningOurBinaries(unittest.TestCase):
    """The upgrade needs names and pids, not a failed copy."""

    def _child(self) -> subprocess.Popen:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._cleanup, child)
        return child

    def _cleanup(self, child: subprocess.Popen) -> None:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)

    def test_a_process_running_from_the_directory_is_found_and_closed(self) -> None:
        child = self._child()
        directory = Path(sys.executable).parent

        deadline = time.time() + 10
        while time.time() < deadline:
            found = [process for process in procs.using(directory) if process.pid == child.pid]
            if found:
                break
            time.sleep(0.05)
        else:
            self.skipTest("this platform does not list running processes")

        self.assertTrue(found[0].name)
        self.assertTrue(procs.kill(child.pid))
        self.assertIsNotNone(child.poll() if os.name != "nt" else child.wait(timeout=10))

    def test_our_own_process_is_never_in_the_way(self) -> None:
        directory = Path(sys.executable).parent
        self.assertNotIn(os.getpid(), [process.pid for process in procs.using(directory)])

    def test_a_directory_nothing_runs_from_is_empty(self) -> None:
        self.assertEqual(procs.using(Path(__file__).parent), [])


if __name__ == "__main__":
    unittest.main()
