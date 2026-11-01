"""Supervision: expectations, observations, due scheduling, sweeps, parking and the bounded watcher."""

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from datetime import timedelta
from unittest import mock

import _guard  # noqa: F401  (must load first: it installs the real-state guard)
import coordinator_state as cs
from helpers import REPO_ROOT, SESSION, Ledger, TempRoot, cli, ok

WATCH = REPO_ROOT / "scripts" / "coordinator_watch.py"
WAIT = ["--wait-on", "quota", "--clears-when", "usage resets", "--source", "limit notice", "--next-check-at", "+4h"]


def later(minutes: float):
    """Patch the ledger's clock forward."""
    moment = cs._now() + timedelta(minutes=minutes)
    return mock.patch.object(cs, "_now", return_value=moment)


class SupervisionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TempRoot(self)
        self.root = self.tmp.root
        self.led = Ledger(self.root)

    def working(self) -> str:
        ws = self.led.add()["workstreams"][-1]["id"]
        self.led.assign(ws)
        self.led.status(ws, "working")
        self.led.issue(ws, "req-1")
        return ws

    def due(self, minutes: float = 0) -> list[dict]:
        record = self.led.snapshot()
        with later(minutes):
            return cs.due_items(self.root, record, cs._now())

    def kinds(self, minutes: float = 0) -> set[str]:
        return {i["kind"] for i in self.due(minutes)}


class DueTests(SupervisionCase):
    def test_reply_deadlines_surface_and_progress_does_not_clear_the_result_expectation(self):
        ws = self.working()
        self.assertNotIn("ack-overdue", self.kinds(5))
        self.assertIn("ack-overdue", self.kinds(11))
        event = {
            "event_id": "e1",
            "request_id": "req-1",
            "generation": 1,
            "attempt": 1,
            "owner": "worker-1",
            "kind": "progress",
            "text": "on it",
            "source": {"kind": "reply-correlated"},
        }
        self.led.do("record-event", "--id", ws, "--event", json.dumps(event))
        self.assertNotIn("ack-overdue", self.kinds(11))
        self.assertIn("result-overdue", self.kinds(91))

    def test_a_quota_wait_suppresses_reply_deadlines_until_its_own_check(self):
        ws = self.working()
        self.led.status(ws, "waiting", *WAIT)
        self.assertEqual(self.kinds(120) & {"ack-overdue", "result-overdue", "wait-check"}, set())
        self.assertIn("wait-check", self.kinds(241))

    def test_a_deferred_decision_surfaces_only_at_its_deferral(self):
        self.led.do("open-decision", "--question", "Rename?", "--defer-until", "+3h")
        self.assertNotIn("decision-overdue", self.kinds(60))
        self.assertNotIn("decision-deferral", self.kinds(60))
        self.assertIn("decision-deferral", self.kinds(181))

    def test_an_acknowledged_item_resurfaces_on_schedule_and_never_loops(self):
        self.working()
        key = next(i["key"] for i in self.due(11) if i["kind"] == "ack-overdue")
        with later(11):
            self.led.do("ack-due", "--key", key, "--next-check-at", "+20m")
            self.assertNotIn(key, {i["key"] for i in cs.due_items(self.root, self.led.snapshot(), cs._now())})
        self.assertIn(key, {i["key"] for i in self.due(32)})

    def test_one_report_recovery_then_recovery_exhausted(self):
        ws = self.working()
        self.assertIn("result-overdue", self.kinds(91))
        self.led.do(
            "issue-request", "--id", ws, "--request-id", "req-1r", "--purpose", "report-recovery", "--root", "req-1"
        )
        self.assertNotIn("result-overdue", self.kinds(91))
        self.assertIn("recovery-exhausted", self.kinds(182))

    def test_a_terminal_result_clears_the_reply_deadlines(self):
        ws = self.working()
        self.led.result(ws, "req-1", issue=False, base_sha=None)
        self.assertEqual(self.kinds(200) & {"ack-overdue", "result-overdue"}, set())

    def test_sweeps_back_off_while_unchanged_and_reset_on_change(self):
        self.working()
        self.assertIn("sweep", self.kinds(11))
        self.led.do("record-sweep", "--changed", "false")
        self.assertEqual(self.led.snapshot()["supervision"]["sweep_interval_minutes"], 10)
        self.led.do("record-sweep", "--changed", "false")
        self.led.do("record-sweep", "--changed", "false")
        self.assertEqual(self.led.snapshot()["supervision"]["sweep_interval_minutes"], 40)
        for _ in range(5):
            self.led.do("record-sweep", "--changed", "false")
        self.assertEqual(self.led.snapshot()["supervision"]["sweep_interval_minutes"], 120)
        self.led.do("record-sweep", "--changed", "true")
        self.assertEqual(self.led.snapshot()["supervision"]["sweep_interval_minutes"], 10)

    def test_a_queued_ready_workstream_is_dispatchable(self):
        ws = self.led.add()["workstreams"][-1]["id"]
        self.led.assign(ws)
        self.assertIn("dispatchable", self.kinds())

    def test_due_reads_without_writing(self):
        self.working()
        path = self.root / "runs" / f"{self.led.run_id}.json"
        before = path.read_bytes()
        report = ok(self.root, ["due", "--run-id", self.led.run_id])
        self.assertIn("items", report)
        self.assertEqual(path.read_bytes(), before)


