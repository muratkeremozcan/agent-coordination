# agent-coordination

A `coordinate` skill for running engineering work as independent workstreams: one
coordinator, one owner session per workstream kept through review and fixes, bounded
subagents under each owner. The coordinator keeps a durable run ledger whose transitions
refuse what the evidence does not support, supervises owners between replies without a
daemon, proves publication and landing against the forge, and composes TreeHouse workspaces
with guarded lease, readiness and return.

- `skills/coordinate/SKILL.md`: the entrypoint, with `references/` for planning, recovery and
  supervision, delivery, runtimes, and the ship and scout briefs.
- `scripts/coordinator_state.py`: the run ledger (schema v3). Requires an explicit
  `--state-root` or `COORDINATE_STATE_ROOT`. `due` and `turnend-check` are its read-only
  supervision views.
- `scripts/coordinator_watch.py`: the bounded, read-only supervision checkpoint a coordinator
  arms before ending a turn.
- `scripts/run_profile.py` and `profiles/`: run profiles resolved before dispatch.
- `scripts/process_identity.py`: owned-process identity with a pinned time zone and locale.
- `adapters/workspace_treehouse.py`: TreeHouse v2.0.0 lease, verification and return, plus
  retained refs for squash-merged heads and the agent co-author check.
- `adapters/forge_github.py`: read-only GitHub evidence for publication and landing.
- `adapters/session_bindings.py`: runtime bindings and delivery-receipt checks.
- `adapters/peer_sessions_receipt.py`: Peer Sessions v1 launch receipt normalization.
- `schemas/`: JSON Schemas for the records the ledger reads and writes.
- `docs/`: provenance, compatibility, transport contract.

## Install

Clone this repository, then symlink `skills/coordinate` into each skill discovery directory,
such as `~/.claude/skills/`. The skill resolves `scripts/`, `adapters/` and `profiles/` from the
checkout behind that link, so the checkout must stay in place. Pick a state root outside any
assistant checkout and skill directory, for example `~/.local/state/coordinate`, and pass it as
`--state-root` or `COORDINATE_STATE_ROOT`.

## Tests

```bash
python3 -B -m unittest discover -s tests -v
```

Tests use temporary state roots, fake TreeHouse and `gh` CLIs and disposable Git
repositories. A guard redirects HOME and fails any test that touches real assistant state,
host skill directories or launches a real `herdr`, `treehouse` or `gh`.
