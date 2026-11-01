"""Runtime choice stays explicit; delivery receipts are checked against the assignment they claim."""

import copy
import json
import unittest

import _guard  # noqa: F401
from helpers import REPO_ROOT
from peer_sessions_receipt import ReceiptError, normalize_launch
from session_bindings import (
    PEER_SESSIONS_PIN,
    PROVISIONAL_LABEL,
    BindingUnavailable,
    capabilities,
    check_session_receipt,
    resolve_binding,
)

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "peer-sessions"
IDENTITY = {"pane_id": "wT:p2", "terminal_id": "term_first", "workspace_id": "wT"}
WS = {
    "id": "ws-1",
    "kind": "ship",
    "binding": "herdr-codex",
    "assignment_generation": 2,
    "dispatch_attempts": 1,
    "worktree": "/fixture/worktrees/ws-1",
    "owner_identity": IDENTITY,
    "requests": [{"request_id": "req-1", "generation": 2, "dispatch_attempt": 1}],
}


def receipt(**fields) -> dict:
    base = {
        "contract": PROVISIONAL_LABEL,
        "run_id": "run-abc123",
        "workstream_id": "ws-1",
        "assignment_generation": 2,
        "request_id": "req-1",
        "binding": "herdr-codex",
        "cwd": "/fixture/worktrees/ws-1",
        "identity": dict(IDENTITY),
        "delivery_state": "delivered",
        "lifecycle_state": "idle",
        "owned_resources": [{"kind": "pane", "id": "wT:p2"}],
    }
    base.update(fields)
    return base


