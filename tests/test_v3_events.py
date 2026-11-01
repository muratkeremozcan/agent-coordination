"""Owner events, evidence sources, delivery receipts and binding choice, through the ledger CLI."""

import json
import unittest

import _guard  # noqa: F401  (must load first: it installs the real-state guard)
from helpers import Ledger, TempRoot, result_payload

IDENTITY = {"pane_id": "wT:p2", "terminal_id": "term_w1", "workspace_id": "wT"}


class EventsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TempRoot(self)
        self.root = self.tmp.root
        self.led = Ledger(self.root)

    def scout(self, kind: str = "scout") -> str:
        led = self.led
        ws = led.add(kind)["workstreams"][-1]["id"]
        if kind == "ship":
            led.ship_ready(ws)
        else:
            led.assign(ws, "worker-1", "--owner-identity", json.dumps(IDENTITY))
        led.status(ws, "working")
        led.issue(ws, "req-1")
        return ws

    def event(self, ws: str, event_id: str, kind: str, source: dict | None = None, *extra: str, **fields) -> dict:
        payload = {
            "event_id": event_id,
            "request_id": "req-1",
            "generation": self.led.ws(ws)["assignment_generation"],
            "attempt": 1,
            "owner": "worker-1",
            "kind": kind,
            "text": fields.pop("text", f"{kind} text"),
            "source": source or {"kind": "reply-correlated"},
            **fields,
        }
        return self.led.do("record-event", "--id", ws, "--event", json.dumps(payload), *extra)

    def request(self, ws: str) -> dict:
        return self.led.ws(ws)["requests"][-1]


class EventTests(EventsCase):
    def test_ack_and_progress_set_supervision_facts_and_never_satisfy_the_result(self):
        ws = self.scout()
        self.event(ws, "e1", "ack")
        self.event(ws, "e2", "progress")
        self.event(ws, "e3", "progress", text="halfway")
        request = self.request(ws)
        self.assertIsNotNone(request["acked_at"])
        self.assertIsNotNone(request["progress_at"])
        self.assertIsNone(request["result_at"])
        self.assertIn(
            "no accepted terminal result", self.led.refuse("update-workstream", "--id", ws, "--status", "review-ready")
        )

    def test_text_correlated_events_change_nothing_until_reconciled_with_a_runtime_read(self):
        ws = self.scout()
        self.event(ws, "e1", "ack", {"kind": "text-correlated"})
        self.assertEqual(self.led.ws(ws)["events"][0]["status"], "uncertain")
        self.assertIsNone(self.request(ws)["acked_at"])
        err = self.led.refuse(
            "reconcile-evidence",
            "--id",
            ws,
            "--event-id",
            "e1",
            "--identity",
            json.dumps({**IDENTITY, "pane_id": "x"}),
            "--evidence",
            "re-read",
        )
        self.assertIn("differs from the assignment", err)
        self.led.do(
            "reconcile-evidence",
            "--id",
            ws,
            "--event-id",
            "e1",
            "--identity",
            json.dumps(IDENTITY),
            "--evidence",
            "herdr agent read wT:p2",
        )
        self.assertEqual(self.led.ws(ws)["events"][0]["status"], "accepted")
        self.assertIsNotNone(self.request(ws)["acked_at"])

    def test_a_runtime_read_bound_to_the_recorded_identity_is_accepted_directly(self):
        ws = self.scout()
        self.event(ws, "e1", "ack", {"kind": "runtime-read", "identity": IDENTITY})
        self.assertEqual(self.led.ws(ws)["events"][0]["status"], "accepted")

    def test_a_ship_question_opens_a_decision_gating_its_own_completion(self):
        ws = self.scout("ship")
        self.event(ws, "e1", "question", key="flag-name", text="Which flag name?")
        decision = self.led.snapshot()["decisions"][0]
        self.assertEqual((decision["origin_workstream"], decision["affected_workstreams"]), (ws, [ws]))
        self.assertEqual(decision["gates"], ["completion", "landing"])

    def test_a_scout_question_is_inventoried_and_scoped_atomically(self):
        ws = self.scout()
        later = self.led.add()["workstreams"][-1]["id"]
        self.event(ws, "e1", "question", None, "--affected", json.dumps([later]), "--gates", '["dispatch"]')
        decision = self.led.snapshot()["decisions"][0]
        self.assertEqual((decision["affected_workstreams"], decision["gates"]), ([later], ["dispatch"]))
        self.led.do(
            "record-result",
            "--id",
            ws,
            "--result",
            json.dumps(result_payload("req-1", base_sha=None, decision_inventory=["dec-1"])),
        )
        self.led.status(ws, "review-ready")

    def test_a_waiting_event_and_a_direct_declaration_produce_the_same_wait(self):
        ws = self.scout()
        wait = {"wait_on": "quota", "clears_when": "usage resets", "next_check_at": "+60m"}
        self.event(ws, "e1", "waiting", wait=wait, text="hit the usage warning")
        self.led.status(ws, "waiting", "--from-event", "e1")
        from_event = self.led.ws(ws)["wait"]
        self.led.status(ws, "working")
        self.led.status(
            ws,
            "waiting",
            "--wait-on",
            "quota",
            "--clears-when",
            "usage resets",
            "--source",
            "hit the usage warning",
            "--next-check-at",
            "+60m",
        )
        direct = self.led.ws(ws)["wait"]
        self.assertEqual(set(from_event), set(direct))
        self.assertEqual((from_event["wait_on"], from_event["clears_when"]), (direct["wait_on"], direct["clears_when"]))

    def test_a_waiting_event_with_an_unbounded_check_is_refused(self):
        ws = self.scout()
        wait = {"wait_on": "quota", "clears_when": "reset", "next_check_at": "+20h"}
        err = self.led.refuse(
            "record-event",
            "--id",
            ws,
            "--event",
            json.dumps(
                {
                    "event_id": "e1",
                    "request_id": "req-1",
                    "generation": 1,
                    "attempt": 1,
                    "owner": "worker-1",
                    "kind": "waiting",
                    "text": "t",
                    "wait": wait,
                    "source": {"kind": "reply-correlated"},
                }
            ),
        )
        self.assertIn("rechecked", err)

    def test_events_from_another_owner_or_attempt_are_stale(self):
        ws = self.scout()
        self.event(ws, "e1", "ack", owner="worker-9")
        self.event(ws, "e2", "ack", attempt=7)
        self.assertEqual([e["status"] for e in self.led.ws(ws)["events"]], ["stale", "stale"])
        self.assertIsNone(self.request(ws)["acked_at"])

    def test_an_event_id_is_idempotent_and_cannot_change_payload(self):
        ws = self.scout()
        self.event(ws, "e1", "progress", text="one")
        self.event(ws, "e1", "progress", text="one")
        self.assertEqual(len(self.led.ws(ws)["events"]), 1)
        with self.assertRaises(AssertionError):
            self.event(ws, "e1", "progress", text="two")


