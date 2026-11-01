"""Schema v3 core: attempt admission, base advance, restacking, decision gates and migration.

Each class drives the public CLI against a temporary state root. The rebase and restack
cases build real Git history so the recorded evidence comes from commands that actually ran.
"""

import json
import subprocess
import unittest
from pathlib import Path

import _guard  # noqa: F401  (must load first: it installs the real-state guard)
from helpers import (
    DEFAULT_PROFILE,
    REPO_ROOT,
    SESSION,
    SHA_A,
    SHA_B,
    Ledger,
    TempRoot,
    fails,
    git,
    make_repo,
    ok,
    result_payload,
    verification,
)

FIXTURES = REPO_ROOT / "tests" / "fixtures"
HANDOVER = ["--handover-reason", "owner stopped answering", "--quiescence-evidence", "pane closed, no child process"]
WAIT = ["--wait-on", "ci", "--clears-when", "checks report", "--source", "gh pr checks", "--next-check-at", "+30m"]


class V3Case(unittest.TestCase):
    def setUp(self):
        self.tmp = TempRoot(self)
        self.root = self.tmp.root

    def profile(self, **limits: int) -> Path:
        text = DEFAULT_PROFILE.read_text()
        for key, value in limits.items():
            text = text.replace(f"{key} = ", f"{key} = {value}\n# was ", 1)
        path = self.tmp.base / "profile.toml"
        path.write_text(text)
        return path

    def scout(self, led: Ledger) -> str:
        ws = led.add()["workstreams"][-1]["id"]
        led.assign(ws)
        led.status(ws, "working")
        return ws

    def replace(self, led: Ledger, ws: str, owner: str) -> dict:
        return led.do(
            "assign",
            "--id",
            ws,
            "--binding",
            "herdr-codex",
            "--owner-type",
            "collaborator",
            "--owner-id",
            owner,
            *HANDOVER,
        )


class AttemptAdmissionTests(V3Case):
    def test_an_admitted_attempt_resumes_after_a_wait_at_the_cap(self):
        led = Ledger(self.root, profile=self.profile(max_dispatch_attempts=1))
        ws = self.scout(led)
        led.status(ws, "waiting", *WAIT)
        led.status(ws, "working")
        self.assertEqual(led.ws(ws)["dispatch_attempts"], 1)

    def test_a_replacement_from_blocked_spends_exactly_one_attempt(self):
        led = Ledger(self.root, profile=self.profile(max_dispatch_attempts=2))
        ws = self.scout(led)
        led.status(ws, "blocked", "--blocker", "owner silent")
        self.replace(led, ws, "worker-2")
        self.assertEqual(led.ws(ws)["dispatch_attempts"], 1)
        self.assertIn(
            "awaiting admission", led.refuse("issue-request", "--id", ws, "--request-id", "r", "--purpose", "brief")
        )
        led.status(ws, "working")
        self.assertEqual(led.ws(ws)["dispatch_attempts"], 2)

    def test_a_replacement_from_review_ready_spends_exactly_one_attempt(self):
        led = Ledger(self.root, profile=self.profile(max_dispatch_attempts=2))
        ws = self.scout(led)
        led.result(ws, base_sha=None)
        led.status(ws, "review-ready")
        self.replace(led, ws, "worker-2")
        led.status(ws, "working")
        self.assertEqual(led.ws(ws)["dispatch_attempts"], 2)

    def test_a_replacement_of_running_work_is_admitted_at_assign(self):
        led = Ledger(self.root, profile=self.profile(max_dispatch_attempts=2))
        ws = self.scout(led)
        self.replace(led, ws, "worker-2")
        self.assertEqual(led.ws(ws)["dispatch_attempts"], 2)
        self.assertFalse(led.ws(ws)["pending_admission"])

    def test_a_replacement_past_the_cap_fails_and_writes_nothing(self):
        led = Ledger(self.root, profile=self.profile(max_dispatch_attempts=1))
        ws = self.scout(led)
        led.status(ws, "blocked", "--blocker", "owner silent")
        before = led.snapshot()
        err = led.refuse(
            "assign",
            "--id",
            ws,
            "--binding",
            "herdr-codex",
            "--owner-type",
            "collaborator",
            "--owner-id",
            "worker-2",
            *HANDOVER,
        )
        self.assertIn("before replacing its owner again", err)
        self.assertEqual(led.snapshot(), before)


