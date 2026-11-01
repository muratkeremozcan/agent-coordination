"""Paths an independent review found open: authority at every start, base changes only with
evidence, waits bound to their assignment, strict GitHub reads, compatible successors, usable
migrated profiles, runtime-matched receipts and complete check summaries."""

import json
import unittest

from test_v3_delivery import HEAD, ForgeCase, check_run, checks_page, scenario

import _guard  # noqa: F401  (must load first: it installs the real-state guard)
import coordinator_state as cs
from helpers import REPO_ROOT, SESSION, SHA_A, SHA_B, SHA_C, Ledger, TempRoot, fails, ok, result_payload, verification
from peer_sessions_receipt import normalize_launch
from session_bindings import check_session_receipt

HANDOVER = ["--handover-reason", "owner stopped", "--quiescence-evidence", "pane closed"]
FIXTURES = REPO_ROOT / "tests" / "fixtures"


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = TempRoot(self)
        self.root = self.tmp.root

    def ship(self, led: Ledger) -> str:
        ws = led.add("ship")["workstreams"][-1]["id"]
        led.ship_ready(ws)
        return ws

    def replace(self, led: Ledger, ws: str, owner: str, *extra: str) -> str:
        return led.refuse(
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
            *extra,
        )


class ExecutionAuthorityTests(Case):
    def test_revoked_edit_authority_stops_dispatch_and_resume(self):
        led = Ledger(self.root)
        ws = self.ship(led)
        led.do("amend-authority", "--action", "local-edit", "--to", "withheld", "--user-words", "stop editing")
        self.assertIn("withholds local-edit", led.refuse("update-workstream", "--id", ws, "--status", "working"))
        led.do("amend-authority", "--action", "local-edit", "--to", "granted", "--user-words", "go on")
        led.status(ws, "working")
        led.status(ws, "waiting", "--wait-on", "ci", "--clears-when", "c", "--source", "s", "--next-check-at", "+30m")
        led.do("amend-permissions", "--id", ws, "--remove", "local-edit", "--user-words", "hold that one")
        self.assertIn("not permitted local-edit", led.refuse("update-workstream", "--id", ws, "--status", "working"))

    def test_an_unresolved_edit_denial_stops_dispatch(self):
        led = Ledger(self.root)
        ws = self.ship(led)
        led.do(
            "record-denial",
            "--action",
            "local-edit",
            "--workstream",
            ws,
            "--executor",
            "owner",
            "--command",
            "edit",
            "--denial",
            "sandbox refused writes",
            "--executed",
            "no",
        )
        self.assertIn("denial", led.refuse("update-workstream", "--id", ws, "--status", "working"))

    def test_a_running_replacement_passes_dispatch_gates_atomically(self):
        led = Ledger(self.root)
        ws = led.add()["workstreams"][-1]["id"]
        led.assign(ws)
        led.status(ws, "working")
        led.do("open-decision", "--question", "Stop?", "--affected", json.dumps([ws]), "--gates", '["dispatch"]')
        before = led.ws(ws)
        self.assertIn("gate dispatch", self.replace(led, ws, "worker-2"))
        after = led.ws(ws)
        self.assertEqual(
            (after["owner_id"], after["assignment_generation"], after["dispatch_attempts"]),
            (before["owner_id"], before["assignment_generation"], before["dispatch_attempts"]),
        )

    def test_a_parked_run_admits_no_new_execution_until_unparked(self):
        led = Ledger(self.root)
        ws = self.ship(led)
        led.do("park", "--reason", "no wake path", "--resume-when", "the user says go")
        self.assertIn("is parked", led.refuse("update-workstream", "--id", ws, "--status", "working"))
        led.do("unpark", "--reason", "the user said go")
        led.status(ws, "working")


class BaseChangeTests(Case):
    def test_a_replacement_cannot_change_an_established_base(self):
        led = Ledger(self.root)
        ws = self.ship(led)
        led.status(ws, "working")
        err = self.replace(led, ws, "worker-2", "--base-sha", SHA_C)
        self.assertIn("advance-base or restack", err)
        self.assertEqual((led.ws(ws)["base_sha"], led.ws(ws)["base_history"]), (SHA_A, []))

    def test_a_queued_workstream_without_a_workspace_may_take_a_new_base(self):
        led = Ledger(self.root)
        ws = led.add("ship")["workstreams"][-1]["id"]
        led.assign(ws, "worker-1", "--base-sha", SHA_A)
        led.assign(ws, "worker-1", "--base-sha", SHA_C)
        self.assertEqual(led.ws(ws)["base_sha"], SHA_C)


