"""Canaries proving the test guard fires if code reaches for real state or live runtimes."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

import _guard


class GuardCanaryTests(unittest.TestCase):
    def test_home_is_redirected_away_from_the_real_account_home(self):
        self.assertNotIn(os.environ["HOME"], _guard.PROTECTED_REAL_PATHS)
        self.assertTrue(os.environ["HOME"].startswith(str(_guard.FAKE_HOME)))
        self.assertNotIn("PERSONAL_AGENT_HOME", os.environ)

    def test_the_fake_home_is_removed_when_the_test_process_exits(self):
        probe = "import sys; sys.path.insert(0, sys.argv[1]); import _guard; print(_guard.FAKE_HOME)"
        result = subprocess.run(
            [sys.executable, "-B", "-c", probe, str(Path(_guard.__file__).parent)],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertFalse(Path(result.stdout.strip()).exists())

    def test_reading_a_real_protected_path_raises(self):
        target = Path(_guard.PROTECTED_REAL_PATHS[0]) / "canary-never-created"
        with self.assertRaises(_guard.GuardViolation):
            open(target, encoding="utf-8")

    def test_creating_a_directory_in_a_real_skill_home_raises(self):
        skills = next(p for p in _guard.PROTECTED_REAL_PATHS if p.endswith(".claude"))
        with self.assertRaises(_guard.GuardViolation):
            os.mkdir(os.path.join(skills, "canary-never-created"))

    def test_launching_a_real_runtime_binary_raises(self):
        for binary in ("herdr", "treehouse", "gh"):
            with self.assertRaises(_guard.GuardViolation):
                subprocess.run([binary, "--version"], capture_output=True)

    def test_a_subprocess_working_in_a_real_protected_path_raises(self):
        with self.assertRaises(_guard.GuardViolation):
            subprocess.run(["true"], cwd=_guard.PROTECTED_REAL_PATHS[0])


if __name__ == "__main__":
    unittest.main()