class GitHistory:
    """A repository with base A, a story head on A, and main moved on to C."""

    def __init__(self, base: Path):
        self.repo = base / "repo"
        self.a = make_repo(self.repo)
        git(self.repo, "switch", "-q", "-c", "story")
        (self.repo / "story.txt").write_text("story\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "story")
        git(self.repo, "switch", "-q", "main")
        (self.repo / "main.txt").write_text("main moved\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "main moved")
        self.c = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", "story")
        git(self.repo, "rebase", "-q", "main")
        self.h = git(self.repo, "rev-parse", "HEAD")

    def check(self, asserts: str, subject: str, obj: str) -> dict:
        """Run the relation for real (`obj` is an ancestor of `subject`) and record it as evidence."""
        argv = ["git", "-C", str(self.repo), "merge-base", "--is-ancestor", obj, subject]
        code = subprocess.run(argv, capture_output=True).returncode
        return {"command": " ".join(argv), "exit_code": code, "asserts": asserts, "subject": subject, "object": obj}


class BaseAdvanceTests(V3Case):
    def setUp(self):
        super().setUp()
        self.history = GitHistory(self.tmp.base)

    def ship(self, led: Ledger) -> str:
        led.add(kind="ship")
        led.ship_ready("ws-1", base=self.history.a)
        led.status("ws-1", "working")
        return "ws-1"

    def advance(self, led: Ledger) -> dict:
        h = self.history
        evidence = [h.check("ancestry", h.c, h.a), h.check("contains", h.h, h.c)]
        return led.do(
            "advance-base",
            "--id",
            "ws-1",
            "--old-base",
            h.a,
            "--new-base",
            h.c,
            "--workspace-head",
            h.h,
            "--evidence",
            json.dumps(evidence),
            "--reason",
            "main moved and the owner rebased",
        )

    def reply(self, led: Ledger, request_id: str, **fields) -> None:
        fields = {"head_sha": self.history.h, "base_sha": self.history.c, **fields}
        led.do("record-result", "--id", "ws-1", "--result", json.dumps(result_payload(request_id, **fields)))

    def test_the_rebase_sequence_completes_and_an_early_rebased_result_never_governs(self):
        led = Ledger(self.root)
        ws = self.ship(led)
        led.issue(ws, "req-1")
        self.reply(led, "req-1")
        self.assertIn("target", led.refuse("update-workstream", "--id", ws, "--status", "review-ready"))
        self.advance(led)
        self.assertIn("target", led.refuse("update-workstream", "--id", ws, "--status", "review-ready"))
        led.issue(ws, "req-2", "rebase")
        self.assertEqual(led.ws(ws)["requests"][-1]["target_base_sha"], self.history.c)
        self.reply(led, "req-2")
        led.status(ws, "review-ready")
        led.status(ws, "completed", "--verification", verification(self.history.h))
        self.assertEqual(led.ws(ws)["verified_head"], self.history.h)

    def test_redispatch_and_wait_resume_after_an_advance_pass_readiness(self):
        led = Ledger(self.root)
        ws = self.ship(led)
        self.advance(led)
        led.status(ws, "waiting", *WAIT)
        led.status(ws, "working")
        led.issue(ws, "req-2", "rebase")
        self.reply(led, "req-2")
        led.status(ws, "review-ready")
        led.status(ws, "working")
        self.assertEqual(led.ws(ws)["dispatch_attempts"], 2)

    def test_an_advance_needs_evidence_for_both_relations(self):
        led = Ledger(self.root)
        self.ship(led)
        h = self.history
        err = led.refuse(
            "advance-base",
            "--id",
            "ws-1",
            "--old-base",
            h.a,
            "--new-base",
            h.c,
            "--workspace-head",
            h.h,
            "--evidence",
            json.dumps([h.check("contains", h.h, h.c)]),
            "--reason",
            "r",
        )
        self.assertIn("missing assertion(s) ['ancestry']", err)
        failing = {**h.check("ancestry", h.c, h.a), "exit_code": 1}
        err = led.refuse(
            "advance-base",
            "--id",
            "ws-1",
            "--old-base",
            h.a,
            "--new-base",
            h.c,
            "--workspace-head",
            h.h,
            "--evidence",
            json.dumps([failing, h.check("contains", h.h, h.c)]),
            "--reason",
            "r",
        )
        self.assertIn("did not exit 0", err)

    def test_a_request_cannot_target_a_base_other_than_the_current_one(self):
        led = Ledger(self.root)
        self.ship(led)
        err = led.refuse(
            "issue-request",
            "--id",
            "ws-1",
            "--request-id",
            "r",
            "--purpose",
            "rebase",
            "--target-base-sha",
            self.history.c,
        )
        self.assertIn("record advance-base first", err)


class RestackTests(V3Case):
    """A parent rewritten to a head that does not descend from its old one."""

    def parent_and_child(self, led: Ledger) -> None:
        led.add(kind="ship", title="parent")
        led.ship_ready("ws-1", base=SHA_A)
        led.status("ws-1", "working")
        led.result("ws-1", "req-p1", head_sha=SHA_B, base_sha=SHA_A)
        led.status("ws-1", "review-ready")
        led.add("ship", ["ws-1"], "ws-1", None, "child")
        led.ship_ready("ws-2", base=SHA_B, worktree="/fixture/worktrees/ws-2")

    def rewrite_parent(self, led: Ledger, new_head: str) -> None:
        led.status("ws-1", "working")
        led.issue("ws-1", "req-p2", "repair")
        led.result("ws-1", "req-p2", issue=False, head_sha=new_head, base_sha=SHA_A, generation=1)
        led.status("ws-1", "review-ready")

    def test_an_active_child_restacks_onto_a_non_descendant_parent_head_without_a_decision(self):
        led = Ledger(self.root)
        self.parent_and_child(led)
        led.status("ws-2", "working")
        new_head = "d" * 40
        self.rewrite_parent(led, new_head)
        led.issue("ws-2", "req-c1")
        led.do(
            "record-result",
            "--id",
            "ws-2",
            "--result",
            json.dumps(result_payload("req-c1", head_sha="e" * 40, base_sha=SHA_B)),
        )
        led.status("ws-2", "review-ready")
        err = led.refuse(
            "update-workstream", "--id", "ws-2", "--status", "completed", "--verification", verification("e" * 40)
        )
        self.assertIn("restack before completing", err)
        contains = {
            "command": "git merge-base --is-ancestor d e",
            "exit_code": 0,
            "asserts": "contains",
            "subject": "f" * 40,
            "object": new_head,
        }
        led.do(
            "restack",
            "--id",
            "ws-2",
            "--old-parent-head",
            SHA_B,
            "--new-parent-head",
            new_head,
            "--workspace-head",
            "f" * 40,
            "--evidence",
            json.dumps([contains]),
        )
        self.assertEqual(led.ws("ws-2")["base_sha"], new_head)
        self.assertEqual(led.snapshot()["decisions"], [])

    def test_a_completed_child_of_a_rewritten_parent_blocks_its_dependent_and_cannot_land(self):
        led = Ledger(self.root)
        self.parent_and_child(led)
        led.status("ws-2", "working")
        led.result("ws-2", "req-c1", head_sha="e" * 40, base_sha=SHA_B)
        led.status("ws-2", "review-ready")
        led.status("ws-2", "completed", "--verification", verification("e" * 40))
        led.add("scout", ["ws-2"], None, None, "dependent")
        led.assign("ws-3")
        led.status("ws-3", "working")
        led.status("ws-3", "blocked", "--blocker", "pause")
        self.rewrite_parent(led, "d" * 40)
        led.status("ws-3", "failed")
        led.add("scout", ["ws-2"], None, None, "late dependent")
        led.assign("ws-4")
        self.assertIn("not verified complete", led.refuse("update-workstream", "--id", "ws-4", "--status", "working"))
        landing = {
            "executor": "coordinator",
            "method": "merge",
            "target_ref": "main",
            "merged_sha": "9" * 40,
            "evidence": "x",
        }
        self.assertIn("cannot land", led.refuse("record-landing", "--id", "ws-2", "--landing", json.dumps(landing)))
        self.assertIn(
            "is terminal",
            led.refuse("restack", "--id", "ws-2", "--old-parent-head", SHA_B, "--new-parent-head", "d" * 40),
        )
        successor = led.add("ship", ["ws-1"], "ws-1", None, "child again", "--supersedes", "ws-2")["workstreams"][-1]
        self.assertEqual(led.ws("ws-2")["superseded_by"], successor["id"])
        self.assertEqual(led.ws("ws-2")["status"], "completed")


class DecisionGateTests(V3Case):
    def test_a_decision_gating_completion_blocks_only_its_workstream(self):
        led = Ledger(self.root)
        ws = self.scout(led)
        other = self.scout(led)
        led.do(
            "open-decision",
            "--question",
            "Which flag name?",
            "--affected",
            json.dumps([ws]),
            "--gates",
            '["completion"]',
        )
        led.result(ws, base_sha=None)
        self.assertIn("gate completion", led.refuse("update-workstream", "--id", ws, "--status", "review-ready"))
        led.result(other, "req-o", base_sha=None)
        led.status(other, "review-ready")

    def test_a_dispatch_gate_holds_queued_work(self):
        led = Ledger(self.root)
        ws = led.add()["workstreams"][-1]["id"]
        led.assign(ws)
        led.do(
            "open-decision", "--question", "Build it at all?", "--affected", json.dumps([ws]), "--gates", '["dispatch"]'
        )
        self.assertIn("gate dispatch", led.refuse("update-workstream", "--id", ws, "--status", "working"))

    def test_a_users_decision_is_never_resolved_tactically(self):
        led = Ledger(self.root)
        led.do("open-decision", "--question", "Ship behind a flag?")
        err = led.refuse(
            "resolve-decision",
            "--id",
            "dec-1",
            "--status",
            "resolved",
            "--resolution",
            "yes",
            "--evidence",
            "e",
            "--provenance",
            "tactical",
        )
        self.assertIn("belongs to the user", err)
        err = led.refuse(
            "resolve-decision",
            "--id",
            "dec-1",
            "--status",
            "resolved",
            "--resolution",
            "yes",
            "--evidence",
            "e",
            "--provenance",
            "user-words",
        )
        self.assertIn("user_words", err)

    def test_an_answer_request_needs_a_resolved_decision_and_then_governs(self):
        led = Ledger(self.root)
        ws = self.scout(led)
        led.result(ws, base_sha=None)
        led.do("open-decision", "--question", "Which name?", "--responsibility", "coordinator")
        self.assertIn(
            "resolve it before answering",
            led.refuse(
                "issue-request",
                "--id",
                ws,
                "--request-id",
                "ans",
                "--purpose",
                "answer",
                "--answers",
                "dec-1",
                "--body",
                "use foo",
            ),
        )
        led.do(
            "resolve-decision",
            "--id",
            "dec-1",
            "--status",
            "resolved",
            "--resolution",
            "use foo",
            "--evidence",
            "matches the existing naming",
            "--provenance",
            "tactical",
        )
        led.do(
            "issue-request",
            "--id",
            ws,
            "--request-id",
            "ans",
            "--purpose",
            "answer",
            "--answers",
            "dec-1",
            "--body",
            "use foo",
        )
        self.assertIn(
            "has no accepted terminal result", led.refuse("update-workstream", "--id", ws, "--status", "review-ready")
        )

    def test_scout_completion_needs_a_complete_decision_inventory(self):
        led = Ledger(self.root)
        ws = self.scout(led)
        led.do("open-decision", "--question", "Keep v1 API?", "--origin-workstream", ws)
        led.result(ws, base_sha=None)
        led.status(ws, "review-ready")
        err = led.refuse("update-workstream", "--id", ws, "--status", "completed", "--verification", verification(None))
        self.assertIn("omits decision(s) ['dec-1']", err)


class MigrationTests(V3Case):
    def copy(self) -> Path:
        path = self.tmp.base / "v2-copy.json"
        path.write_bytes((FIXTURES / "v2-run.json").read_bytes())
        return path

    def test_migration_writes_v3_without_touching_or_trusting_the_source(self):
        source = self.copy()
        before = source.read_bytes()
        record = ok(self.root, ["migrate-v2", "--source", str(source), "--session-id", SESSION])
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(record["schema_version"], 3)
        self.assertIsNone(record["coordinator_runtime"])
        ship, scout = record["workstreams"]
        self.assertEqual(ship["verified_head"], "b" * 40)
        self.assertEqual({r["status"] for r in ship["results"] + scout["results"]}, {"historical"})
        self.assertIsNone(record["decisions"][0]["responsibility"])

    def test_a_migrated_working_workstream_needs_a_fresh_v3_result(self):
        source = self.copy()
        record = ok(self.root, ["migrate-v2", "--source", str(source), "--session-id", SESSION])
        args = [
            "update-workstream",
            "--run-id",
            record["run_id"],
            "--session-id",
            SESSION,
            "--id",
            "ws-2",
            "--status",
            "review-ready",
        ]
        self.assertIn("no accepted terminal result", fails(self.root, args))

    def test_the_v3_reader_refuses_a_v2_record_in_place(self):
        source = self.copy()
        runs = self.root / "runs"
        runs.mkdir(parents=True)
        target = runs / "fixture-v2-run-a9e12d.json"
        target.write_bytes(source.read_bytes())
        args = ["open-decision", "--run-id", "fixture-v2-run-a9e12d", "--session-id", SESSION, "--question", "q"]
        self.assertIn("migrate-v2", fails(self.root, args))
        self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_migration_refuses_non_v2_sources_and_existing_runs(self):
        v1 = self.tmp.base / "v1.json"
        v1.write_bytes((FIXTURES / "v1-run.json").read_bytes())
        self.assertIn(
            "schema_version 2 only", fails(self.root, ["migrate-v2", "--source", str(v1), "--session-id", SESSION])
        )
        source = self.copy()
        ok(self.root, ["migrate-v2", "--source", str(source), "--session-id", SESSION])
        self.assertIn(
            "already exists", fails(self.root, ["migrate-v2", "--source", str(source), "--session-id", SESSION])
        )


if __name__ == "__main__":
    unittest.main()
