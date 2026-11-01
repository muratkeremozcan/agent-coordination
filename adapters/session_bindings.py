#!/usr/bin/env python3
"""Runtime bindings the coordinator may dispatch through, and provisional session receipts.

Runtime kind, transport and model are separate choices. A Codex coordinator dispatches
Codex workers and a Claude coordinator dispatches Claude workers unless the run's
instruction names another kind; the presence of an installed Claude transport never turns
a Codex request into a Claude one. An unavailable route is reported as a block with its
reason, and switching vendors needs the user's instruction.

A delivery receipt is checked against the ledger's own record: the request must have been
issued for the current generation and attempt, the runtime identity must match what the
assignment recorded, and a ship owner must be running in its leased worktree. Delivery and
lifecycle states are transport facts and never establish a result. Receipts built here carry
the label `provisional, non-authoritative`; receipts normalized from Peer Sessions carry that
transport's own contract object as received (`peer_sessions_receipt.py`). See
docs/transport-contract.md.
"""

PROVISIONAL_LABEL = "provisional, non-authoritative"
PEER_SESSIONS_REPO = "muratkeremozcan/peer-sessions"
PEER_SESSIONS_PIN = "3c7ad363a7baf37f331b2b095bf1d754d8c2ce23"
PEER_SESSIONS_CONTRACT = {
    "name": "peer-sessions-transport",
    "version": "1",
    "path": "docs/coordination-contract-v1.md",
    "sha256": "df8e6344d125968718419261dff08ad0cb66ea2e14da95a098c7141efd946322",
}

BINDINGS: dict[str, dict] = {
    "herdr-codex": {
        "runtime": "codex",
        "transport": "herdr",
        "requires": {"env": {"HERDR_ENV": "1"}, "coordinator_runtime": None},
        "operations": {
            "start": "herdr agent start <name> --kind codex --pane <returned pane id>",
            "send": "herdr agent prompt <name> <one-line pointer> --wait --timeout <ms> (transport evidence only)",
            "observe": "herdr pane process-info --pane <pane> / herdr agent read <name> --source recent-unwrapped",
            "interrupt": "read the pane first, then one `herdr agent send-keys <name> esc`; unconfirmed until idle",
            "close_owned": "close only pane ids this run created and recorded",
        },
        "identity_fields": ["pane_id", "terminal_id", "workspace_id"],
        "unsupported": [
            "provider session id",
            "process start identity",
            "request correlation (agent names follow the pane occupant)",
        ],
    },
    "codex-subagent": {
        "runtime": "codex",
        "transport": "native",
        "requires": {"env": {}, "coordinator_runtime": "codex"},
        "operations": {
            "start": "spawn_agent",
            "send": "send_message",
            "observe": "wait_agent",
            "interrupt": "interrupt_agent",
            "close_owned": "host-managed",
        },
        "identity_fields": ["agent_id"],
        "unsupported": ["visible pane", "request correlation beyond the host agent id"],
    },
    "peer-session": {
        "runtime": "claude",
        "transport": "peer-sessions",
        "transport_pin": f"{PEER_SESSIONS_REPO}@{PEER_SESSIONS_PIN}",
        "requires": {"env": {}, "coordinator_runtime": None},
        "operations": {
            "start": "peer-sessions spawn-fleet.py",
            "send": "SendMessage to the address resolved at send time",
            "observe": "peer-inbox.py filtered by message id",
            "interrupt": "unsupported in the pinned transport",
            "close_owned": "opt-in teardown of sessions this run created",
        },
        "identity_fields": ["session_id", "pane_id", "terminal_id"],
        "unsupported": [
            "targeted interrupt",
            "sender or time provenance for text-correlated replies read from a pane",
        ],
    },
}


class BindingUnavailable(Exception):
    """The requested route cannot be used from here; the reason is reported."""


def resolve_binding(
    *,
    coordinator_runtime: str,
    explicit_kind: str | None = None,
    herdr_requested: bool = False,
    env: dict[str, str] | None = None,
) -> str:
    """Pick the binding for a worker, or refuse with the reason."""
    env = env or {}
    if coordinator_runtime not in {"codex", "claude"}:
        raise BindingUnavailable(f"unknown coordinator runtime {coordinator_runtime!r}")
    kind = explicit_kind or coordinator_runtime
    if kind not in {"codex", "claude"}:
        raise BindingUnavailable(f"unknown agent kind {kind!r}")
    if kind == "claude":
        return "peer-session"
    if herdr_requested or coordinator_runtime == "claude":
        if env.get("HERDR_ENV") != "1":
            raise BindingUnavailable(
                "Codex workers through Herdr need HERDR_ENV=1 in the coordinator's pane; Herdr "
                "control is unavailable here and a different runtime needs an explicit instruction"
            )
        return "herdr-codex"
    return "codex-subagent"