def launch(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class BindingResolutionTests(unittest.TestCase):
    def test_coordinator_runtime_selects_the_worker_runtime_by_default(self):
        self.assertEqual(resolve_binding(coordinator_runtime="claude"), "peer-session")
        self.assertEqual(resolve_binding(coordinator_runtime="codex"), "codex-subagent")

    def test_an_installed_claude_transport_never_turns_a_codex_request_into_claude(self):
        self.assertEqual(
            resolve_binding(coordinator_runtime="codex", herdr_requested=True, env={"HERDR_ENV": "1"}), "herdr-codex"
        )
        with self.assertRaisesRegex(BindingUnavailable, "explicit instruction"):
            resolve_binding(coordinator_runtime="codex", herdr_requested=True, env={})

    def test_cross_runtime_routes_need_an_explicit_kind(self):
        self.assertEqual(resolve_binding(coordinator_runtime="codex", explicit_kind="claude"), "peer-session")
        with self.assertRaises(BindingUnavailable):
            resolve_binding(coordinator_runtime="claude", explicit_kind="codex", env={})
        self.assertEqual(
            resolve_binding(coordinator_runtime="claude", explicit_kind="codex", env={"HERDR_ENV": "1"}), "herdr-codex"
        )

    def test_unsupported_operations_and_the_transport_pin_are_declared(self):
        self.assertIn("targeted interrupt", capabilities("peer-session")["unsupported"])
        self.assertEqual(
            capabilities("peer-session")["transport_pin"], f"muratkeremozcan/peer-sessions@{PEER_SESSIONS_PIN}"
        )
        with self.assertRaises(BindingUnavailable):
            capabilities("cursor")

    def test_herdr_codex_interrupts_with_a_single_escape(self):
        interrupt = capabilities("herdr-codex")["operations"]["interrupt"]
        self.assertIn("send-keys <name> esc", interrupt)
        self.assertNotIn("ctrl+c", interrupt)


class ReceiptTests(unittest.TestCase):
    def check(self, **fields) -> list[str]:
        return check_session_receipt(receipt(**fields), run_id="run-abc123", workstream=WS)

    def test_a_receipt_matching_the_assignment_has_no_problems(self):
        self.assertEqual(self.check(), [])

    def test_a_never_issued_request_and_a_wrong_cwd_are_reported(self):
        problems = self.check(request_id="NEVER-ISSUED", cwd="/wrong/worktree")
        self.assertTrue(any("never issued" in p for p in problems))
        self.assertTrue(any("leased worktree" in p for p in problems))

    def test_a_request_from_an_earlier_attempt_is_reported(self):
        ws = copy.deepcopy(WS)
        ws["dispatch_attempts"] = 2
        problems = check_session_receipt(receipt(), run_id="run-abc123", workstream=ws)
        self.assertTrue(any("earlier generation or attempt" in p for p in problems))

    def test_a_different_or_missing_runtime_identity_is_reported(self):
        self.assertTrue(any("differs" in p for p in self.check(identity={**IDENTITY, "terminal_id": "term_other"})))
        self.assertTrue(any("does not report" in p for p in self.check(identity={"pane_id": "wT:p2"})))

    def test_unknown_contracts_are_refused(self):
        self.assertTrue(any("contract" in p for p in self.check(contract="v1")))
        other = {"name": "peer-sessions-transport", "version": "2", "status": "agreed"}
        self.assertTrue(any("unsupported transport contract" in p for p in self.check(contract=other)))

    def test_stale_generation_and_foreign_run_are_reported(self):
        problems = self.check(assignment_generation=1, run_id="other")
        self.assertTrue(any("stale" in p for p in problems))
        self.assertTrue(any("different run" in p for p in problems))

    def test_binding_and_state_vocabularies_are_enforced(self):
        problems = self.check(binding="peer-session", lifecycle_state="finished")
        self.assertTrue(any("not the assigned" in p for p in problems))
        self.assertTrue(any("lifecycle_state" in p for p in problems))


class PeerSessionsNormalizationTests(unittest.TestCase):
    def test_a_started_claude_session_normalizes_without_inventing_facts(self):
        normalized = normalize_launch(
            launch("launch-receipt.partial.provisional.json"), session="first", request_id="req-1"
        )
        self.assertEqual(normalized["contract"]["status"], "pending")
        self.assertEqual(
            (normalized["run_id"], normalized["workstream_id"], normalized["assignment_generation"]),
            ("run-7", "ws-2", 3),
        )
        self.assertEqual(normalized["identity"]["pane_id"], "wT:p2")
        self.assertEqual(normalized["delivery_state"], "unknown")
        self.assertEqual(normalized["lifecycle_state"], "unknown")
        self.assertIn("session state is 'started', not ready", normalized["uncertainty"])
        self.assertEqual({r["id"] for r in normalized["owned_resources"]}, {"/work/a", "wT:p2"})

    def test_a_failed_session_keeps_only_what_the_transport_recorded(self):
        normalized = normalize_launch(
            launch("launch-receipt.partial.provisional.json"), session="second", request_id="req-1"
        )
        self.assertEqual(normalized["identity"], {"pane_id": "wT:p3"})
        self.assertEqual(normalized["source_receipt"]["failure"]["code"], "agent_start_failed")

    def test_ready_and_incomplete_launches_map_to_idle_and_unknown(self):
        ready = launch("launch-receipt.codex-ready.provisional.json")
        ready["correlation"].update(workstream_id="ws-1", generation="1")
        normalized = normalize_launch(ready, session="reviewer", request_id="req-1")
        self.assertEqual((normalized["lifecycle_state"], normalized["delivery_state"]), ("idle", "unknown"))
        self.assertEqual(normalized["identity"]["terminal_id"], "term_reviewer")
        incomplete = copy.deepcopy(ready)
        incomplete["status"] = "incomplete"
        incomplete["sessions"][0]["state"] = "blocked"
        normalized = normalize_launch(incomplete, session="reviewer", request_id="req-1")
        self.assertEqual((normalized["lifecycle_state"], normalized["delivery_state"]), ("unknown", "blocked"))
        self.assertIn("launch status is 'incomplete'", normalized["uncertainty"])

    def test_a_launch_without_coordinator_correlation_is_refused(self):
        with self.assertRaisesRegex(ReceiptError, "missing 'workstream_id'"):
            normalize_launch(launch("launch-receipt.codex-ready.provisional.json"), session="reviewer", request_id="r")

    def test_malformed_receipts_are_refused(self):
        bad = launch("launch-receipt.partial.provisional.json")
        bad["schema_version"] = 2
        with self.assertRaisesRegex(ReceiptError, "schema_version"):
            normalize_launch(bad, session="first", request_id="r")
        bad = launch("launch-receipt.partial.provisional.json")
        bad["correlation"]["generation"] = "three"
        with self.assertRaisesRegex(ReceiptError, "not an integer"):
            normalize_launch(bad, session="first", request_id="r")
        with self.assertRaisesRegex(ReceiptError, "0 session"):
            normalize_launch(launch("launch-receipt.partial.provisional.json"), session="nobody", request_id="r")

    def test_a_normalized_receipt_is_checked_like_any_other(self):
        normalized = normalize_launch(
            launch("launch-receipt.partial.provisional.json"), session="first", request_id="req-1"
        )
        ws = {
            "id": "ws-2",
            "kind": "ship",
            "binding": "peer-session",
            "assignment_generation": 3,
            "dispatch_attempts": 1,
            "worktree": "/work/a",
            "owner_identity": {"pane_id": "wT:p2", "terminal_id": "term_first"},
            "requests": [{"request_id": "req-1", "generation": 3, "dispatch_attempt": 1}],
        }
        self.assertEqual(check_session_receipt(normalized, run_id="run-7", workstream=ws), [])
        stale = {**ws, "assignment_generation": 4}
        self.assertTrue(check_session_receipt(normalized, run_id="run-7", workstream=stale))
        wrong_cwd = {**ws, "worktree": "/work/b"}
        self.assertTrue(
            any("leased worktree" in p for p in check_session_receipt(normalized, run_id="run-7", workstream=wrong_cwd))
        )


if __name__ == "__main__":
    unittest.main()
