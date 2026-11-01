#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Bounded supervision checkpoint for the coordinator that holds a run.

Read-only on the ledger. It polls the run's `due` items and exits as soon as one exists, when
`--max-wait` passes (a heartbeat), when the run's claim moves to another holder or claim
generation, or on SIGINT or SIGTERM. It never sends, launches, collects replies or writes the
run record; the coordinator handles what it reports and re-arms it before ending a turn.

While it runs it keeps a beacon at `runs/<run_id>.watch` naming its holder, claim generation,
pinned process identity, interval and last cycle, and every exit path rewrites the beacon as
`retired`. `coordinator_state.py turnend-check` trusts only a running beacon whose process is
still alive and cycling, so an exited or killed helper never counts as an armed wake source.

Exit codes: 0 due items or heartbeat (printed as JSON), 3 claim changed, 130 interrupted,
1 or 2 for ledger or state-root errors.

Usage:
    coordinator_watch.py --state-root <root> --run-id <id> --session-id <holder> \\
        --max-wait 1200 --interval 30
"""

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_state as cs  # noqa: E402
from process_identity import process_identity  # noqa: E402
from state_root import StateRootError, resolve_state_root  # noqa: E402


class _Interrupted(Exception):
    pass


def _on_signal(signum, frame):  # noqa: ARG001
    raise _Interrupted()


def _write_beacon(path: Path, beacon: dict) -> None:
    cs._atomic_write_text(path, json.dumps(beacon, indent=2, sort_keys=True) + "\n")


def watch(root: Path, run_id: str, session_id: str, max_wait: float, interval: float) -> tuple[int, dict]:
    record = cs._load_snapshot(root, run_id)
    holder = record.get("claimed_by") or {}
    if holder.get("session_id") != session_id:
        return 3, {"outcome": "claim-changed", "detail": f"session {session_id!r} does not hold run {run_id}"}
    generation = holder["generation"]
    path = cs.watch_beacon_path(root, run_id)
    beacon = {
        "session_id": session_id,
        "claim_generation": generation,
        "pid": os.getpid(),
        "process_identity": process_identity(os.getpid()),
        "interval": max(1, int(interval)),
        "state": "running",
        "started_at": cs._now_iso(),
        "last_cycle_at": cs._now_iso(),
    }
    _write_beacon(path, beacon)
    outcome: tuple[int, dict] = (130, {"outcome": "interrupted"})
    deadline = time.monotonic() + max_wait
    try:
        while True:
            record = cs._load_snapshot(root, run_id)
            current = record.get("claimed_by") or {}
            if (current.get("session_id"), current.get("generation")) != (session_id, generation):
                outcome = (3, {"outcome": "claim-changed", "claimed_by": current})
                break
            beacon["last_cycle_at"] = cs._now_iso()
            _write_beacon(path, beacon)
            items = cs.due_items(root, record)
            if items:
                outcome = (0, {"outcome": "due", "items": items})
                break
            if time.monotonic() >= deadline:
                outcome = (0, {"outcome": "heartbeat", "items": []})
                break
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    except _Interrupted:
        outcome = (130, {"outcome": "interrupted"})
    finally:
        beacon.update(state="retired", retired_at=cs._now_iso(), exit_reason=outcome[1]["outcome"])
        _write_beacon(path, beacon)
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-root", default=None)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--max-wait", type=float, required=True, help="seconds before a heartbeat exit")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between checks")
    args = parser.parse_args(argv)
    previous = {sig: signal.signal(sig, _on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        root = resolve_state_root(args.state_root)
        code, report = watch(root, args.run_id, args.session_id, args.max_wait, args.interval)
    except StateRootError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except cs.CoordinatorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(json.dumps({"run_id": args.run_id, **report}, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