def capabilities(binding: str) -> dict:
    if binding not in BINDINGS:
        raise BindingUnavailable(f"unknown binding {binding!r}")
    return BINDINGS[binding]


_RECEIPT_REQUIRED = (
    "contract",
    "run_id",
    "workstream_id",
    "assignment_generation",
    "request_id",
    "binding",
    "cwd",
    "identity",
    "delivery_state",
    "lifecycle_state",
    "owned_resources",
)
DELIVERY_STATES = {"delivered", "acknowledged", "blocked", "unknown"}
LIFECYCLE_STATES = {"working", "idle", "done", "unknown"}


def _contract_problem(contract: object) -> str | None:
    if contract == PROVISIONAL_LABEL:
        return None
    if isinstance(contract, dict):
        if (contract.get("name"), contract.get("version")) == (
            PEER_SESSIONS_CONTRACT["name"],
            PEER_SESSIONS_CONTRACT["version"],
        ):
            return None
        return f"unsupported transport contract {contract.get('name')!r} version {contract.get('version')!r}"
    return f"receipt contract must be {PROVISIONAL_LABEL!r} or a supported transport contract"


def check_session_receipt(receipt: dict, *, run_id: str, workstream: dict) -> list[str]:
    """Problems with a delivery receipt against the coordinator's own record of the assignment.

    An empty list means the receipt describes the current assignment; it still proves only
    delivery or lifecycle, never a result.
    """
    problems = []
    missing = [key for key in _RECEIPT_REQUIRED if key not in receipt]
    if missing:
        return [f"receipt is missing {missing}"]
    contract = _contract_problem(receipt["contract"])
    if contract:
        problems.append(contract)
    if receipt["binding"] not in BINDINGS:
        problems.append(f"unknown binding {receipt['binding']!r}")
    elif receipt["binding"] != workstream.get("binding"):
        problems.append(f"receipt binding {receipt['binding']!r} is not the assigned {workstream.get('binding')!r}")
    expected_runtime = BINDINGS.get(workstream.get("binding"), {}).get("runtime")
    reported = {receipt.get("runtime"), (receipt.get("identity") or {}).get("agent_kind")} - {None}
    if expected_runtime and reported - {expected_runtime}:
        problems.append(
            f"receipt reports runtime {sorted(reported)}; the {workstream.get('binding')} binding runs "
            f"{expected_runtime}"
        )
    if receipt["run_id"] != run_id or receipt["workstream_id"] != workstream["id"]:
        problems.append("receipt names a different run or workstream")
    if receipt["assignment_generation"] != workstream["assignment_generation"]:
        problems.append(
            f"receipt generation {receipt['assignment_generation']} is stale; current is "
            f"{workstream['assignment_generation']}"
        )
    request = next((r for r in workstream.get("requests", []) if r["request_id"] == receipt["request_id"]), None)
    if request is None:
        problems.append(f"request {receipt['request_id']} was never issued for this workstream")
    elif (request["generation"], request["dispatch_attempt"]) != (
        workstream["assignment_generation"],
        workstream["dispatch_attempts"],
    ):
        problems.append(f"request {receipt['request_id']} belongs to an earlier generation or attempt")
    if workstream.get("kind") == "ship" and receipt["cwd"] != workstream.get("worktree"):
        problems.append(f"receipt cwd {receipt['cwd']!r} is not the leased worktree {workstream.get('worktree')!r}")
    identity = receipt["identity"] if isinstance(receipt["identity"], dict) else {}
    recorded = workstream.get("owner_identity") or {}
    differing = sorted(k for k, v in recorded.items() if k in identity and identity[k] != v)
    if differing:
        problems.append(f"receipt identity differs from the assignment on {differing}")
    absent = sorted(k for k in recorded if k not in identity)
    if absent:
        problems.append(f"receipt does not report the recorded identity field(s) {absent}")
    if receipt["delivery_state"] not in DELIVERY_STATES:
        problems.append(f"delivery_state must be one of {sorted(DELIVERY_STATES)}")
    if receipt["lifecycle_state"] not in LIFECYCLE_STATES:
        problems.append(f"lifecycle_state must be one of {sorted(LIFECYCLE_STATES)}")
    if not isinstance(receipt["owned_resources"], list):
        problems.append("owned_resources must be a list")
    return problems