class ResultSourceTests(EventsCase):
    def test_a_text_correlated_result_cannot_govern_until_reconciled(self):
        ws = self.scout()
        payload = result_payload("req-1", base_sha=None, source={"kind": "text-correlated"})
        self.led.do("record-result", "--id", ws, "--result", json.dumps(payload))
        self.assertEqual(self.led.ws(ws)["results"][0]["status"], "uncertain")
        self.assertIn(
            "no accepted terminal result", self.led.refuse("update-workstream", "--id", ws, "--status", "review-ready")
        )
        self.led.do(
            "reconcile-evidence",
            "--id",
            ws,
            "--request-id",
            "req-1",
            "--identity",
            json.dumps(IDENTITY),
            "--evidence",
            "read the pane",
        )
        self.led.status(ws, "review-ready")

    def test_a_result_naming_another_owner_is_stale(self):
        ws = self.scout()
        self.led.do(
            "record-result",
            "--id",
            ws,
            "--result",
            json.dumps(result_payload("req-1", base_sha=None, owner="worker-9")),
        )
        self.assertEqual(self.led.ws(ws)["results"][0]["status"], "stale")


class DeliveryAndBindingTests(EventsCase):
    def receipt(self, ws: str, **fields) -> dict:
        base = {
            "contract": "provisional, non-authoritative",
            "run_id": self.led.run_id,
            "workstream_id": ws,
            "assignment_generation": 1,
            "request_id": "req-1",
            "binding": "herdr-codex",
            "cwd": f"/fixture/worktrees/{ws}",
            "identity": {},
            "delivery_state": "delivered",
            "lifecycle_state": "working",
            "owned_resources": [],
        }
        return {**base, **fields}

    def test_a_foreign_request_or_wrong_cwd_receipt_is_kept_and_advances_nothing(self):
        ws = self.scout("ship")
        self.led.do("record-delivery", "--id", ws, "--receipt", json.dumps(self.receipt(ws, request_id="NEVER-ISSUED")))
        self.led.do("record-delivery", "--id", ws, "--receipt", json.dumps(self.receipt(ws, cwd="/wrong/worktree")))
        self.led.do("record-delivery", "--id", ws, "--receipt", json.dumps(self.receipt(ws)))
        self.assertEqual([d["status"] for d in self.led.ws(ws)["deliveries"]], ["stale", "uncertain", "accepted"])
        self.assertIsNone(self.request(ws)["acked_at"])
        self.assertEqual(self.led.ws(ws)["status"], "working")

    def test_assign_refuses_unknown_bindings_and_unrequested_runtimes(self):
        ws = self.led.add()["workstreams"][-1]["id"]
        err = self.led.refuse("assign", "--id", ws, "--binding", "cursor", "--owner-type", "x", "--owner-id", "w")
        self.assertIn("unknown binding", err)
        err = self.led.refuse(
            "assign", "--id", ws, "--binding", "peer-session", "--owner-type", "peer", "--owner-id", "c1"
        )
        self.assertIn("needs the user's instruction", err)
        self.led.do(
            "assign",
            "--id",
            ws,
            "--binding",
            "peer-session",
            "--owner-type",
            "peer",
            "--owner-id",
            "c1",
            "--runtime-instruction",
            "use a Claude peer for this one",
            "--runtime-model",
            "claude-opus-5-5",
        )
        runtime = self.led.ws(ws)["runtime"]
        self.assertEqual(
            (runtime["kind"], runtime["model"], runtime["effort"]), ("claude", "claude-opus-5-5", "unknown")
        )


if __name__ == "__main__":
    unittest.main()
