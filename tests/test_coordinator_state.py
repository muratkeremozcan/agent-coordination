"""Ledger tests through the public command interface, each against its own temporary root.

The cases that matter are the ones where a wrong answer looks like a right one: a child
dispatched before its dependency is verified, a completion with no evidence behind it, a
late reply from a replaced owner advancing the work, a stale coordinator overwriting the
holder, cleanup evidence from another decade, a batch that partially applies, or a corrupt
record silently "fixed" by an overwrite.
"""

import fcntl
import json
import os
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import _guard  # noqa: F401  (must load first: it installs the real-state guard)
from helpers import (
    DEFAULT_PROFILE,
    EXAMPLE_PROFILE,
    SESSION,
    SHA_A,
    SHA_B,
    SHA_C,
    Ledger,
    TempRoot,
    all_authority,
    clean_evidence,
    cli,
    cs,
    fails,
    iso,
    ok,
    verification,
)

VERIFY = ["--verification", verification()]


WAIT = ["--wait-on", "ci", "--clears-when", "checks report", "--source", "gh pr checks", "--next-check-at", "+30m"]


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TempRoot(self)
        self.root = self.tmp.root

    def ledger(self, **kwargs) -> Ledger:
        return Ledger(self.root, **kwargs)

    def dispatched_scout(self, led: Ledger) -> str:
        ws = led.add()["workstreams"][-1]["id"]
        led.assign(ws)
        led.status(ws, "working")
        return ws


class CreationTests(LedgerCase):
    def test_start_creates_an_active_run_claimed_by_its_creator(self):
        record = self.ledger().snapshot()
        self.assertEqual(record["schema_version"], 3)
        self.assertEqual(record["status"], "active")
        self.assertEqual(record["revision"], 1)
        self.assertEqual(record["claimed_by"]["session_id"], SESSION)
        self.assertEqual(record["claimed_by"]["generation"], 1)
        self.assertEqual(len(record["profile_digest"]), 64)
        self.assertEqual([g["action"] for g in record["authority"]["grants"]], list(cs.AUTHORITY_ACTIONS))

    def test_run_id_collision_retries_to_a_free_id(self):
        with mock.patch.object(cs.secrets, "token_hex", side_effect=["aaaaaa", "aaaaaa", "bbbbbb"] + ["cccccccc"] * 10):
            first = self.ledger()
            second = self.ledger()
        self.assertTrue(first.run_id.endswith("-aaaaaa"))
        self.assertTrue(second.run_id.endswith("-bbbbbb"))

    def test_run_id_allocation_fails_loud_when_exhausted(self):
        self.ledger()
        existing = next((self.root / "runs").glob("*.json")).stem.rsplit("-", 1)[1]
        with mock.patch.object(cs.secrets, "token_hex", side_effect=lambda n: existing if n == 3 else "dddddddd"):
            err = fails(
                self.root,
                [
                    "start",
                    "--title",
                    "Fixture run",
                    "--intent",
                    "x",
                    "--coordination-reason",
                    "x",
                    "--delivery-authority",
                    "x",
                    "--authority",
                    json.dumps(all_authority()),
                    "--profile",
                    str(DEFAULT_PROFILE),
                    "--session-id",
                    SESSION,
                    "--coordinator-runtime",
                    "codex",
                ],
            )
        self.assertIn("could not allocate a unique run id", err)

    def test_start_requires_every_action_explicitly_granted_or_withheld(self):
        partial = all_authority()[:-1]
        err = fails(
            self.root,
            [
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
                json.dumps(partial),
                "--profile",
                str(DEFAULT_PROFILE),
                "--session-id",
                SESSION,
                "--coordinator-runtime",
                "codex",
            ],
        )
        self.assertIn("missing ['cleanup']", err)
        self.assertFalse(self.root.exists())

    def test_start_refuses_an_invalid_profile_before_writing(self):
        bad = self.tmp.base / "bad.toml"
        bad.write_text(DEFAULT_PROFILE.read_text().replace('state_writer = "coordinator"', 'state_writer = "workers"'))
        err = fails(
            self.root,
            [
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
                str(bad),
                "--session-id",
                SESSION,
                "--coordinator-runtime",
                "codex",
            ],
            code=2,
        )
        self.assertIn("[topology].state_writer", err)
        self.assertFalse(self.root.exists())