class WaitBindingTests(Case):
    def waiting_event(self, led: Ledger, ws: str) -> None:
        led.issue(ws, "req-1")
        event = {
            "event_id": "old-wait",
            "request_id": "req-1",
            "generation": 1,
            "attempt": 1,
            "owner": "worker-1",
            "kind": "waiting",
            "text": "usage warning",
            "source": {"kind": "reply-correlated"},
            "wait": {"wait_on": "quota", "clears_when": "reset", "next_check_at": "+4h"},
        }
        led.do("record-event", "--id", ws, "--event", json.dumps(event))

    def test_an_old_owners_wait_cannot_be_applied_after_replacement(self):
        led = Ledger(self.root)
        ws = led.add()["workstreams"][-1]["id"]
        led.assign(ws)
        led.status(ws, "working")
        self.waiting_event(led, ws)
        led.do(
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
        err = led.refuse("update-workstream", "--id", ws, "--status", "waiting", "--from-event", "old-wait")
        self.assertIn("not the current assignment", err)

    def test_an_accepted_wait_keeps_its_absolute_check_time(self):
        led = Ledger(self.root)
        ws = led.add()["workstreams"][-1]["id"]
        led.assign(ws)
        led.status(ws, "working")
        self.waiting_event(led, ws)
        stored = led.ws(ws)["events"][0]["wait"]["next_check_at"]
        self.assertFalse(stored.startswith("+"))
        led.status(ws, "waiting", "--from-event", "old-wait")
        self.assertEqual(led.ws(ws)["wait"]["next_check_at"], stored)


class LateEvidenceTests(Case):
    def test_a_question_after_completion_is_kept_without_effect(self):
        led = Ledger(self.root)
        ws = led.add()["workstreams"][-1]["id"]
        led.assign(ws)
        led.status(ws, "working")
        led.complete(ws, base=None)
        event = {
            "event_id": "late",
            "request_id": "req-done",
            "generation": 1,
            "attempt": 1,
            "owner": "worker-1",
            "kind": "question",
            "text": "one more thing?",
            "source": {"kind": "reply-correlated"},
        }
        led.do("record-event", "--id", ws, "--event", json.dumps(event))
        self.assertEqual(led.ws(ws)["events"][0]["status"], "stale")
        self.assertEqual(led.snapshot()["decisions"], [])


class SuccessorTests(Case):
    def completed_ship(self, led: Ledger) -> str:
        ws = self.ship(led)
        led.status(ws, "working")
        led.complete(ws)
        return ws

    def test_an_incompatible_successor_is_refused(self):
        led = Ledger(self.root)
        ws = self.completed_ship(led)
        err = led.refuse(
            "add-workstream",
            "--title",
            "t",
            "--kind",
            "scout",
            "--repository",
            "fixture-repo",
            "--scope",
            "s",
            "--brief",
            "b",
            "--acceptance-criteria",
            '["it works"]',
            "--delivery-authority",
            "d",
            "--permitted-operations",
            '["local-edit"]',
            "--supersedes",
            ws,
        )
        self.assertIn("must carry the same ['kind', 'deliverable']", err)
        err = led.refuse(
            "add-workstream",
            "--title",
            "t",
            "--kind",
            "ship",
            "--repository",
            "other-repo",
            "--scope",
            "s",
            "--brief",
            "b",
            "--acceptance-criteria",
            '["it works"]',
            "--delivery-authority",
            "d",
            "--permitted-operations",
            '["local-edit"]',
            "--deliverable",
            "local-branch",
            "--supersedes",
            ws,
        )
        self.assertIn("repository", err)

    def test_superseded_work_cannot_land_and_dependents_wait_for_the_successor(self):
        led = Ledger(self.root)
        ws = self.completed_ship(led)
        successor = led.add("ship", None, None, None, "again", "--supersedes", ws)["workstreams"][-1]["id"]
        landing = {
            "executor": "coordinator",
            "method": "merge",
            "target_ref": "main",
            "merged_sha": "9" * 40,
            "evidence": "x",
        }
        self.assertIn("superseded", led.refuse("record-landing", "--id", ws, "--landing", json.dumps(landing)))
        dependent = led.add("scout", [ws])["workstreams"][-1]["id"]
        led.assign(dependent)
        self.assertIn(
            "not verified complete", led.refuse("update-workstream", "--id", dependent, "--status", "working")
        )
        led.ship_ready(successor, worktree="/fixture/worktrees/again")
        led.status(successor, "working")
        led.complete(successor)
        led.status(dependent, "working")

    def test_a_stacked_child_follows_its_parents_successor(self):
        led = Ledger(self.root)
        parent = self.completed_ship(led)
        successor = led.add("ship", None, None, None, "again", "--supersedes", parent)["workstreams"][-1]["id"]
        led.ship_ready(successor, worktree="/fixture/worktrees/again")
        led.status(successor, "working")
        led.complete(successor, head=SHA_C)
        child = led.add("ship", [parent], parent, None, "child")["workstreams"][-1]["id"]
        led.ship_ready(child, base=SHA_C, worktree="/fixture/worktrees/child")
        led.status(child, "working")


class MigratedProfileTests(Case):
    def test_a_migrated_fixture_supports_due_and_a_fresh_v3_completion(self):
        source = self.tmp.base / "v2.json"
        source.write_bytes((FIXTURES / "v2-run.json").read_bytes())
        before = source.read_bytes()
        record = ok(self.root, ["migrate-v2", "--source", str(source), "--session-id", SESSION])
        self.assertEqual(record["profile"]["supervision"]["result_window_minutes"], 90)
        self.assertIn("source_profile_digest", record["migrated_from"])
        run_id = record["run_id"]
        ok(self.root, ["due", "--run-id", run_id])

        def do(*args: str) -> dict:
            return ok(self.root, [args[0], "--run-id", run_id, "--session-id", SESSION, *args[1:]])

        do("issue-request", "--id", "ws-2", "--request-id", "v3-brief", "--purpose", "brief")
        payload = result_payload("v3-brief", owner="worker-2", base_sha=None, head_sha=None)
        do("record-result", "--id", "ws-2", "--result", json.dumps(payload))
        do("update-workstream", "--id", "ws-2", "--status", "review-ready")
        do("update-workstream", "--id", "ws-2", "--status", "completed", "--verification", verification(None))
        self.assertEqual(source.read_bytes(), before)


class ReceiptRuntimeTests(unittest.TestCase):
    def test_a_codex_launch_cannot_stand_for_a_claude_assignment(self):
        ready = json.loads((FIXTURES / "peer-sessions" / "launch-receipt.codex-ready.provisional.json").read_text())
        ready["correlation"].update(workstream_id="ws-1", generation="1")
        normalized = normalize_launch(ready, session="reviewer", request_id="req-1")
        ws = {
            "id": "ws-1",
            "kind": "ship",
            "binding": "peer-session",
            "assignment_generation": 1,
            "dispatch_attempts": 1,
            "worktree": "/work/repo",
            "owner_identity": {"pane_id": "wT:p2"},
            "requests": [{"request_id": "req-1", "generation": 1, "dispatch_attempt": 1}],
        }
        problems = check_session_receipt(normalized, run_id="run-7", workstream=ws)
        self.assertTrue(any("binding runs claude" in p for p in problems), problems)
        claude = json.loads((FIXTURES / "peer-sessions" / "launch-receipt.partial.provisional.json").read_text())
        first = normalize_launch(claude, session="first", request_id="req-1")
        ws_claude = {
            **ws,
            "id": "ws-2",
            "assignment_generation": 3,
            "worktree": "/work/a",
            "requests": [{"request_id": "req-1", "generation": 3, "dispatch_attempt": 1}],
        }
        self.assertEqual(check_session_receipt(first, run_id="run-7", workstream=ws_claude), [])


class StrictForgeReadTests(ForgeCase):
    def test_missing_or_broken_connections_are_uncertain(self):
        data = scenario()
        data["graphql"]["Threads"][""]["repository"]["pullRequest"]["reviewThreads"] = None
        self.assertTrue(self.read(data)["uncertain"])
        data = scenario()
        del data["graphql"]["Threads"][""]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"]
        self.assertTrue(self.read(data)["uncertain"])
        data = scenario()
        data["graphql"]["Checks"][""]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"][
            "statusCheckRollup"
        ]["contexts"] = None
        self.assertTrue(self.read(data)["uncertain"])

    def test_a_commit_without_any_check_rollup_is_a_real_empty_inventory(self):
        data = scenario()
        data["graphql"]["Checks"][""]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"][
            "statusCheckRollup"
        ] = None
        ev = self.read(data)
        self.assertEqual((ev["uncertain"], ev["checks"]["count"]), (False, 0))

    def test_checks_read_for_another_commit_are_uncertain_on_any_page(self):
        data = scenario()
        data["graphql"]["Checks"][""]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]["oid"] = "9" * 40
        self.assertTrue(self.read(data)["uncertain"])
        data = scenario()
        second = checks_page([check_run("test")])
        second["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]["oid"] = "9" * 40
        data["graphql"]["Checks"] = {"": checks_page([check_run("lint", app=99)], more="c2"), "c2": second}
        self.assertTrue(self.read(data)["uncertain"])

    def test_ambiguous_active_runs_stay_pending_in_any_order(self):
        undated = check_run("test", "QUEUED")
        undated["startedAt"] = None
        for nodes in ([check_run("test"), undated], [undated, check_run("test")]):
            data = scenario()
            data["graphql"]["Checks"][""] = checks_page(nodes)
            self.assertEqual(self.read(data)["checks"]["pending"], ["test"])
        tied = [check_run("test", "IN_PROGRESS"), check_run("test")]
        for nodes in (tied, list(reversed(tied))):
            data = scenario()
            data["graphql"]["Checks"][""] = checks_page(nodes)
            self.assertEqual(self.read(data)["checks"]["pending"], ["test"])

    def test_a_provably_newer_success_clears_an_older_failure(self):
        data = scenario()
        old = check_run("test", "FAILURE", at="2026-10-31T09:00:00Z")
        data["graphql"]["Checks"][""] = checks_page([check_run("test", at="2026-10-31T11:00:00Z"), old])
        self.assertEqual(self.read(data)["checks"]["failing"], [])

    def test_a_missing_pr_is_uncertain(self):
        data = scenario()
        data["graphql"]["PrFacts"] = [{"repository": {"nameWithOwner": "o/r", "pullRequest": None}}]
        self.assertTrue(self.read(data)["uncertain"])


class CheckSummaryTests(ForgeCase):
    def ship(self, led: Ledger, deliverable: str = "pr") -> str:
        perms = ["local-edit", "push", "pr-create"]
        args = ("--deliverable", deliverable)
        if deliverable == "pr":
            args += ("--forge-repo", "o/r", "--target-branch", "main")
        ws = led.add("ship", None, None, perms, "ship", *args)["workstreams"][-1]["id"]
        led.ship_ready(ws)
        led.status(ws, "working")
        led.result(ws, head_sha=HEAD, base_sha=SHA_A)
        led.status(ws, "review-ready")
        return ws

    def test_incomplete_or_inconsistent_summaries_are_refused(self):
        led = Ledger(self.root)
        ws = self.ship(led)
        good = self.read(scenario())
        for broken in (
            {**good, "checks": {"count": 1}},
            {**good, "checks": {**good["checks"], "required_passing": []}},
            {**good, "checks": {**good["checks"], "failing": "none"}},
            {**good, "review_threads": {}},
        ):
            err = led.refuse("record-publication", "--id", ws, "--evidence", json.dumps(broken))
            self.assertIn("fails", err)
        led.do("record-publication", "--id", ws, "--evidence", json.dumps(good))

    def test_published_branch_completion_needs_the_heads_remote_ref(self):
        led = Ledger(self.root)
        ws = self.ship(led, "published-branch")
        check = json.dumps({"command": "c", "check": "agent-coauthors", "exit_code": 0, "head_sha": HEAD})
        self.assertIn(
            "no published-branch evidence",
            led.refuse("update-workstream", "--id", ws, "--status", "completed", "--verification", check),
        )
        branch = {
            "schema": 1,
            "kind": "branch",
            "source": "git",
            "remote": "origin",
            "remote_ref": "refs/heads/s",
            "oid": SHA_B,
            "read_at": cs._now_iso(),
        }
        led.do("record-publication", "--id", ws, "--evidence", json.dumps(branch))
        err = led.refuse("update-workstream", "--id", ws, "--status", "completed", "--verification", check)
        self.assertIn("not the result head", err)
        led.do("record-publication", "--id", ws, "--evidence", json.dumps({**branch, "oid": HEAD}))
        led.status(ws, "completed", "--verification", check)


class RebaseRetainedRefTests(Case):
    def completed(self, led: Ledger, worktree: str) -> str:
        perms = ["local-edit", "push", "pr-create", "merge"]
        ws = led.add("ship", None, None, perms, "ship", "--deliverable", "pr", "--forge", "none")["workstreams"][-1][
            "id"
        ]
        led.ship_ready(ws, worktree=worktree)
        led.status(ws, "working")
        led.complete(ws)
        return ws

    def test_a_rebase_landing_needs_and_keeps_its_retained_ref(self):
        led = Ledger(self.root)
        ws = self.completed(led, "/fixture/worktrees/a")
        landing = {
            "executor": "coordinator",
            "method": "rebase",
            "target_ref": "main",
            "merged_sha": "9" * 40,
            "evidence": "x",
            "retained_ref": "refs/coordinate/retained/r/ws-1/g1-a1-b",
        }
        led.do("record-landing", "--id", ws, "--landing", json.dumps(landing))
        self.assertEqual(led.ws(ws)["landing"]["retained_ref"], landing["retained_ref"])
        other = self.completed(led, "/fixture/worktrees/b")
        del landing["retained_ref"]
        self.assertIn("retained_ref", led.refuse("record-landing", "--id", other, "--landing", json.dumps(landing)))


class TransitiveStackTests(Case):
    def test_a_grandchild_cannot_build_on_a_child_whose_parent_moved(self):
        led = Ledger(self.root)
        led.add("ship", None, None, None, "parent")
        led.ship_ready("ws-1")
        led.status("ws-1", "working")
        led.result("ws-1", "p1", head_sha=SHA_B, base_sha=SHA_A)
        led.status("ws-1", "review-ready")
        led.add("ship", ["ws-1"], "ws-1", None, "child")
        led.ship_ready("ws-2", base=SHA_B, worktree="/fixture/worktrees/c")
        led.status("ws-2", "working")
        led.complete("ws-2", head="e" * 40, base=SHA_B, request_id="c1")
        led.add("ship", ["ws-2"], "ws-2", None, "grandchild")
        led.ship_ready("ws-3", base="e" * 40, worktree="/fixture/worktrees/g")
        led.status("ws-1", "working")
        led.issue("ws-1", "p2", "repair")
        led.result("ws-1", "p2", issue=False, head_sha="d" * 40, base_sha=SHA_A)
        led.status("ws-1", "review-ready")
        self.assertIn("verified result commit", led.refuse("update-workstream", "--id", "ws-3", "--status", "working"))
        kinds = {(i["kind"], i["subject"]) for i in cs.due_items(self.root, led.snapshot())}
        self.assertIn(("restack-required", "ws-3"), kinds)


class LandingDeliverableTests(Case):
    LANDING = {
        "executor": "coordinator",
        "method": "merge",
        "target_ref": "main",
        "merged_sha": "9" * 40,
        "evidence": "x",
    }

    def test_only_pr_or_landed_deliverables_land(self):
        led = Ledger(self.root)
        ws = led.add("ship", None, None, ["local-edit", "merge"], "local")["workstreams"][-1]["id"]
        led.ship_ready(ws)
        led.status(ws, "working")
        led.complete(ws)
        err = led.refuse("record-landing", "--id", ws, "--landing", json.dumps(self.LANDING))
        self.assertIn("only a pr or landed deliverable lands", err)

    def test_a_migrated_ship_without_a_deliverable_cannot_land(self):
        source = self.tmp.base / "v2.json"
        source.write_bytes((FIXTURES / "v2-run.json").read_bytes())
        run_id = ok(self.root, ["migrate-v2", "--source", str(source), "--session-id", SESSION])["run_id"]
        base = ["--run-id", run_id, "--session-id", SESSION]
        ok(self.root, ["amend-permissions", *base, "--id", "ws-1", "--add", "merge", "--user-words", "merge it"])
        err = fails(self.root, ["record-landing", *base, "--id", "ws-1", "--landing", json.dumps(self.LANDING)])
        self.assertIn("set-deliverable first", err)


class DueScopeTests(Case):
    def test_an_unbased_stacked_child_is_not_reported_for_restack(self):
        led = Ledger(self.root)
        led.add("ship", None, None, None, "parent")
        led.ship_ready("ws-1")
        led.status("ws-1", "working")
        led.complete("ws-1")
        led.add("ship", ["ws-1"], "ws-1", None, "child")
        kinds = {i["kind"] for i in cs.due_items(self.root, led.snapshot())}
        self.assertNotIn("restack-required", kinds)

    def test_a_conflict_surfaces_while_the_workstream_waits(self):
        led = Ledger(self.root)
        ws = led.add()["workstreams"][-1]["id"]
        led.assign(ws)
        led.status(ws, "working")
        led.result(ws, "req-1", base_sha=None)
        led.result(ws, "req-1", issue=False, base_sha=None, outcome="failed")
        led.status(ws, "waiting", "--wait-on", "ci", "--clears-when", "c", "--source", "s", "--next-check-at", "+30m")
        kinds = {i["kind"] for i in cs.due_items(self.root, led.snapshot())}
        self.assertIn("conflicting-result", kinds)


class RecheckTests(Case):
    def scout(self, led: Ledger) -> str:
        ws = led.add()["workstreams"][-1]["id"]
        led.assign(ws)
        led.status(ws, "working")
        return ws

    def test_a_reconciliation_does_not_settle_a_newer_conflict(self):
        led = Ledger(self.root)
        ws = self.scout(led)
        led.result(ws, "r", base_sha=None)
        led.result(ws, "r", issue=False, base_sha=None, outcome="failed")
        chosen = led.ws(ws)["results"][0]["normalized_digest"]
        led.do("reconcile-result", "--id", ws, "--request-id", "r", "--choose", chosen, "--evidence", "re-read")
        led.result(ws, "r", issue=False, base_sha=None, summary="a third, different reply")
        self.assertIn("conflicting replies", led.refuse("update-workstream", "--id", ws, "--status", "review-ready"))

    def test_a_successor_inherits_accepted_amendments(self):
        led = Ledger(self.root)
        ws = self.ship(led)
        led.status(ws, "working")
        led.issue(ws, "req-1")
        led.do("amend-intent", "--id", ws, "--user-words", "also cover Windows", "--request-id", "req-1")
        led.issue(ws, "req-2", "amend", "--body", "Also cover Windows.")
        led.result(ws, "req-2", issue=False, head_sha=SHA_B, base_sha=SHA_A)
        led.status(ws, "review-ready")
        led.status(ws, "completed", "--verification", verification())
        successor = led.add("ship", None, None, None, "again", "--supersedes", ws)["workstreams"][-1]
        self.assertEqual([a["user_words"] for a in successor["amendments"]], ["also cover Windows"])
        self.assertEqual(successor["amendments"][0]["inherited_from"], ws)

    def test_a_parked_run_refuses_a_running_replacement(self):
        led = Ledger(self.root)
        ws = self.scout(led)
        led.do("park", "--reason", "no wake path", "--resume-when", "go")
        self.assertIn("is parked", self.replace(led, ws, "worker-2"))
        self.assertEqual(led.ws(ws)["owner_id"], "worker-1")

    def test_evidence_reconciled_after_completion_has_no_effect(self):
        led = Ledger(self.root)
        identity = {"pane_id": "wT:p2"}
        ws = led.add()["workstreams"][-1]["id"]
        led.assign(ws, "worker-1", "--owner-identity", json.dumps(identity))
        led.status(ws, "working")
        led.issue(ws, "req-done")
        event = {
            "event_id": "q",
            "request_id": "req-done",
            "generation": 1,
            "attempt": 1,
            "owner": "worker-1",
            "kind": "question",
            "text": "which name?",
            "source": {"kind": "text-correlated"},
        }
        led.do("record-event", "--id", ws, "--event", json.dumps(event))
        led.result(ws, "req-done", issue=False, base_sha=None)
        led.status(ws, "review-ready")
        led.status(ws, "completed", "--verification", verification(None))
        led.do(
            "reconcile-evidence",
            "--id",
            ws,
            "--event-id",
            "q",
            "--identity",
            json.dumps(identity),
            "--evidence",
            "read",
        )
        self.assertEqual(led.ws(ws)["events"][0]["status"], "stale")
        self.assertEqual(led.snapshot()["decisions"], [])


if __name__ == "__main__":
    unittest.main()
