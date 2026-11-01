"""TreeHouse adapter against a fake v2.0.0 CLI and disposable Git repositories only.

No real pool entry is acquired, returned or destroyed: the guard refuses to launch a binary
named `treehouse`, and every repository here is created fresh with independent metadata.
"""

import contextlib
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import _guard  # noqa: F401  (must load first: it installs the real-state guard)
import workspace_treehouse as wt
from helpers import EXAMPLE_PROFILE, Ledger, TempRoot, all_authority, clean_evidence, git, make_repo

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_treehouse.py"
READY = ["test", "-f", ".deps-ready"]


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TempRoot(self)
        self.root = self.tmp.root
        self.repo = self.tmp.base / "repo"
        self.base = make_repo(self.repo)
        self.fake_dir = self.tmp.base / "fake-treehouse"
        self.env = {
            wt.TREEHOUSE_BIN_ENV: str(FAKE),
            "FAKE_TREEHOUSE_DIR": str(self.fake_dir),
            "FAKE_TREEHOUSE_MODE": "ok",
        }
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mode(self, mode: str) -> None:
        os.environ["FAKE_TREEHOUSE_MODE"] = mode

    def calls(self) -> list[dict]:
        log = self.fake_dir / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def acquire(self, ws: str = "ws-1", run_id: str = "fixture-run-abc123", **kwargs) -> dict:
        args = dict(
            run_id=run_id,
            ws_id=ws,
            repo=self.repo,
            base=self.base,
            branch=f"story-{ws}",
            readiness_cmd=READY,
            lockfiles=[],
        )
        args.update(kwargs)
        return wt.acquire(self.root, **args)

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wt.main([args[0], "--state-root", str(self.root), *args[1:]])
        return code, out.getvalue(), err.getvalue()