class WorkstreamTests(LedgerCase):
    def test_full_scout_lifecycle_records_result_and_coordinator_verification(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        record = led.complete(ws, base=None)
        w = record["workstreams"][0]
        self.assertEqual(w["status"], "completed")
        self.assertEqual(w["results"][0]["status"], "accepted")
        self.assertEqual(w["verification"][0]["command"], "workspace_treehouse.py check-coauthors")
        self.assertEqual(w["dispatch_attempts"], 1)

    def test_update_with_no_changes_is_rejected(self):
        led = self.ledger()
        ws = led.add()["workstreams"][0]["id"]
        self.assertIn("requires at least one change", led.refuse("update-workstream", "--id", ws))

    def test_permitted_operations_can_only_narrow_the_run_grants(self):
        led = self.ledger(authority=all_authority(merge=False))
        err = led.refuse(
            "add-workstream",
            "--title",
            "t",
            "--kind",
            "ship",
            "--repository",
            "r",
            "--scope",
            "s",
            "--brief",
            "b",
            "--acceptance-criteria",
            '["x"]',
            "--delivery-authority",
            "ship and merge",
            "--permitted-operations",
            '["merge"]',
        )
        self.assertIn("exceed the run's granted authority", err)

    def test_stack_parent_must_be_a_declared_dependency(self):
        led = self.ledger()
        led.add(kind="ship")
        led.add(kind="ship")
        err = led.refuse(
            "add-workstream",
            "--title",
            "t",
            "--kind",
            "ship",
            "--repository",
            "r",
            "--scope",
            "s",
            "--brief",
            "b",
            "--acceptance-criteria",
            '["x"]',
            "--delivery-authority",
            "d",
            "--permitted-operations",
            "[]",
            "--dependencies",
            '["ws-1"]',
            "--stack-parent",
            "ws-2",
        )
        self.assertIn("must also be listed in dependencies", err)

    def test_forward_dependency_reference_fails(self):
        led = self.ledger()
        err = led.refuse(
            "add-workstream",
            "--title",
            "t",
            "--kind",
            "scout",
            "--repository",
            "r",
            "--scope",
            "s",
            "--brief",
            "b",
            "--acceptance-criteria",
            '["x"]',
            "--delivery-authority",
            "d",
            "--permitted-operations",
            "[]",
            "--dependencies",
            '["ws-9"]',
        )
        self.assertIn("unknown dependency workstream id(s): ['ws-9']", err)

    def test_reassigning_without_worktree_flags_preserves_the_worktree_and_bumps_generation(self):
        led = self.ledger()
        led.add(kind="ship")
        led.ship_ready("ws-1")
        led.assign("ws-1", "worker-2")
        w = led.ws("ws-1")
        self.assertEqual(w["worktree"], "/fixture/worktrees/ws-1")
        self.assertEqual(w["assignment_generation"], 2)
        self.assertEqual(w["assignment_history"][0]["owner_id"], "worker-1")


class TransitionTests(LedgerCase):
    def test_invalid_transition_is_rejected(self):
        led = self.ledger()
        led.add()
        self.assertIn(
            "cannot move from 'queued' to 'completed'",
            led.refuse("update-workstream", "--id", "ws-1", "--status", "completed"),
        )

    def test_working_cannot_skip_review_ready_even_with_verification(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        led.result(ws)
        err = led.refuse("update-workstream", "--id", ws, "--status", "completed", *VERIFY)
        self.assertIn("cannot move from 'working' to 'completed'", err)

    def test_completed_refuses_rewritten_status_or_deliverable_but_takes_a_correction(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        led.complete(ws, base=None)
        self.assertIn("terminal", led.refuse("update-workstream", "--id", ws, "--deliverable", '{"x": 1}'))
        record = led.do("update-workstream", "--id", ws, "--correct-field", "summary", "--correct-note", "typo")
        self.assertEqual(record["workstreams"][0]["corrections"][0]["note"], "typo")

    def test_review_ready_can_fail_or_return_to_the_same_owner(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        led.result(ws, base_sha=None)
        led.status(ws, "review-ready")
        led.status(ws, "working")
        w = led.ws(ws)
        self.assertEqual((w["owner_id"], w["assignment_generation"], w["dispatch_attempts"]), ("worker-1", 1, 2))
        self.assertIn(
            "needs an accepted completed result",
            led.refuse("update-workstream", "--id", ws, "--status", "review-ready"),
        )
        led.status(ws, "failed")

    def test_untouched_workstreams_fail_only_with_a_reason(self):
        led = self.ledger()
        led.add()
        self.assertIn(
            "requires a blocker reason", led.refuse("update-workstream", "--id", "ws-1", "--status", "failed")
        )
        led.status("ws-1", "failed", "--blocker", "superseded by ws-2")

    def test_blocked_requires_a_reason_and_can_fail_directly(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        self.assertIn("requires a blocker reason", led.refuse("update-workstream", "--id", ws, "--status", "blocked"))
        led.status(ws, "blocked", "--blocker", "waiting on credentials")
        led.status(ws, "failed")

    def test_a_string_deliverable_is_refused_by_name(self):
        led = self.ledger()
        led.add()
        err = led.refuse("update-workstream", "--id", "ws-1", "--deliverable", '"a string"')
        self.assertIn("each entry in 'deliverables' must be a JSON object", err)

    def test_repeating_the_current_status_is_an_idempotent_no_op(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        before = led.snapshot()["revision"]
        led.status(ws, "working")
        self.assertEqual(led.snapshot()["revision"], before)


class DependencyTests(LedgerCase):
    """CHECK-1 from the original investigation, now driven through the CLI."""

    def test_child_cannot_start_while_its_dependency_is_queued(self):
        led = self.ledger()
        led.add()
        led.add(deps=["ws-1"])
        led.assign("ws-2")
        err = led.refuse("update-workstream", "--id", "ws-2", "--status", "working")
        self.assertIn("dependency workstream(s) ['ws-1'] are not verified complete", err)
        self.assertEqual(led.ws("ws-2")["status"], "queued")

    def test_child_cannot_start_while_its_dependency_is_only_review_ready(self):
        led = self.ledger()
        ws1 = self.dispatched_scout(led)
        led.result(ws1, base_sha=None)
        led.status(ws1, "review-ready")
        led.add(deps=[ws1])
        led.assign("ws-2")
        self.assertIn("not verified complete", led.refuse("update-workstream", "--id", "ws-2", "--status", "working"))
        led.status(ws1, "completed", *VERIFY)
        led.status("ws-2", "working")

    def test_stacked_child_starts_from_the_parents_verified_head_before_parent_completes(self):
        led = self.ledger()
        led.add(kind="ship")
        led.ship_ready("ws-1")
        led.status("ws-1", "working")
        led.add(kind="ship", deps=["ws-1"], stack_parent="ws-1")
        led.ship_ready("ws-2", base=SHA_B)
        self.assertIn(
            "has not produced a verified result commit",
            led.refuse("update-workstream", "--id", "ws-2", "--status", "working"),
        )
        led.result("ws-1", head_sha=SHA_B)
        led.status("ws-1", "review-ready")
        led.status("ws-2", "working")

    def test_stacked_child_cannot_start_from_a_parent_head_whose_checks_failed(self):
        led = self.ledger()
        led.add(kind="ship")
        led.ship_ready("ws-1")
        led.status("ws-1", "working")
        led.result("ws-1", head_sha=SHA_B, verification=[{"command": "make test", "exit_code": 1}], artifacts=[])
        led.status("ws-1", "review-ready")
        led.add(kind="ship", deps=["ws-1"], stack_parent="ws-1")
        led.ship_ready("ws-2", base=SHA_B)
        self.assertIn(
            "has not produced a verified result commit",
            led.refuse("update-workstream", "--id", "ws-2", "--status", "working"),
        )

    def test_a_ship_base_comes_from_the_receipt_and_completion_always_checks_it(self):
        led = self.ledger()
        led.add(kind="ship")
        led.assign("ws-1", "worker-1", "--worktree", "/fixture/worktrees/ws-1", "--branch", "story-ws-1")
        led.receipt("ws-1")
        self.assertEqual(led.ws("ws-1")["base_sha"], SHA_A)
        led.status("ws-1", "working")
        led.result("ws-1", base_sha=None)
        self.assertIn(
            "is not the current base",
            led.refuse("update-workstream", "--id", "ws-1", "--status", "review-ready"),
        )

    def test_stacked_child_must_be_assigned_the_parents_head_as_base(self):
        led = self.ledger()
        led.add(kind="ship")
        led.ship_ready("ws-1")
        led.status("ws-1", "working")
        led.result("ws-1", head_sha=SHA_B)
        led.status("ws-1", "review-ready")
        led.add(kind="ship", deps=["ws-1"], stack_parent="ws-1")
        led.ship_ready("ws-2", base=SHA_C)
        self.assertIn("restack onto that head", led.refuse("update-workstream", "--id", "ws-2", "--status", "working"))

    def test_dispatch_requires_an_assigned_owner(self):
        led = self.ledger()
        led.add()
        self.assertIn("no assigned owner", led.refuse("update-workstream", "--id", "ws-1", "--status", "working"))


class CompletionEvidenceTests(LedgerCase):
    """CHECK-2 from the original investigation, now driven through the CLI."""

    def ready(self, led: Ledger, **result_fields) -> str:
        ws = self.dispatched_scout(led)
        led.result(ws, **{"base_sha": None, **result_fields})
        led.status(ws, "review-ready")
        return ws

    def test_review_ready_needs_a_substantive_result(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        self.assertIn(
            "idle or done is a lifecycle signal",
            led.refuse("update-workstream", "--id", ws, "--status", "review-ready"),
        )

    def test_completion_refuses_empty_artifacts_and_verification(self):
        led = self.ledger()
        ws = self.ready(led, artifacts=[], verification=[])
        err = led.refuse("update-workstream", "--id", ws, "--status", "completed", *VERIFY)
        self.assertIn("no artifacts", err)
        self.assertIn("no verification commands", err)
        self.assertEqual(led.ws(ws)["status"], "review-ready")

    def test_completion_refuses_a_failing_verification_command(self):
        led = self.ledger()
        ws = self.ready(led, verification=[{"command": "make test", "exit_code": 2}])
        self.assertIn(
            "exited nonzero: ['make test']",
            led.refuse("update-workstream", "--id", ws, "--status", "completed", *VERIFY),
        )

    def test_ship_completion_needs_the_tested_head_on_the_assigned_base(self):
        led = self.ledger()
        led.add(kind="ship")
        led.ship_ready("ws-1")
        led.status("ws-1", "working")
        led.result("ws-1", head_sha=None, base_sha=SHA_C)
        err = led.refuse("update-workstream", "--id", "ws-1", "--status", "review-ready")
        self.assertIn(f"is not the current base '{SHA_A}'", err)
        led.issue("ws-1", "req-2", "follow-up")
        led.result("ws-1", "req-2", issue=False, head_sha=None, base_sha=SHA_A)
        led.status("ws-1", "review-ready")
        err = led.refuse("update-workstream", "--id", "ws-1", "--status", "completed", *VERIFY)
        self.assertIn("no tested head_sha", err)

    def test_required_review_must_pass_and_findings_block(self):
        led = self.ledger()
        ws = self.ready(led, review=[{"reviewer": "independent", "status": "findings"}])
        err = led.refuse("update-workstream", "--id", ws, "--status", "completed", *VERIFY)
        self.assertIn("required reviewer 'independent' is 'findings'", err)

    def test_unavailable_reviewer_is_accepted_only_when_the_profile_allows_it(self):
        review = [{"reviewer": "independent", "status": "passed"}, {"reviewer": "coderabbit", "status": "unavailable"}]
        strict = self.ledger()
        ws = self.ready(strict, review=review)
        self.assertIn(
            "'coderabbit' is 'unavailable'",
            strict.refuse("update-workstream", "--id", ws, "--status", "completed", *VERIFY),
        )
        lenient = Ledger(TempRoot(self).root, profile=EXAMPLE_PROFILE)
        ws = self.ready(lenient, review=review)
        lenient.status(ws, "completed", *VERIFY)

    def test_coordinator_verification_must_pass_on_the_result_head(self):
        led = self.ledger()
        led.add(kind="ship")
        led.ship_ready("ws-1")
        led.status("ws-1", "working")
        led.result("ws-1", head_sha=SHA_B)
        led.status("ws-1", "review-ready")
        for bad, expected in (
            ("{}", "needs the command that was run"),
            (verification(exit_code=1), "did not exit 0"),
            (verification(head=SHA_C), "not the result head"),
        ):
            with self.subTest(bad):
                self.assertIn(
                    expected,
                    led.refuse("update-workstream", "--id", "ws-1", "--status", "completed", "--verification", bad),
                )
        led.status("ws-1", "completed", "--verification", verification())

    def test_completion_needs_the_coordinators_own_verification(self):
        led = self.ledger()
        ws = self.ready(led)
        self.assertIn(
            "coordinator's own verification", led.refuse("update-workstream", "--id", ws, "--status", "completed")
        )


class ResultCorrelationTests(LedgerCase):
    HANDOVER = ["--handover-reason", "owner hit its usage limit", "--quiescence-evidence", "pane closed; branch pushed"]

    def test_a_reply_to_a_replaced_owners_request_is_retained_but_cannot_advance(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        led.issue(ws, "req-old")
        led.assign(ws, "worker-2", *self.HANDOVER)
        led.result(ws, "req-old", issue=False, generation=1, base_sha=None)
        w = led.ws(ws)
        self.assertEqual(w["results"][0]["status"], "stale")
        self.assertIn("belongs to generation 1", w["results"][0]["rejected_reason"])
        self.assertIn(
            "needs an accepted completed result",
            led.refuse("update-workstream", "--id", ws, "--status", "review-ready"),
        )

    def test_a_delayed_reply_from_a_previous_attempt_cannot_complete_the_repair(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        led.issue(ws, "req-unused")
        led.result(ws, "req-1", base_sha=None)
        led.status(ws, "review-ready")
        led.status(ws, "working")
        led.result(ws, "req-unused", issue=False, base_sha=None, head_sha=SHA_C)
        late = led.ws(ws)["results"][-1]
        self.assertEqual(late["status"], "stale")
        self.assertIn("attempt 1; current is generation 1 attempt 2", late["rejected_reason"])
        self.assertIn(
            "needs an accepted completed result",
            led.refuse("update-workstream", "--id", ws, "--status", "review-ready"),
        )

    def test_replies_to_unissued_requests_or_with_a_mismatched_generation_are_stale(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        led.result(ws, "req-ghost", issue=False, base_sha=None)
        led.issue(ws, "req-1")
        led.result(ws, "req-1", issue=False, generation=7, base_sha=None)
        reasons = [r["rejected_reason"] for r in led.ws(ws)["results"]]
        self.assertIn("was never issued", reasons[0])
        self.assertIn("claims generation 7", reasons[1])

    def test_requests_go_only_to_a_dispatched_owner(self):
        led = self.ledger()
        led.add()
        self.assertIn(
            "issued only to a dispatched owner",
            led.refuse("issue-request", "--id", "ws-1", "--request-id", "r", "--purpose", "brief"),
        )

    def test_a_later_accepted_failure_supersedes_an_older_completion(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        led.result(ws, "req-1", base_sha=None)
        led.issue(ws, "req-2", "follow-up")
        led.result(ws, "req-2", issue=False, outcome="failed", base_sha=None)
        self.assertIn(
            "needs an accepted completed result",
            led.refuse("update-workstream", "--id", ws, "--status", "review-ready"),
        )

    def test_a_newer_request_supersedes_an_older_completion_until_it_is_answered(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        led.result(ws, "req-1", base_sha=None)
        led.issue(ws, "req-2", "follow-up")
        self.assertIn(
            "needs an accepted completed result",
            led.refuse("update-workstream", "--id", ws, "--status", "review-ready"),
        )
        led.result(ws, "req-2", issue=False, base_sha=None, summary="follow-up done")
        led.status(ws, "review-ready")
        led.status(ws, "completed", "--verification", verification(None))
        self.assertEqual(led.ws(ws)["status"], "completed")

    def recovery_chain(self, led: Ledger) -> str:
        ws = self.dispatched_scout(led)
        led.issue(ws, "req-1")
        led.do("issue-request", "--id", ws, "--request-id", "req-1r", "--purpose", "report-recovery", "--root", "req-1")
        return ws

    def test_an_identical_recovery_reply_is_a_duplicate_of_the_root_outcome(self):
        led = self.ledger()
        ws = self.recovery_chain(led)
        led.result(ws, "req-1", issue=False, base_sha=None)
        led.result(ws, "req-1r", issue=False, base_sha=None)
        self.assertEqual([r["status"] for r in led.ws(ws)["results"]], ["accepted", "duplicate"])
        led.status(ws, "review-ready")

    def test_conflicting_root_and_recovery_replies_block_until_reconciled(self):
        led = self.ledger()
        ws = self.recovery_chain(led)
        led.result(ws, "req-1", issue=False, base_sha=None, outcome="failed")
        led.result(ws, "req-1r", issue=False, base_sha=None)
        self.assertEqual(led.ws(ws)["results"][-1]["status"], "conflict")
        self.assertIn("conflicting replies", led.refuse("update-workstream", "--id", ws, "--status", "review-ready"))
        success = led.ws(ws)["results"][-1]["normalized_digest"]
        led.do(
            "reconcile-result",
            "--id",
            ws,
            "--request-id",
            "req-1",
            "--choose",
            success,
            "--evidence",
            "re-read the pane",
        )
        led.status(ws, "review-ready")

    def test_a_recovery_reply_cannot_clear_a_newer_failure(self):
        led = self.ledger()
        ws = self.recovery_chain(led)
        led.result(ws, "req-1", issue=False, base_sha=None)
        led.issue(ws, "req-2", "repair")
        led.result(ws, "req-2", issue=False, base_sha=None, outcome="failed")
        led.result(ws, "req-1r", issue=False, base_sha=None)
        self.assertIn(
            "needs an accepted completed result",
            led.refuse("update-workstream", "--id", ws, "--status", "review-ready"),
        )

    def test_report_recovery_is_spent_once_per_root(self):
        led = self.ledger()
        self.recovery_chain(led)
        err = led.refuse(
            "issue-request", "--id", "ws-1", "--request-id", "req-1s", "--purpose", "report-recovery", "--root", "req-1"
        )
        self.assertIn("one report recovery", err)

    def test_an_unknown_request_purpose_is_refused(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        self.assertIn(
            "purpose must be one of", led.refuse("issue-request", "--id", ws, "--request-id", "r", "--purpose", "chat")
        )

    def test_an_identical_result_retry_is_idempotent(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        led.result(ws, "req-1")
        revision = led.snapshot()["revision"]
        led.result(ws, "req-1")
        self.assertEqual(led.snapshot()["revision"], revision)
        self.assertEqual(len(led.ws(ws)["results"]), 1)

    def test_a_different_reply_to_the_same_request_is_kept_as_a_conflict(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        led.result(ws, "req-1", base_sha=None)
        led.result(ws, "req-1", issue=False, base_sha=None, outcome="failed")
        self.assertEqual([r["status"] for r in led.ws(ws)["results"]], ["accepted", "conflict"])
        self.assertIn("conflicting replies", led.refuse("update-workstream", "--id", ws, "--status", "review-ready"))

    def test_a_late_result_after_completion_is_kept_as_stale_evidence(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        led.issue(ws, "req-late")
        led.complete(ws, base=None)
        led.result(ws, "req-late", issue=False, base_sha=None)
        late = led.ws(ws)["results"][-1]
        self.assertEqual(late["status"], "stale")
        self.assertEqual(led.ws(ws)["status"], "completed")

    def test_replacing_an_active_owner_needs_a_recorded_quiescent_handover(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        self.assertIn(
            "keeps its assignment",
            led.refuse(
                "assign",
                "--id",
                ws,
                "--binding",
                "herdr-codex",
                "--owner-type",
                "collaborator",
                "--owner-id",
                "worker-1",
            ),
        )
        self.assertIn(
            "'handover_reason' is required",
            led.refuse(
                "assign", "--id", ws, "--binding", "herdr-codex", "--owner-type", "collaborator", "--owner-id", "w2"
            ),
        )
        led.assign(ws, "worker-2", *self.HANDOVER)
        history = led.ws(ws)["assignment_history"][0]
        self.assertEqual(history["handover"]["quiescence_evidence"], "pane closed; branch pushed")


class ClaimTests(LedgerCase):
    def test_a_second_session_cannot_claim_or_mutate_a_held_run(self):
        led = self.ledger()
        led.add()
        self.assertIn("already claimed", led.refuse("claim", session="coordinator-2"))
        self.assertIn(
            "a stale coordinator must not write",
            led.refuse("update-workstream", "--id", "ws-1", "--blocker", "x", session="coordinator-2"),
        )

    def test_claiming_again_as_the_holder_is_idempotent(self):
        led = self.ledger()
        revision = led.snapshot()["revision"]
        led.do("claim")
        self.assertEqual(led.snapshot()["revision"], revision)

    def test_controlled_handover_through_release(self):
        led = self.ledger()
        led.do("release-claim", "--reason", "handing over at story boundary")
        record = led.do("claim", session="coordinator-2")
        self.assertEqual(record["claimed_by"], {**record["claimed_by"], "session_id": "coordinator-2", "generation": 2})
        self.assertEqual([e["event"] for e in record["claim_history"]], ["claim", "release", "claim"])
        self.assertIn(
            "does not hold",
            led.refuse(
                "add-workstream",
                "--title",
                "t",
                "--kind",
                "scout",
                "--repository",
                "r",
                "--scope",
                "s",
                "--brief",
                "b",
                "--acceptance-criteria",
                '["x"]',
                "--delivery-authority",
                "d",
                "--permitted-operations",
                "[]",
            ),
        )

    def test_forced_takeover_needs_expected_holder_reason_and_liveness_evidence(self):
        led = self.ledger()
        self.assertIn("'expected_holder' is required", led.refuse("claim", "--force", session="coordinator-2"))
        self.assertIn(
            "re-read the run",
            led.refuse(
                "claim",
                "--force",
                "--expected-holder",
                "someone-else",
                "--reason",
                "r",
                "--liveness-evidence",
                "e",
                session="coordinator-2",
            ),
        )
        self.assertIn(
            "'liveness_evidence' is required",
            led.refuse("claim", "--force", "--expected-holder", SESSION, "--reason", "r", session="coordinator-2"),
        )
        record = led.do(
            "claim",
            "--force",
            "--expected-holder",
            SESSION,
            "--reason",
            "holder pane closed",
            "--liveness-evidence",
            "herdr agent get returned not found",
            session="coordinator-2",
        )
        event = record["claim_history"][-1]
        self.assertEqual((event["event"], event["previous"], event["generation"]), ("forced-handover", SESSION, 2))


class WorkspaceTests(LedgerCase):
    def test_ship_dispatch_requires_a_ready_receipt_on_the_pinned_base(self):
        led = self.ledger()
        led.add(kind="ship")
        led.assign(
            "ws-1", "worker-1", "--worktree", "/fixture/worktrees/ws-1", "--branch", "story-ws-1", "--base-sha", SHA_A
        )
        self.assertIn(
            "without a workspace receipt", led.refuse("update-workstream", "--id", "ws-1", "--status", "working")
        )
        led.receipt("ws-1", provisioning={"status": "blocked", "problems": ["readiness exited 1"]})
        self.assertIn(
            "provisioning is 'blocked'", led.refuse("update-workstream", "--id", "ws-1", "--status", "working")
        )
        led.receipt("ws-1", observed_head_sha=SHA_C)
        self.assertIn(
            "does not match its pinned base", led.refuse("update-workstream", "--id", "ws-1", "--status", "working")
        )
        led.receipt("ws-1")
        led.status("ws-1", "working")

    def test_receipt_must_name_this_runs_lease_holder_and_assigned_path(self):
        led = self.ledger()
        led.add(kind="ship")
        led.assign("ws-1", "worker-1", "--worktree", "/fixture/worktrees/ws-1")
        from helpers import receipt_payload

        bad_holder = receipt_payload(led.run_id, "ws-1", lease_holder="other-run/ws-1")
        self.assertIn("is not", led.refuse("record-workspace", "--id", "ws-1", "--receipt", json.dumps(bad_holder)))
        bad_path = receipt_payload(led.run_id, "ws-1", path="/elsewhere")
        self.assertIn(
            "not the assigned worktree",
            led.refuse("record-workspace", "--id", "ws-1", "--receipt", json.dumps(bad_path)),
        )

    def test_concurrent_writers_never_share_a_workspace(self):
        led = self.ledger()
        led.add(kind="ship")
        led.add(kind="ship")
        led.ship_ready("ws-1", worktree="/fixture/shared")
        err = led.refuse(
            "assign",
            "--id",
            "ws-2",
            "--binding",
            "herdr-codex",
            "--owner-type",
            "collaborator",
            "--owner-id",
            "worker-2",
            "--worktree",
            "/fixture/shared",
        )
        self.assertIn("concurrent writers need distinct workspaces", err)

    def test_a_scout_may_read_a_ship_worktree(self):
        led = self.ledger()
        led.add(kind="ship")
        led.add(kind="scout")
        led.ship_ready("ws-1", worktree="/fixture/shared")
        led.assign("ws-2", "reviewer-1", "--worktree", "/fixture/shared")

    def test_workstreams_profile_forbids_lane_reuse(self):
        led = self.ledger()
        led.add(kind="ship")
        led.ship_ready("ws-1", worktree="/fixture/lane")
        led.status("ws-1", "working")
        led.complete("ws-1")
        led.do(
            "update-workstream",
            "--id",
            "ws-1",
            "--cleanup-disposition",
            "eligible",
            "--cleanup-evidence",
            clean_evidence("/fixture/lane"),
        )
        led.add(kind="ship")
        err = led.refuse(
            "assign",
            "--id",
            "ws-2",
            "--binding",
            "herdr-codex",
            "--owner-type",
            "collaborator",
            "--owner-id",
            "worker-1",
            "--worktree",
            "/fixture/lane",
        )
        self.assertIn("forbids lane reuse", err)

    def test_lanes_profile_reuses_a_checkout_only_after_verified_clean_retention(self):
        led = self.ledger(profile=EXAMPLE_PROFILE)
        led.add(kind="ship")
        led.ship_ready("ws-1", worktree="/fixture/lane")
        led.status("ws-1", "working")
        led.complete("ws-1")
        led.do("update-workstream", "--id", "ws-1", "--cleanup-disposition", "retained")
        led.add(kind="ship")
        assign = [
            "--id",
            "ws-2",
            "--binding",
            "herdr-codex",
            "--owner-type",
            "collaborator",
            "--owner-id",
            "worker-1",
            "--worktree",
            "/fixture/lane",
        ]
        self.assertIn("verified clean and retained", led.refuse("assign", *assign))
        led.do(
            "update-workstream",
            "--id",
            "ws-1",
            "--cleanup-disposition",
            "eligible",
            "--cleanup-evidence",
            clean_evidence("/fixture/lane"),
        )
        led.do("assign", *assign)


class CleanupEvidenceTests(LedgerCase):
    """CHECK-3 from the original investigation, plus binding to the workstream."""

    def completed_ship(self) -> Ledger:
        led = self.ledger()
        led.add(kind="ship")
        led.ship_ready("ws-1")
        led.status("ws-1", "working")
        led.complete("ws-1")
        return led

    def eligible(self, led: Ledger, evidence: str) -> str:
        return led.refuse(
            "update-workstream", "--id", "ws-1", "--cleanup-disposition", "eligible", "--cleanup-evidence", evidence
        )

    def test_evidence_dated_2000_is_refused(self):
        led = self.completed_ship()
        self.assertIn(
            "older than",
            self.eligible(led, clean_evidence("/fixture/worktrees/ws-1", checked_at="2000-01-01T00:00:00+00:00")),
        )

    def test_timestamps_need_a_timezone_and_cannot_be_in_the_future(self):
        led = self.completed_ship()
        self.assertIn(
            "timezone", self.eligible(led, clean_evidence("/fixture/worktrees/ws-1", checked_at="2026-01-01T00:00:00"))
        )
        self.assertIn(
            "in the future",
            self.eligible(led, clean_evidence("/fixture/worktrees/ws-1", checked_at=iso(timedelta(hours=2)))),
        )

    def test_evidence_must_predate_nothing_it_claims_to_cover(self):
        led = self.completed_ship()
        self.assertIn(
            "predates",
            self.eligible(led, clean_evidence("/fixture/worktrees/ws-1", checked_at=iso(timedelta(minutes=-1)))),
        )

    def test_truthy_strings_and_missing_detail_are_refused(self):
        led = self.completed_ship()
        err = self.eligible(led, clean_evidence("/fixture/worktrees/ws-1", clean_worktree="true", detail=""))
        self.assertIn("clean_worktree must be exactly true", err)
        self.assertIn("detail must name", err)

    def test_evidence_must_name_this_worktree_and_verified_head(self):
        led = self.completed_ship()
        err = self.eligible(led, clean_evidence("/fixture/other", head=SHA_C))
        self.assertIn("is not '/fixture/worktrees/ws-1'", err)
        self.assertIn("is not the verified head", err)

    def test_fresh_bound_evidence_is_accepted(self):
        led = self.completed_ship()
        led.do(
            "update-workstream",
            "--id",
            "ws-1",
            "--cleanup-disposition",
            "eligible",
            "--cleanup-evidence",
            clean_evidence("/fixture/worktrees/ws-1"),
        )
        self.assertEqual(led.ws("ws-1")["cleanup_disposition"], "eligible")


class LandingAndDenialTests(LedgerCase):
    LANDING = {
        "executor": "coordinator",
        "method": "merge",
        "target_ref": "main",
        "merged_sha": SHA_C,
        "evidence": "gh pr view: MERGED",
    }

    def completed(self, authority=None, permitted=("local-edit", "push", "pr-create", "merge")) -> Ledger:
        led = self.ledger(authority=authority)
        pr = {"push", "pr-create"} <= set(permitted)
        extra = ("--deliverable", "pr", "--forge", "none") if pr else ()
        led.add("ship", None, None, list(permitted), "Workstream", *extra)
        led.ship_ready("ws-1")
        led.status("ws-1", "working")
        led.complete("ws-1")
        return led

    def test_landing_requires_verified_completion(self):
        led = self.ledger()
        led.add(kind="ship", permitted=["merge"])
        self.assertIn(
            "only after verified completion",
            led.refuse("record-landing", "--id", "ws-1", "--landing", json.dumps(self.LANDING)),
        )

    def test_landing_requires_run_grant_and_workstream_permission(self):
        withheld = self.completed(authority=all_authority(merge=False), permitted=("local-edit",))
        self.assertIn(
            "withholds merge", withheld.refuse("record-landing", "--id", "ws-1", "--landing", json.dumps(self.LANDING))
        )
        narrow = Ledger(TempRoot(self).root)
        narrow.add(kind="ship", permitted=["local-edit"])
        narrow.ship_ready("ws-1")
        narrow.status("ws-1", "working")
        narrow.complete("ws-1")
        self.assertIn(
            "not permitted to merge",
            narrow.refuse("record-landing", "--id", "ws-1", "--landing", json.dumps(self.LANDING)),
        )

    def test_an_open_denial_blocks_landing_until_reconciled(self):
        led = self.completed()
        led.do(
            "record-denial",
            "--action",
            "merge",
            "--workstream",
            "ws-1",
            "--executor",
            "worker-1",
            "--command",
            "gh pr merge 1 --squash",
            "--denial",
            "Merge Without Review",
            "--executed",
            "unknown",
        )
        self.assertIn(
            "another session must not run it",
            led.refuse("record-landing", "--id", "ws-1", "--landing", json.dumps(self.LANDING)),
        )
        led.do(
            "resolve-denial",
            "--id",
            "den-1",
            "--resolution",
            "user approved the merge in the host prompt",
            "--evidence",
            "gh pr view shows not merged",
            "--executed",
            "no",
        )
        led.do("record-landing", "--id", "ws-1", "--landing", json.dumps(self.LANDING))

    def test_profile_reserves_merges_for_the_coordinator(self):
        led = self.completed()
        landing = {**self.LANDING, "executor": "worker-1"}
        self.assertIn(
            "reserves merges for the coordinator",
            led.refuse("record-landing", "--id", "ws-1", "--landing", json.dumps(landing)),
        )

    def test_a_squash_landing_must_name_the_retained_original_head(self):
        led = self.completed()
        squash = {**self.LANDING, "method": "squash"}
        self.assertIn(
            "'retained_ref' is required", led.refuse("record-landing", "--id", "ws-1", "--landing", json.dumps(squash))
        )
        for local in ("story-ws-1", "refs/heads/backup", "HEAD"):
            self.assertIn(
                "local branch the workspace reset can lose",
                led.refuse(
                    "record-landing", "--id", "ws-1", "--landing", json.dumps({**squash, "retained_ref": local})
                ),
            )
        led.do(
            "record-landing",
            "--id",
            "ws-1",
            "--landing",
            json.dumps({**squash, "retained_ref": "refs/remotes/origin/story-ws-1"}),
        )

    def test_close_refuses_unreconciled_denials(self):
        led = self.ledger()
        led.do(
            "record-denial",
            "--action",
            "push",
            "--executor",
            "worker-1",
            "--command",
            "git push",
            "--denial",
            "blocked",
            "--executed",
            "no",
        )
        self.assertIn("denial(s) not reconciled: ['den-1']", led.refuse("close", "--outcome", "done"))


class BudgetTests(LedgerCase):
    def test_active_workstreams_are_capped_by_the_profile(self):
        led = self.ledger()
        for _ in range(4):
            led.add()
        for ws in ("ws-1", "ws-2", "ws-3"):
            led.assign(ws)
            led.status(ws, "working")
        led.assign("ws-4")
        self.assertIn("profile allows 3", led.refuse("update-workstream", "--id", "ws-4", "--status", "working"))

    def test_dispatch_attempts_are_bounded(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        for n in range(2):
            led.result(ws, f"req-{n}", base_sha=None)
            led.status(ws, "review-ready")
            led.status(ws, "working")
        led.result(ws, "req-last", base_sha=None)
        led.status(ws, "review-ready")
        self.assertIn("open a decision before", led.refuse("update-workstream", "--id", ws, "--status", "working"))

    def test_same_owner_wait_and_resume_keeps_the_attempt(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        for _ in range(5):
            led.status(ws, "waiting", *WAIT)
            led.status(ws, "working")
        self.assertEqual(led.ws(ws)["dispatch_attempts"], 1)

    def test_replacing_owners_through_blocked_spends_attempts_until_exhausted(self):
        led = self.ledger()
        ws = self.dispatched_scout(led)
        handover = ["--handover-reason", "owner stopped", "--quiescence-evidence", "pane closed"]
        for n in (2, 3):
            led.status(ws, "blocked", "--blocker", "owner unresponsive")
            led.assign(ws, f"worker-{n}", *handover)
            led.status(ws, "working")
        self.assertEqual(led.ws(ws)["dispatch_attempts"], 3)
        led.status(ws, "blocked", "--blocker", "owner unresponsive")
        self.assertIn(
            "before replacing its owner again",
            led.refuse(
                "assign",
                "--id",
                ws,
                "--binding",
                "herdr-codex",
                "--owner-type",
                "collaborator",
                "--owner-id",
                "worker-4",
                *handover,
            ),
        )


class DecisionAndClosureTests(LedgerCase):
    def test_open_decision_blocks_close_until_resolved(self):
        led = self.ledger()
        led.do("open-decision", "--question", "Ship behind a flag?")
        self.assertIn("decision(s) still open", led.refuse("close", "--outcome", "done"))
        led.do(
            "resolve-decision",
            "--id",
            "dec-1",
            "--status",
            "resolved",
            "--resolution",
            "yes",
            "--evidence",
            "user said so",
            "--provenance",
            "user-words",
            "--user-words",
            "yes, ship it",
        )
        self.assertEqual(led.do("close", "--outcome", "done")["status"], "completed")

    def test_close_refuses_unfinished_workstreams(self):
        led = self.ledger()
        led.add()
        self.assertIn("not terminal: ['ws-1']", led.refuse("close", "--outcome", "done"))

    def test_close_requires_failed_workstreams_acknowledged_as_whole_tokens(self):
        led = self.ledger()
        for _ in range(10):
            led.add()
        led.status("ws-1", "failed", "--blocker", "cancelled")
        for n in range(2, 11):
            led.status(f"ws-{n}", "failed", "--blocker", "cancelled")
        outcome = "cancelled " + ", ".join(f"ws-{n}" for n in range(2, 11))
        self.assertIn("['ws-1']", led.refuse("close", "--outcome", outcome))
        led.do("close", "--outcome", outcome + " and ws-1")

    def test_close_requires_a_cleanup_disposition_for_ship_worktrees(self):
        led = self.ledger()
        led.add(kind="ship")
        led.ship_ready("ws-1")
        led.status("ws-1", "failed", "--blocker", "dropped")
        self.assertIn("missing a cleanup disposition", led.refuse("close", "--outcome", "ws-1 dropped"))
        led.do("update-workstream", "--id", "ws-1", "--cleanup-disposition", "retained")
        led.do("close", "--outcome", "ws-1 dropped")

    def test_mentions_id_is_token_exact(self):
        self.assertFalse(cs._mentions_id("ws-10 failed", "ws-1"))
        self.assertTrue(cs._mentions_id("ws-1 failed", "ws-1"))


class AbandonmentTests(LedgerCase):
    def test_abandon_requires_a_reason_and_stays_inspectable(self):
        led = self.ledger()
        self.assertIn("'reason' is required", led.refuse("abandon", "--reason", " "))
        record = led.do("abandon", "--reason", "superseded")
        self.assertEqual((record["status"], record["outcome"]), ("abandoned", "superseded"))

    def test_abandon_recovers_a_corrupt_record_and_preserves_the_raw_bytes(self):
        led = self.ledger()
        path = self.root / "runs" / f"{led.run_id}.json"
        path.write_text("{not json")
        record = led.do("abandon", "--reason", "corrupt")
        self.assertTrue(record["recovered_from_corruption"])
        quarantined = list((self.root / "runs").glob(f"{led.run_id}.json.corrupt-*"))
        self.assertEqual(quarantined[0].read_text(), "{not json")


class RecordFileSafetyTests(LedgerCase):
    def test_a_quarantine_symlink_cannot_redirect_the_corrupt_copy(self):
        led = self.ledger()
        path = self.root / "runs" / f"{led.run_id}.json"
        path.write_text("{corrupt")
        victim = self.tmp.base / "victim.txt"
        victim.write_text("keep me")
        with mock.patch.object(cs.time, "time", return_value=1234):
            (self.root / "runs" / f"{led.run_id}.json.corrupt-1234").symlink_to(victim)
            err = led.refuse("abandon", "--reason", "corrupt")
        self.assertIn("is a symlink", err)
        self.assertEqual(victim.read_text(), "keep me")
        self.assertEqual(path.read_text(), "{corrupt")

    def test_a_record_symlink_cannot_route_one_run_id_to_another_run(self):
        target = self.ledger()
        (self.root / "runs" / "wrong-run-000000.json").symlink_to(self.root / "runs" / f"{target.run_id}.json")
        err = fails(
            self.root, ["open-decision", "--run-id", "wrong-run-000000", "--session-id", SESSION, "--question", "q"]
        )
        self.assertIn("is a symlink", err)
        self.assertEqual(target.snapshot()["decisions"], [])

    def test_a_record_missing_a_workstream_key_is_refused_cleanly(self):
        led = self.ledger()
        led.add()
        path = self.root / "runs" / f"{led.run_id}.json"
        record = json.loads(path.read_text())
        del record["workstreams"][0]["requests"]
        path.write_text(json.dumps(record))
        before = path.read_bytes()
        self.assertIn("quarantined", led.refuse("open-decision", "--question", "q"))
        self.assertEqual(path.read_bytes(), before)


class DurabilityTests(LedgerCase):
    def test_backup_rotates_and_allows_one_step_recovery(self):
        led = self.ledger()
        led.add()
        backup = json.loads((self.root / "runs" / f"{led.run_id}.json.bak").read_text())
        self.assertEqual((backup["revision"], backup["workstreams"]), (1, []))

    def test_revision_mismatch_rejects_and_preserves_state(self):
        led = self.ledger()
        led.add()
        err = led.refuse("open-decision", "--question", "q", "--base-revision", "1")
        self.assertIn("base revision 1 does not match current revision 2", err)
        self.assertEqual(led.snapshot()["revision"], 2)

    def test_lock_timeout_raises_a_clear_error_and_writes_nothing(self):
        led = self.ledger()
        path = self.root / "runs" / f"{led.run_id}.json"
        fd = os.open(path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with mock.patch.object(cs, "LOCK_TIMEOUT_SECONDS", 0.2):
                self.assertIn("could not acquire", led.refuse("open-decision", "--question", "q"))
        finally:
            os.close(fd)
        self.assertEqual(led.snapshot()["revision"], 1)

    def test_fd_matches_path_detects_a_replacement(self):
        led = self.ledger()
        path = self.root / "runs" / f"{led.run_id}.json"
        fd = os.open(path, os.O_RDONLY)
        try:
            self.assertTrue(cs._fd_matches_path(fd, path))
            cs._atomic_write_text(path, path.read_text())
            self.assertFalse(cs._fd_matches_path(fd, path))
        finally:
            os.close(fd)


class BatchTests(LedgerCase):
    def write_ops(self, led: Ledger, ops: list[dict], base_revision: int, session: str = SESSION) -> list[str]:
        ops_file = self.tmp.base / "ops.json"
        ops_file.write_text(
            json.dumps({"run_id": led.run_id, "session_id": session, "base_revision": base_revision, "ops": ops})
        )
        return ["apply", "--ops-file", str(ops_file)]

    ADD = {
        "op": "add-workstream",
        "title": "t",
        "kind": "scout",
        "repository": "r",
        "scope": "s",
        "brief": "b",
        "acceptance_criteria": ["x"],
        "delivery_authority": "d",
        "permitted_operations": [],
    }

    def test_batch_commits_all_ops_atomically_as_one_revision(self):
        led = self.ledger()
        record = ok(
            self.root, self.write_ops(led, [self.ADD, self.ADD, {"op": "set-next-action", "text": "dispatch"}], 1)
        )
        self.assertEqual((record["revision"], len(record["workstreams"]), record["next_action"]), (2, 2, "dispatch"))

    def test_batch_aborts_completely_on_a_failing_op(self):
        led = self.ledger()
        fails(
            self.root,
            self.write_ops(led, [self.ADD, {"op": "update-workstream", "id": "ws-9", "status": "working"}], 1),
        )
        self.assertEqual(led.snapshot()["workstreams"], [])

    def test_batch_rejects_lifecycle_ops_stale_revisions_and_non_holders(self):
        led = self.ledger()
        self.assertIn(
            "not permitted inside a batch", fails(self.root, self.write_ops(led, [{"op": "close", "outcome": "x"}], 1))
        )
        self.assertIn("does not match", fails(self.root, self.write_ops(led, [self.ADD], 7)))
        self.assertIn("does not hold", fails(self.root, self.write_ops(led, [self.ADD], 1, session="intruder")))

    def test_batch_never_touches_a_different_run(self):
        first, second = self.ledger(), self.ledger()
        ok(self.root, self.write_ops(first, [self.ADD], 1))
        self.assertEqual(second.snapshot()["revision"], 1)


class SnapshotAndListTests(LedgerCase):
    def test_snapshot_is_byte_deterministic_across_repeated_reads(self):
        led = self.ledger()
        _, a, _ = cli(self.root, ["snapshot", "--run-id", led.run_id])
        _, b, _ = cli(self.root, ["snapshot", "--run-id", led.run_id])
        self.assertEqual(a, b)

    def test_list_reports_corrupt_and_shapeless_records_without_crashing(self):
        led = self.ledger()
        (self.root / "runs" / "broken-000000.json").write_text("{")
        (self.root / "runs" / "shapeless-000000.json").write_text(json.dumps({"schema_version": 3}))
        statuses = {s["run_id"]: s["status"] for s in ok(self.root, ["list"])}
        self.assertEqual(statuses, {led.run_id: "active", "broken-000000": "corrupt", "shapeless-000000": "corrupt"})

    def test_mutating_a_shapeless_record_fails_clean(self):
        (self.root / "runs").mkdir(parents=True)
        (self.root / "runs" / "shapeless-000000.json").write_text(json.dumps({"schema_version": 3}))
        err = fails(
            self.root, ["open-decision", "--run-id", "shapeless-000000", "--session-id", SESSION, "--question", "q"]
        )
        self.assertIn("quarantined", err)

    def test_listing_an_empty_root_creates_nothing(self):
        self.assertEqual(ok(self.root, ["list"]), [])
        self.assertFalse(self.root.exists())

    def test_run_ids_cannot_escape_the_runs_directory(self):
        for bad in ("../../etc/passwd", "/tmp/target", "UPPER"):
            self.assertIn("invalid run id", fails(self.root, ["snapshot", "--run-id", bad]))


class SchemaVersionTests(LedgerCase):
    def test_an_unknown_schema_version_is_refused_and_left_untouched(self):
        led = self.ledger()
        path = self.root / "runs" / f"{led.run_id}.json"
        record = json.loads(path.read_text())
        record["schema_version"] = 4
        path.write_text(json.dumps(record))
        before = path.read_bytes()
        self.assertIn("schema_version 4", led.refuse("open-decision", "--question", "q"))
        self.assertIn("schema_version 4", led.refuse("abandon", "--reason", "r"))
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / "runs" / f"{led.run_id}.json.bak").exists())

    def test_a_v1_record_placed_in_a_v2_root_is_refused(self):
        v1 = json.loads((Path(__file__).parent / "fixtures" / "v1-run.json").read_text())
        (self.root / "runs").mkdir(parents=True)
        path = self.root / "runs" / f"{v1['run_id']}.json"
        path.write_text(json.dumps(v1))
        err = fails(
            self.root, ["open-decision", "--run-id", v1["run_id"], "--session-id", "v1-session", "--question", "q"]
        )
        self.assertIn("schema_version 1", err)


class ImportV1Tests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.fixture = Path(__file__).parent / "fixtures" / "v1-run.json"
        self.copy = self.tmp.base / "v1-copy.json"
        self.copy.write_bytes(self.fixture.read_bytes())

    def import_args(self, source: Path) -> list[str]:
        return ["import-v1", "--source", str(source), "--profile", str(DEFAULT_PROFILE), "--session-id", SESSION]

    def test_import_converts_a_fixture_copy_without_touching_it(self):
        before = (self.copy.read_bytes(), self.copy.stat().st_mtime_ns)
        record = ok(self.root, self.import_args(self.copy))
        self.assertEqual((self.copy.read_bytes(), self.copy.stat().st_mtime_ns), before)
        self.assertEqual(record["schema_version"], 3)
        self.assertEqual(record["imported_from"]["schema_version"], 1)
        self.assertTrue(all(g["granted"] is False for g in record["authority"]["grants"]))
        ws = record["workstreams"][0]
        self.assertEqual((ws["assignment_generation"], ws["results"], ws["status"]), (1, [], "working"))
        self.assertEqual(record["claimed_by"]["session_id"], SESSION)
        self.assertEqual(record["imported_from"]["source_claimed_by"]["session_id"], "v1-session")
        resolved = ok(
            self.root,
            [
                "resolve-decision",
                "--run-id",
                record["run_id"],
                "--session-id",
                SESSION,
                "--id",
                "dec-1",
                "--status",
                "resolved",
                "--resolution",
                "main",
                "--evidence",
                "user said so",
                "--provenance",
                "user-words",
                "--user-words",
                "use main",
            ],
        )
        self.assertEqual(resolved["decisions"][0]["status"], "resolved")

    def test_import_refuses_to_overwrite_an_existing_run(self):
        ok(self.root, self.import_args(self.copy))
        self.assertIn("refusing to overwrite", fails(self.root, self.import_args(self.copy)))

    def test_import_refuses_non_v1_sources(self):
        record = json.loads(self.copy.read_text())
        record["schema_version"] = 2
        self.copy.write_text(json.dumps(record))
        self.assertIn("reads schema_version 1 only", fails(self.root, self.import_args(self.copy)))

    def test_import_refuses_a_source_inside_a_protected_location(self):
        live = (
            Path(os.environ["HOME"])
            / "personal-agent"
            / "_bmad"
            / "memory"
            / "personal-agent"
            / ".runtime"
            / "coordinator"
            / "runs"
        )
        live.mkdir(parents=True, exist_ok=True)
        source = live / "active-run.json"
        source.write_bytes(self.fixture.read_bytes())
        self.assertIn("protected location", fails(self.root, self.import_args(source), code=2))
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
