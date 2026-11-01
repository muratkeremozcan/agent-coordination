# Transport receipt contract

**Status: consumer of Peer Sessions contract v1 (pending), supported subset declared below.**

Transport: `muratkeremozcan/peer-sessions@3c7ad363a7baf37f331b2b095bf1d754d8c2ce23`, contract
`docs/coordination-contract-v1.md`, SHA-256
`df8e6344d125968718419261dff08ad0cb66ea2e14da95a098c7141efd946322`. The transport labels the
contract pending; this declaration records what this package consumes and does not change the
transport's status. Its example receipts are labelled provisional and are fixtures here, never
evidence of a live run.

## Receipts the ledger checks

`record-delivery` checks every receipt against the assignment it claims
(`adapters/session_bindings.py check_session_receipt`): the contract (this package's
`provisional, non-authoritative` label, or the Peer Sessions v1 contract object as received),
run and workstream, assignment generation, an issued request of the current generation and
attempt, the assigned binding, the runtime identity recorded at `assign`, and for ship work the
leased worktree as `cwd`. A receipt with problems is stored `stale` or `uncertain`. Delivery
and lifecycle states never establish a result.

## Peer Sessions v1: supported subset

`adapters/peer_sessions_receipt.py` normalizes one named session of a v1 launch receipt.

| v1 field | Used as |
|---|---|
| `contract` | kept exactly as received |
| `correlation.run_id`, `workstream_id`, `generation` | run, workstream, assignment generation (generation parsed as an integer) |
| `sessions[].cwd` | `cwd` |
| `sessions[].identity` (`pane_id`, `terminal_id`, `workspace_id`, `tab_id`, `agent_kind`, `session_id`, `pid`, `proc_start`) | identity, only fields the transport recorded |
| `sessions[].state` | `ready` is lifecycle `idle`; every other state is `unknown` and named in `uncertainty` |
| `resources[]` with `owned: true` for the session | `owned_resources` |
| `status`, `launch_id`, `failure`, `persistence_error` | `source_receipt` and `uncertainty` |

Not consumed or not provable from a launch receipt: delivery of a brief (`delivery_state` is
`unknown`, or `blocked` at an approval UI), a request id (the coordinator supplies the brief's
request), targeted interrupt, and sender or time provenance for `text-correlated` replies.
Replies read with `peer-inbox.py --request-id --target` are `reply-correlated` evidence;
correlation lines found in pane text are `text-correlated` and stay uncertain until
`reconcile-evidence`.

## Fields and issuers

| Field | Issuer | Transport duty |
|---|---|---|
| `run_id`, `workstream_id` | Coordinate | echo |
| `assignment_generation` | Coordinate (bumped on every `assign`) | echo |
| `request_id` | Coordinate (fresh per brief or follow-up) | echo on acknowledgement and result |
| `binding`, `runtime_kind` | Coordinate selects; transport confirms | report |
| session id, process start identity, pane/workspace id, cwd | transport backend | report |
| `delivery_state` (`delivered`, `acknowledged`, `blocked`, `unknown`) | transport | report |
| `lifecycle_state` (`working`, `idle`, `done`, `unknown`) | transport | report |
| `owned_resources`, partial-launch inventory, uncertainty | transport | report |
| result outcome, artifacts, verification, review | owner, carried by transport | carry; Coordinate verifies |
| completion, landing, cleanup disposition | Coordinate | none |