class AcquireTests(AdapterCase):
    def test_a_ready_lease_is_verified_on_the_pinned_base_and_new_branch(self):
        receipt = self.acquire(lockfiles=[".gitignore"])
        self.assertEqual(receipt["provisioning"]["status"], "ready", receipt["provisioning"]["problems"])
        path = Path(receipt["path"])
        self.assertEqual(git(path, "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(path, "symbolic-ref", "--short", "HEAD"), "story-ws-1")
        self.assertEqual(receipt["observed_head_sha"], receipt["expected_base_sha"])
        self.assertEqual(receipt["lease_holder"], "fixture-run-abc123/ws-1")
        self.assertFalse(receipt["lease_observed"])
        self.assertTrue(any("machine-readable lease status" in gap for gap in receipt["capability_gaps"]))
        self.assertEqual(self.calls()[0]["argv"], ["get", "--lease", "--lease-holder", "fixture-run-abc123/ws-1"])
        self.assertEqual(Path(self.calls()[0]["cwd"]).resolve(), self.repo.resolve())
        readiness = receipt["provisioning"]["readiness"]
        self.assertEqual(readiness["exit_code"], 0)
        self.assertTrue(Path(readiness["artifact"]).is_file())
        self.assertIn(".gitignore", receipt["provisioning"]["lockfiles"])

    def test_the_story_base_is_pinned_even_when_the_root_checkout_has_moved_on(self):
        (self.repo / "later.txt").write_text("later\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "root moved on")
        receipt = self.acquire()
        self.assertEqual(receipt["provisioning"]["status"], "ready")
        self.assertEqual(git(Path(receipt["path"]), "rev-parse", "HEAD"), self.base)

    def test_a_hook_that_moves_head_after_branch_creation_blocks_dispatch(self):
        (self.repo / "later.txt").write_text("later\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "later")
        later = git(self.repo, "rev-parse", "HEAD")
        hook = (
            Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks" / "post-checkout"
        )
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f"#!/bin/sh\ngit reset -q --hard {later}\n")
        hook.chmod(0o755)
        receipt = self.acquire()
        self.assertEqual(receipt["provisioning"]["status"], "blocked")
        self.assertEqual(receipt["observed_head_sha"], later)
        self.assertIn("does not match the pinned base", receipt["provisioning"]["problems"][0])

    def test_a_swallowed_hook_failure_leaves_the_workspace_leased_and_blocked(self):
        self.mode("hook-fail")
        receipt = self.acquire()
        self.assertEqual(receipt["provisioning"]["status"], "blocked")
        self.assertIn("post_create hook failed", receipt["stderr_tail"])
        self.assertIn("readiness check exited 1", receipt["provisioning"]["problems"][0])
        self.assertTrue(Path(receipt["path"]).is_dir())
        self.assertEqual([c["argv"][0] for c in self.calls()], ["get"])

    def test_a_pool_entry_with_leftovers_is_retained_without_switching_branches(self):
        self.mode("dirty")
        receipt = self.acquire()
        self.assertEqual(receipt["provisioning"]["status"], "blocked")
        self.assertTrue((Path(receipt["path"]) / "leftover.txt").exists())
        self.assertNotEqual(git(Path(receipt["path"]), "rev-parse", "--abbrev-ref", "HEAD"), "story-ws-1")

    def test_a_path_from_another_repository_is_blocked_not_used(self):
        foreign = self.tmp.base / "foreign"
        make_repo(foreign)
        os.environ["FAKE_TREEHOUSE_FOREIGN"] = str(foreign)
        self.mode("foreign")
        receipt = self.acquire()
        self.assertEqual(receipt["provisioning"]["status"], "blocked")
        self.assertTrue(any("not the intended repository" in p for p in receipt["provisioning"]["problems"]))

    def test_a_subdirectory_of_the_root_checkout_is_never_provisioned(self):
        self.mode("subdir")
        receipt = self.acquire()
        self.assertEqual(receipt["provisioning"]["status"], "blocked")
        self.assertIn("root checkout or inside it", receipt["provisioning"]["problems"][0])
        self.assertEqual(git(self.repo, "symbolic-ref", "--short", "HEAD"), "main")

    def test_a_concurrent_acquire_for_the_same_holder_fails_fast(self):
        with wt._holder_lock(self.root, "fixture-run-abc123", "ws-1"):
            with self.assertRaisesRegex(wt.AdapterError, "in progress"):
                self.acquire()
        self.assertEqual(self.calls(), [])

    def test_a_workspaces_symlink_cannot_escape_the_state_root(self):
        outside = self.tmp.base / "outside"
        outside.mkdir()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "workspaces").symlink_to(outside)
        with self.assertRaisesRegex(wt.StateRootError, "outside the state root"):
            self.acquire()
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_missing_treehouse_binary_reserves_nothing(self):
        os.environ[wt.TREEHOUSE_BIN_ENV] = str(self.tmp.base / "no-such-treehouse")
        with self.assertRaisesRegex(wt.AdapterError, "was not found; nothing was reserved"):
            self.acquire()
        self.assertEqual(list((self.root / "workspaces").glob("*.json")), [])

    def test_an_error_after_reservation_keeps_the_validated_path_in_the_journal(self):
        with mock.patch.object(wt, "_provision", side_effect=wt.AdapterError("git exploded")):
            with self.assertRaisesRegex(wt.AdapterError, "git exploded"):
                self.acquire()
        journal = json.loads((self.root / "workspaces" / "fixture-run-abc123__ws-1.json").read_text())
        self.assertEqual(journal["state"], "blocked")
        self.assertTrue(Path(journal["receipt"]["path"]).is_dir())
        self.assertIn("adapter error after reservation", journal["receipt"]["provisioning"]["problems"][-1])

    def test_reconcile_works_when_no_receipt_was_ever_written(self):
        with mock.patch.object(wt, "_lease_and_provision", side_effect=OSError("binary vanished")):
            with self.assertRaisesRegex(wt.AdapterError, "binary vanished"):
                self.acquire()
        found = self.tmp.base / "found"
        git(self.repo, "worktree", "add", "--detach", str(found), "HEAD")
        entry = wt.reconcile(self.root, run_id="fixture-run-abc123", ws_id="ws-1", path=found, evidence="status row")
        self.assertEqual(entry["state"], "blocked")

    def test_a_missing_lockfile_blocks_provisioning(self):
        receipt = self.acquire(lockfiles=["package-lock.json"])
        self.assertEqual(receipt["provisioning"]["status"], "blocked")
        self.assertIn("package-lock.json is missing", receipt["provisioning"]["problems"][0])

    def test_an_existing_branch_is_refused_before_treehouse_is_called(self):
        git(self.repo, "branch", "story-ws-1")
        with self.assertRaisesRegex(wt.AdapterError, "already exists"):
            self.acquire()
        self.assertEqual(self.calls(), [])
        self.assertEqual(list((self.root / "workspaces").glob("*.json")), [])

    def test_cli_exit_code_signals_a_blocked_workspace(self):
        self.mode("hook-fail")
        code, out, _ = self.cli(
            "acquire",
            "--run-id",
            "fixture-run-abc123",
            "--workstream",
            "ws-1",
            "--repo",
            str(self.repo),
            "--base",
            self.base,
            "--branch",
            "story-ws-1",
            "--readiness-cmd",
            json.dumps(READY),
        )
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["provisioning"]["status"], "blocked")


class UncertainAcquisitionTests(AdapterCase):
    def test_unparseable_output_is_uncertain_and_blocks_a_blind_retry(self):
        self.mode("garbage")
        receipt = self.acquire()
        self.assertEqual(receipt["provisioning"]["status"], "uncertain")
        self.mode("ok")
        with self.assertRaisesRegex(wt.AdapterError, "already has a 'uncertain' workspace entry"):
            self.acquire()
        self.assertEqual(len(self.calls()), 1)

    def test_a_nonzero_exit_is_uncertain(self):
        self.mode("exit1")
        receipt = self.acquire()
        self.assertEqual(receipt["provisioning"]["status"], "uncertain")
        self.assertIn("may or may not exist", receipt["provisioning"]["problems"][0])

    def test_a_timeout_is_uncertain(self):
        self.mode("timeout")
        receipt = self.acquire(get_timeout=1)
        self.assertEqual(receipt["provisioning"]["status"], "uncertain")
        self.assertIn("timed out", receipt["provisioning"]["problems"][0])

    def test_reconciling_no_lease_permits_a_fresh_acquire(self):
        self.mode("garbage")
        self.acquire()
        wt.reconcile(
            self.root,
            run_id="fixture-run-abc123",
            ws_id="ws-1",
            path=None,
            evidence="treehouse status lists no lease for the holder",
        )
        self.mode("ok")
        self.assertEqual(self.acquire()["provisioning"]["status"], "ready")

    def test_reconciling_a_found_path_records_it_as_blocked(self):
        self.mode("exit1")
        self.acquire()
        found = self.tmp.base / "found"
        git(self.repo, "worktree", "add", "--detach", str(found), "HEAD")
        entry = wt.reconcile(
            self.root,
            run_id="fixture-run-abc123",
            ws_id="ws-1",
            path=found,
            evidence="treehouse status row for the holder",
        )
        self.assertEqual(entry["state"], "blocked")
        self.assertEqual(entry["receipt"]["path"], str(found.resolve()))
        with self.assertRaisesRegex(wt.AdapterError, "only to an uncertain"):
            wt.reconcile(self.root, run_id="fixture-run-abc123", ws_id="ws-1", path=None, evidence="again")


class ReleaseCheckTests(AdapterCase):
    def leased(self) -> tuple[dict, Path]:
        receipt = self.acquire()
        self.assertEqual(receipt["provisioning"]["status"], "ready")
        return receipt, Path(receipt["path"])

    def commit(self, path: Path, name: str = "work.txt") -> str:
        (path / name).write_text("work\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "story work")
        return git(path, "rev-parse", "HEAD")

    def check(self, receipt: dict, **kwargs) -> dict:
        args = dict(target_ref="main", retained_ref=None, owned_processes=[], valuable_ignored=[])
        args.update(kwargs)
        return wt.check_release(receipt, **args)

    def test_uncommitted_and_untracked_work_is_never_releasable(self):
        receipt, path = self.leased()
        (path / "draft.txt").write_text("unsaved\n")
        report = self.check(receipt)
        self.assertFalse(report["releasable"])
        self.assertIn("would be destroyed", report["problems"][0])

    def test_an_unlanded_head_is_not_releasable(self):
        receipt, path = self.leased()
        self.commit(path)
        report = self.check(receipt)
        self.assertFalse(report["releasable"])
        self.assertIn("squash merge needs the original head", report["problems"][0])

    def test_a_landed_head_is_releasable(self):
        receipt, path = self.leased()
        head = self.commit(path)
        git(self.repo, "merge", "-q", "--ff-only", head)
        report = self.check(receipt)
        self.assertTrue(report["releasable"], report["problems"])
        self.assertEqual(report["checks"]["retained_by"], "ancestor of main")

    def test_a_squash_merged_head_needs_a_retained_ref(self):
        receipt, path = self.leased()
        head = self.commit(path)
        git(self.repo, "merge", "-q", "--squash", head)
        git(self.repo, "commit", "-q", "-m", "squashed story")
        self.assertFalse(self.check(receipt)["releasable"])
        git(self.repo, "update-ref", "refs/retained/story-ws-1", head)
        report = self.check(receipt, retained_ref="refs/retained/story-ws-1")
        self.assertTrue(report["releasable"], report["problems"])

    def test_the_workspaces_own_branch_or_head_never_proves_retention(self):
        receipt, path = self.leased()
        self.commit(path)
        for kwargs in ({"target_ref": "story-ws-1"}, {"target_ref": "HEAD"}, {"retained_ref": "story-ws-1"}):
            with self.subTest(kwargs):
                report = self.check(receipt, **kwargs)
                self.assertFalse(report["releasable"])
        git(self.repo, "branch", "elsewhere", git(path, "rev-parse", "HEAD"))
        report = self.check(receipt, retained_ref="elsewhere")
        self.assertFalse(report["releasable"])
        self.assertIn("is a local branch", " ".join(report["problems"]))

    def test_valuable_ignored_artifacts_block_release(self):
        receipt, path = self.leased()
        git(self.repo, "merge", "-q", "--ff-only", self.commit(path))
        (path / "node_modules").mkdir()
        (path / "node_modules" / "dep.js").write_text("x")
        self.assertTrue(self.check(receipt, valuable_ignored=["coverage*"])["releasable"])
        with open(path / ".gitignore", "a") as f:
            f.write("coverage-report/\n")
        git(path, "commit", "-q", "-am", "ignore coverage")
        git(self.repo, "merge", "-q", "--ff-only", git(path, "rev-parse", "HEAD"))
        (path / "coverage-report").mkdir()
        (path / "coverage-report" / "index.html").write_text("report")
        report = self.check(receipt, valuable_ignored=["coverage*"])
        self.assertFalse(report["releasable"])
        self.assertIn("valuable ignored artifact", report["problems"][0])

    def test_a_valuable_file_inside_an_ignored_directory_is_found(self):
        receipt, path = self.leased()
        git(self.repo, "merge", "-q", "--ff-only", self.commit(path))
        (path / "node_modules" / "pkg").mkdir(parents=True)
        (path / "node_modules" / "pkg" / "patched.js").write_text("local patch")
        report = self.check(receipt, valuable_ignored=["node_modules/pkg/*.js"])
        self.assertFalse(report["releasable"])
        self.assertEqual(report["checks"]["valuable_ignored"], ["node_modules/pkg/patched.js"])

    def test_a_live_owned_process_blocks_release_and_a_reused_pid_does_not(self):
        receipt, path = self.leased()
        git(self.repo, "merge", "-q", "--ff-only", self.commit(path))
        proc = subprocess.Popen(["sleep", "30"], cwd=path)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        identity = wt.process_identity(proc.pid)
        report = self.check(receipt, owned_processes=[identity])
        self.assertFalse(report["releasable"])
        self.assertEqual(report["checks"]["live_owned_processes"], [proc.pid])
        reused = self.check(receipt, owned_processes=[{**identity, "started": "Thu Jan  1 00:00:00 1970"}])
        self.assertTrue(reused["releasable"], reused["problems"])

    def test_process_identity_is_stable_across_time_zones_and_locales(self):
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        captured = wt.process_identity(proc.pid)
        for env in ({"TZ": "Asia/Tokyo"}, {"LC_ALL": "tr_TR.UTF-8", "LANG": "tr_TR.UTF-8"}):
            with mock.patch.dict(os.environ, env):
                self.assertEqual(wt.process_identity(proc.pid), captured)

    def test_an_unnormalized_or_unprobeable_identity_blocks_release(self):
        receipt, path = self.leased()
        git(self.repo, "merge", "-q", "--ff-only", self.commit(path))
        legacy = self.check(receipt, owned_processes=[{"pid": 999999, "started": "Thu Jan  1 00:00:00 1970"}])
        self.assertFalse(legacy["releasable"])
        self.assertIn("identity format", " ".join(legacy["problems"]))
        normalized = {"pid": 999999, "started": "x", "identity_format": wt.PROCESS_IDENTITY_FORMAT}
        with mock.patch.object(
            wt.process_identity_module, "process_started", side_effect=wt.ProcessProbeError("ps exited 2: boom")
        ):
            unknown = self.check(receipt, owned_processes=[normalized])
        self.assertFalse(unknown["releasable"])
        self.assertEqual(unknown["checks"]["unknown_owned_processes"], [999999])


class ReleaseTests(AdapterCase):
    """The adapter returns a workspace only through the ledger's authorized path."""

    def prepared(self, authority=None) -> tuple[Ledger, dict]:
        led = Ledger(self.root, profile=EXAMPLE_PROFILE, authority=authority)
        led.add(kind="ship")
        receipt = self.acquire(ws="ws-1", run_id=led.run_id)
        path = Path(receipt["path"])
        led.assign("ws-1", "worker-1", "--base-sha", self.base)
        led.receipt("ws-1", **receipt)
        led.status("ws-1", "working")
        (path / "work.txt").write_text("work\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "story work")
        head = git(path, "rev-parse", "HEAD")
        led.complete("ws-1", head=head, base=self.base)
        git(self.repo, "merge", "-q", "--ff-only", head)
        return led, receipt

    def mark_eligible(self, led: Ledger, receipt: dict) -> None:
        head = git(Path(receipt["path"]), "rev-parse", "HEAD")
        led.do(
            "update-workstream",
            "--id",
            "ws-1",
            "--cleanup-disposition",
            "eligible",
            "--cleanup-evidence",
            clean_evidence(receipt["path"], head=head),
        )

    def release(self, led: Ledger, session: str = "coordinator-1") -> dict:
        return wt.release(
            self.root,
            run_id=led.run_id,
            ws_id="ws-1",
            session_id=session,
            target_ref="main",
            retained_ref=None,
            owned_processes=[],
            valuable_ignored=[],
        )

    def test_release_requires_an_eligible_disposition_in_the_ledger(self):
        led, receipt = self.prepared()
        with self.assertRaisesRegex(wt.AdapterError, "disposition is None"):
            self.release(led)
        self.assertNotIn("return", [c["argv"][0] for c in self.calls()])

    def test_cleanup_eligibility_needs_a_terminal_workstream(self):
        led = Ledger(self.root, profile=EXAMPLE_PROFILE)
        led.add(kind="ship")
        receipt = self.acquire(ws="ws-1", run_id=led.run_id)
        led.assign("ws-1", "worker-1", "--base-sha", self.base)
        led.receipt("ws-1", **receipt)
        led.status("ws-1", "working")
        err = led.refuse(
            "update-workstream",
            "--id",
            "ws-1",
            "--cleanup-disposition",
            "eligible",
            "--cleanup-evidence",
            clean_evidence(receipt["path"], head=self.base),
        )
        self.assertIn("only after the workstream is terminal", err)

    def test_release_requires_the_cleanup_grant_and_the_claim_holder(self):
        led, receipt = self.prepared(authority=all_authority(cleanup=False))
        self.mark_eligible(led, receipt)
        with self.assertRaisesRegex(wt.AdapterError, "withholds cleanup"):
            self.release(led)
        with self.assertRaisesRegex(wt.AdapterError, "does not hold"):
            self.release(led, session="intruder")

    def test_an_authorized_verified_release_returns_without_force(self):
        led, receipt = self.prepared()
        self.mark_eligible(led, receipt)
        entry = self.release(led)
        self.assertEqual(entry["state"], "returned")
        returns = [c["argv"] for c in self.calls() if c["argv"][0] == "return"]
        self.assertEqual(returns, [["return", receipt["path"]]])
        with self.assertRaisesRegex(wt.AdapterError, "already returned"):
            self.release(led)

    def test_new_work_after_the_evidence_keeps_the_workspace(self):
        led, receipt = self.prepared()
        self.mark_eligible(led, receipt)
        (Path(receipt["path"]) / "late.txt").write_text("written after the check\n")
        with self.assertRaisesRegex(wt.AdapterError, "workspace retained"):
            self.release(led)
        self.assertNotIn("return", [c["argv"][0] for c in self.calls()])

    def test_a_failed_return_is_recorded_as_uncertain(self):
        led, receipt = self.prepared()
        self.mark_eligible(led, receipt)
        self.mode("return-fail")
        with self.assertRaisesRegex(wt.AdapterError, "lease state is uncertain"):
            self.release(led)
        journal = json.loads((self.root / "workspaces" / f"{led.run_id}__ws-1.json").read_text())
        self.assertEqual(journal["state"], "return-failed")
        self.mode("ok")
        with self.assertRaisesRegex(wt.AdapterError, "reconcile current ownership"):
            self.release(led)
        self.assertEqual(len([c for c in self.calls() if c["argv"][0] == "return"]), 1)
        entry = wt.reconcile_return(
            self.root, run_id=led.run_id, ws_id="ws-1", found="still-leased", evidence="status row still leased"
        )
        self.assertEqual(entry["state"], "blocked")
        self.assertEqual(self.release(led)["state"], "returned")

    def test_an_interrupted_return_is_never_retried_blindly(self):
        led, receipt = self.prepared()
        self.mark_eligible(led, receipt)
        journal = self.root / "workspaces" / f"{led.run_id}__ws-1.json"
        entry = json.loads(journal.read_text())
        entry["state"] = "returning"
        journal.write_text(json.dumps(entry))
        with self.assertRaisesRegex(wt.AdapterError, "reconcile current ownership"):
            self.release(led)
        self.assertNotIn("return", [c["argv"][0] for c in self.calls()])


if __name__ == "__main__":
    unittest.main()
