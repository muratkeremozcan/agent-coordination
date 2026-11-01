#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Normalize a Peer Sessions v1 launch receipt into the coordinator's delivery receipt.

Peer Sessions (`muratkeremozcan/peer-sessions`, contract v1, status pending) writes one nested
launch receipt per spawn: `sessions[].identity`, `resources[]`, a `correlation` map of
coordinator ids it echoes verbatim, and a `contract` object. The coordinator checks flat
delivery receipts (`session_bindings.check_session_receipt`). This module is the boundary
between the two. It keeps the transport's contract object exactly as received, reports
only identity fields the transport actually recorded, and lists what the launch receipt
cannot prove:

- a launch proves no delivery, so `delivery_state` is `unknown` (or `blocked` when the session
  stopped at an approval UI); a brief's delivery is recorded separately;
- `ready` means the session reached its prompt, which is lifecycle `idle`; every other session
  state is `unknown` with the state named in `uncertainty`;
- resources are the ones this launch created for the named session, with `owned: true`.

Nothing is fabricated: a missing pid, session id or terminal id stays missing.

Usage:
    peer_sessions_receipt.py --receipt <launch receipt> --session <name> --request-id <id>
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_bindings import PEER_SESSIONS_CONTRACT  # noqa: E402

LAUNCH_SCHEMA = "peer-sessions/launch-receipt"
IDENTITY_KEYS = ("pane_id", "terminal_id", "workspace_id", "tab_id", "agent_kind", "session_id", "pid", "proc_start")


class ReceiptError(Exception):
    pass


def normalize_launch(receipt: dict, *, session: str, request_id: str) -> dict:
    """The coordinator's delivery receipt for one named session of a v1 launch receipt."""
    if not isinstance(receipt, dict) or receipt.get("schema") != LAUNCH_SCHEMA:
        raise ReceiptError(f"not a Peer Sessions launch receipt (schema {receipt.get('schema')!r})")
    if receipt.get("schema_version") != 1:
        raise ReceiptError(f"unsupported launch receipt schema_version {receipt.get('schema_version')!r}")
    contract = receipt.get("contract")
    if not isinstance(contract, dict) or contract.get("version") != PEER_SESSIONS_CONTRACT["version"]:
        raise ReceiptError(f"unsupported transport contract {contract!r}")
    sessions = [s for s in receipt.get("sessions") or [] if isinstance(s, dict) and s.get("name") == session]
    if len(sessions) != 1:
        raise ReceiptError(f"launch receipt holds {len(sessions)} session(s) named {session!r}; expected one")
    entry = sessions[0]
    correlation = receipt.get("correlation") or {}
    for key in ("run_id", "workstream_id", "generation"):
        if key not in correlation:
            raise ReceiptError(f"launch correlation is missing {key!r}; spawn with --correlate {key}=...")
    try:
        generation = int(correlation["generation"])
    except (TypeError, ValueError) as exc:
        raise ReceiptError(f"correlation generation {correlation['generation']!r} is not an integer") from exc
    identity = {k: v for k, v in (entry.get("identity") or {}).items() if k in IDENTITY_KEYS and v is not None}
    state = entry.get("state")
    uncertainty = []
    if state == "ready":
        lifecycle = "idle"
    else:
        lifecycle = "unknown"
        uncertainty.append(f"session state is {state!r}, not ready")
    delivery = "blocked" if state == "blocked" else "unknown"
    uncertainty.append("a launch receipt proves no delivery of a brief")
    if receipt.get("status") != "ready":
        uncertainty.append(f"launch status is {receipt.get('status')!r}")
    if receipt.get("persistence_error"):
        uncertainty.append(f"receipt file could not be written: {receipt['persistence_error']}")
    resources = [r for r in receipt.get("resources") or [] if r.get("session") == session and r.get("owned") is True]
    return {
        "contract": contract,
        "run_id": correlation["run_id"],
        "workstream_id": correlation["workstream_id"],
        "assignment_generation": generation,
        "request_id": request_id,
        "binding": "peer-session",
        "runtime": entry.get("runtime"),
        "backend": receipt.get("backend"),
        "cwd": entry.get("cwd"),
        "identity": identity,
        "delivery_state": delivery,
        "lifecycle_state": lifecycle,
        "owned_resources": resources,
        "uncertainty": uncertainty,
        "source_receipt": {
            "launch_id": receipt.get("launch_id"),
            "status": receipt.get("status"),
            "session_state": state,
            "failure": receipt.get("failure"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--request-id", required=True)
    args = parser.parse_args(argv)
    try:
        receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
        print(json.dumps(normalize_launch(receipt, session=args.session, request_id=args.request_id), indent=2))
    except (OSError, json.JSONDecodeError, ReceiptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
