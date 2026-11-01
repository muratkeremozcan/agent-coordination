"""State-root safety: no default, no write before a valid root, no protected aliases.

Protected locations are built under the redirected test HOME, so every alias and refusal
is proven with safe fixtures. The real-home cases only resolve paths; the guard would turn
any write there into a test failure.
"""

import json
import os
import pwd
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import _guard  # noqa: F401  (must load first: it installs the real-state guard)
import state_root
from helpers import DEFAULT_PROFILE, SESSION, TempRoot, all_authority, cli

START = [
    "start",
    "--title",
    "t",
    "--intent",
    "i",
    "--coordination-reason",
    "r",
    "--delivery-authority",
    "d",
    "--authority",
    json.dumps(all_authority()),
    "--profile",
    str(DEFAULT_PROFILE),
    "--session-id",
    SESSION,
    "--coordinator-runtime",
    "codex",
]


def tree(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


class MissingRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempRoot(self)
        self.cwd = self.tmp.base / "cwd"
        self.cwd.mkdir()
        previous = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)

    def test_every_command_without_a_root_exits_2_before_creating_anything(self):
        home = Path(os.environ["HOME"])
        home_before, cwd_before = tree(home), tree(self.cwd)
        for args in (
            START,
            ["list"],
            ["snapshot", "--run-id", "x-000000"],
            ["open-decision", "--run-id", "x-000000", "--session-id", SESSION, "--question", "q"],
        ):
            code, _, err = cli(None, args)
            self.assertEqual(code, 2, args)
            self.assertIn("no state root configured", err)
        self.assertEqual(tree(home), home_before)
        self.assertEqual(tree(self.cwd), cwd_before)

    def test_the_cli_process_exits_nonzero_without_a_root(self):
        env = {k: v for k, v in os.environ.items() if k != "COORDINATE_STATE_ROOT"}
        script = Path(__file__).resolve().parent.parent / "scripts" / "coordinator_state.py"
        result = subprocess.run(
            [sys.executable, "-B", str(script), "list"], capture_output=True, text=True, env=env, cwd=self.cwd
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(tree(self.cwd), [])

    def test_a_relative_root_is_refused(self):
        code, _, err = cli(None, START[:1] + ["--state-root", "relative/state"] + START[1:])
        self.assertEqual(code, 2)
        self.assertIn("must be an absolute path", err)
        self.assertFalse((self.cwd / "relative").exists())

    def test_the_environment_variable_is_honored(self):
        root = self.tmp.base / "from-env"
        code, _, err = cli(None, START, env={state_root.STATE_ROOT_ENV: str(root)})
        self.assertEqual(code, 0, err)
        self.assertEqual(len(list((root / "runs").glob("*.json"))), 1)


class ProtectedRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempRoot(self)
        self.home = Path(os.environ["HOME"]).resolve()

    def refused(self, root: Path, env: dict | None = None) -> str:
        code, _, err = cli(None, START[:1] + ["--state-root", str(root)] + START[1:], env=env)
        self.assertEqual(code, 2, err)
        self.assertIn("protected location", err)
        return err

    def test_each_protected_location_is_refused(self):
        for relative in ("personal-agent", "opensource/personal-agent", ".agents", ".claude", ".codex"):
            target = self.home / relative / "coordinator-state"
            self.refused(target)
            self.assertFalse(target.exists(), relative)

    def test_the_personal_agent_state_home_from_the_environment_is_refused(self):
        custom = self.tmp.base / "custom-home"
        custom.mkdir()
        self.refused(
            custom / "_bmad" / "memory" / "personal-agent" / ".runtime", env={"PERSONAL_AGENT_HOME": str(custom)}
        )
        self.assertFalse((custom / "_bmad").exists())

    def test_a_symlink_alias_into_a_protected_location_is_refused(self):
        (self.home / ".claude").mkdir(exist_ok=True)
        alias = self.tmp.base / "innocent-looking"
        alias.symlink_to(self.home / ".claude")
        self.refused(alias / "state")
        self.assertEqual(list((self.home / ".claude").iterdir()), [])

    def test_a_symlinked_ancestor_with_a_nonexistent_descendant_is_refused(self):
        (self.home / "personal-agent").mkdir(exist_ok=True)
        alias = self.tmp.base / "pa-link"
        alias.symlink_to(self.home / "personal-agent")
        self.refused(alias / "not" / "yet" / "created")
        self.assertFalse((self.home / "personal-agent" / "not").exists())

    def test_parent_components_apply_after_symlinks_like_the_filesystem_does(self):
        other = self.tmp.base / "other"
        (other / "nested").mkdir(parents=True)
        (other / "live").mkdir()
        alias = self.tmp.base / "alias"
        alias.symlink_to(other / "nested")
        env = {"PERSONAL_AGENT_HOME": f"{alias}/../live"}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(state_root.StateRootError):
                state_root.resolve_state_root(str(other / "live" / "state"))
        self.assertEqual(state_root.canonicalize(Path(f"{alias}/../live")), (other / "live").resolve())

    def test_a_runs_symlink_inside_the_root_cannot_escape_into_a_protected_location(self):
        protected = self.home / "personal-agent"
        protected.mkdir(exist_ok=True)
        before = tree(protected)
        root = self.tmp.base / "state"
        root.mkdir()
        (root / "runs").symlink_to(protected)
        code, _, err = cli(None, START[:1] + ["--state-root", str(root)] + START[1:])
        self.assertEqual(code, 2, err)
        self.assertIn("outside the state root", err)
        self.assertEqual(tree(protected), before)

    def test_dot_dot_traversal_into_a_protected_location_is_refused(self):
        self.refused(self.home / "x" / ".." / ".codex" / "state")

    def test_a_case_variant_spelling_of_a_protected_location_is_refused(self):
        (self.home / "personal-agent").mkdir(exist_ok=True)
        self.refused(self.home / "PERSONAL-AGENT" / "state")
        self.refused(self.home / ".CODEX" / "state")
        self.assertFalse((self.home / "PERSONAL-AGENT" / "state").exists())

    def test_an_inode_alias_of_a_protected_location_is_refused(self):
        (self.home / ".agents").mkdir(exist_ok=True)
        with mock.patch.object(state_root, "_folded", side_effect=lambda p: p):
            with mock.patch.object(state_root, "canonicalize", side_effect=lambda p: Path(os.path.abspath(p))):
                alias = self.tmp.base / "hard-alias"
                alias.symlink_to(self.home / ".agents")
                with self.assertRaises(state_root.StateRootError):
                    state_root.check_not_protected(alias / "state", purpose="state root")

    def test_an_ancestor_of_a_protected_location_is_refused(self):
        self.refused(self.home)

    def test_real_protected_paths_are_refused_even_with_home_redirected(self):
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        for relative in (".claude", "personal-agent"):
            with self.assertRaises(state_root.StateRootError):
                state_root.resolve_state_root(str(real_home / relative / "coordinator"))

    def test_canonicalize_resolves_the_existing_prefix_and_keeps_the_rest(self):
        real = self.tmp.base / "real"
        real.mkdir()
        link = self.tmp.base / "link"
        link.symlink_to(real)
        self.assertEqual(state_root.canonicalize(link / "new" / ".." / "child"), real / "child")

    def test_a_safe_root_is_created_lazily_on_first_write(self):
        root = self.tmp.base / "safe"
        self.assertEqual(state_root.resolve_state_root(str(root)), root)
        self.assertFalse(root.exists())
        with mock.patch.dict(os.environ, {}):
            code, _, err = cli(None, START[:1] + ["--state-root", str(root)] + START[1:])
        self.assertEqual(code, 0, err)
        self.assertTrue((root / "runs").is_dir())


if __name__ == "__main__":
    unittest.main()