class ObservationTests(SupervisionCase):
    def observe(self, ws: str, owner: str, generation: int, attempt: int = 1) -> None:
        self.led.do(
            "observe",
            "--id",
            ws,
            "--owner",
            owner,
            "--binding",
            "herdr-codex",
            "--generation",
            str(generation),
            "--attempt",
            str(attempt),
            "--observed-at",
            cs._now_iso(),
            "--lifecycle",
            "idle",
            "--source",
            "herdr pane process-info",
            "--evidence",
            "shell prompt",
        )

    def test_an_observation_from_before_a_replacement_stays_stale(self):
        ws = self.working()
        self.led.status(ws, "blocked", "--blocker", "silent")
        self.led.do(
            "assign",
            "--id",
            ws,
            "--binding",
            "herdr-codex",
            "--owner-type",
            "collaborator",
            "--owner-id",
            "worker-2",
            "--handover-reason",
            "silent",
            "--quiescence-evidence",
            "pane closed",
        )
        self.observe(ws, "worker-1", 1)
        self.led.status(ws, "working")
        self.observe(ws, "worker-2", 2, attempt=1)
        self.observe(ws, "worker-2", 2, attempt=2)
        self.assertEqual([o["status"] for o in self.led.ws(ws)["observations"]], ["stale", "stale", "accepted"])


class ParkingTests(SupervisionCase):
    def test_a_parked_run_passes_turnend_check_and_unparks(self):
        self.working()
        code, out, _ = cli(self.root, ["turnend-check", "--run-id", self.led.run_id, "--session-id", SESSION])
        self.assertEqual(code, 2)
        self.led.do("park", "--reason", "no wake source in this host", "--resume-when", "the user says go")
        code, out, _ = cli(self.root, ["turnend-check", "--run-id", self.led.run_id, "--session-id", SESSION])
        self.assertEqual((code, json.loads(out)["reason"]), (0, "run is parked"))
        self.led.do("unpark", "--reason", "user said go")
        self.assertIsNone(self.led.snapshot()["supervision"]["parked"])


class WatcherTests(SupervisionCase):
    def spawn(self, max_wait: float, interval: float = 0.2) -> subprocess.Popen:
        env = {**os.environ, "COORDINATE_STATE_ROOT": str(self.root)}
        argv = [
            sys.executable,
            "-B",
            str(WATCH),
            "--run-id",
            self.led.run_id,
            "--session-id",
            SESSION,
            "--max-wait",
            str(max_wait),
            "--interval",
            str(interval),
        ]
        return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    def beacon(self) -> dict:
        return json.loads((self.root / "runs" / f"{self.led.run_id}.watch").read_text())

    def turnend(self) -> tuple[int, dict]:
        code, out, _ = cli(self.root, ["turnend-check", "--run-id", self.led.run_id, "--session-id", SESSION])
        return code, json.loads(out)

    def wait_for_beacon(self, proc: subprocess.Popen) -> None:
        for _ in range(100):
            if (self.root / "runs" / f"{self.led.run_id}.watch").exists():
                return
            time.sleep(0.05)
        self.fail(f"no beacon; watcher said {proc.communicate()}")

    def test_the_watcher_exits_on_a_due_item_and_retires_its_beacon(self):
        ws = self.led.add()["workstreams"][-1]["id"]
        self.led.assign(ws)
        path = self.root / "runs" / f"{self.led.run_id}.json"
        before = path.read_bytes()
        proc = self.spawn(10)
        out, _ = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(out)["outcome"], "due")
        self.assertEqual(self.beacon()["state"], "retired")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.turnend()[0], 0)  # nothing active: queued work only

    def test_a_running_watcher_counts_as_armed_and_an_exited_one_does_not(self):
        self.working()
        self.led.do("record-sweep", "--changed", "true")
        proc = self.spawn(30, 0.2)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.wait_for_beacon(proc)
        time.sleep(0.5)
        code, status = self.turnend()
        self.assertEqual((code, status["reason"]), (0, "watched"), status)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 130)
        self.assertEqual(self.beacon()["state"], "retired")
        self.assertEqual(self.turnend()[0], 2)

    def test_a_killed_or_recycled_watcher_never_counts(self):
        self.working()
        self.led.do("record-sweep", "--changed", "true")
        proc = self.spawn(30, 0.2)
        self.wait_for_beacon(proc)
        proc.kill()
        proc.communicate(timeout=10)
        self.assertEqual(self.beacon()["state"], "running")
        code, status = self.turnend()
        self.assertEqual(code, 2)
        self.assertIn("gone", status["reason"])
        beacon = self.beacon()
        beacon["process_identity"] = {
            **beacon["process_identity"],
            "pid": os.getpid(),
            "started": "Thu Jan  1 00:00:00 1970",
        }
        beacon["last_cycle_at"] = cs._now_iso()
        (self.root / "runs" / f"{self.led.run_id}.watch").write_text(json.dumps(beacon))
        self.assertEqual(self.turnend()[0], 2)

    def test_the_watcher_exits_when_the_claim_moves(self):
        self.working()
        self.led.do("record-sweep", "--changed", "true")
        proc = self.spawn(30, 0.2)
        self.wait_for_beacon(proc)
        self.led.do("release-claim", "--reason", "handing over")
        out, _ = proc.communicate(timeout=10)
        self.assertEqual((proc.returncode, json.loads(out)["outcome"]), (3, "claim-changed"))
        self.assertEqual(self.beacon()["exit_reason"], "claim-changed")

    def test_the_watcher_returns_a_heartbeat_at_max_wait(self):
        self.working()
        self.led.do("record-sweep", "--changed", "true")
        proc = self.spawn(0.6, 0.2)
        out, _ = proc.communicate(timeout=10)
        self.assertEqual((proc.returncode, json.loads(out)["outcome"]), (0, "heartbeat"))


if __name__ == "__main__":
    unittest.main()
