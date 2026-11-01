# Source provenance

This package was extracted on 2026-10-31 from the Engineering Coordinator capability of a
personal assistant repository. Raw copies of the sources were staged outside this repository
and never committed. Every file here was rewritten and sanitized before the first commit:
historical anecdotes, names, dates of past incidents, private context and the hard-coded
personal state default were removed or turned into profile values.

Source repository: `~/personal-agent`, HEAD `841f83ae36cc8adbd69c6e79ffd4b39dfe356c9d`.
A second checkout at `~/opensource/personal-agent` (HEAD
`dbac8a37fe790f47005947fcf2c81d0efc27890b`) held byte-identical copies of the three primary
sources. Neither checkout was modified.

| Source path | SHA-256 | Became |
|---|---|---|
| `_bmad/memory/personal-agent/capabilities/engineering-coordinator.md` | `a3dba7696bbb22e13b25c7fe821ab64fbd3607e3d31a474f047b5b09a0afc9d4` | `skills/coordinate/SKILL.md`, `references/{planning,recovery,delivery}.md`, `profiles/*.toml` |
| `_bmad/memory/personal-agent/capabilities/engineering-coordinator-brief.md` | `3db0a3ac5c61454d64eebf48444f2e90d842100ae68cc187d8a9b17e695cba7b` | `skills/coordinate/references/worker-brief.md` |
| `scripts/coordinator_state.py` | `32033feb366eae943262057dcb1eab6556ec7a9fdbb3c7b30eccf5b45bc91d0a` | `scripts/coordinator_state.py` (schema v2) |
| `scripts/_sanctum.py` | `4d31bff43c3eac8fd75018db288b92f0a83dcfcd83746f942eb9675b4dafcb5b` | read as source only; its default layout is restated in `scripts/state_root.py` as protected locations |
| `scripts/tests/test_coordinator_state.py` | `e9efbfaeb47c6299f8888660e2df7a043d6148b870cff473045e6df82871c662` | `tests/test_coordinator_state.py` (rewritten for v2 and the public CLI) |
| `scripts/tests/test_engineering_coordinator_capability.py` | `67171c1804f71d2047f95eff0d7371e644032e3564184dc1d658b8b1c40a1acf` | `tests/test_skill_entrypoint.py` (checks the repository entrypoint, without host registration) |

The investigation's diagnostic script (`inspection_checks.py`, SHA-256
`ed637fa16ffe6b13b88e53fb88b47b1cd0f32a916cf084703a7c968e9d537a0e`) was read only.
Its CHECK-1, CHECK-2 and CHECK-3 intents are covered by `DependencyTests`,
`CompletionEvidenceTests` and `CleanupEvidenceTests`, driven through the public CLI against
temporary roots. CHECK-4 and CHECK-5 concern the Peer Sessions transport and belong to that
effort.

## What was retained from the ledger

Locking with inode re-verification, temp-file and fsync writes with atomic rename, backup
rotation, revision compare-and-swap, hard-link run creation, corrupt-record quarantine and
abandonment, run-id path gating, whole-token failed-workstream acknowledgement, batch
atomicity and deterministic snapshots.

## What changed

See `docs/compatibility.md` for the schema v2 and v3 field and transition changes.

## Operational guarantees ported from firstmate

Schema v3 and its supervision, evidence and delivery rules port guarantees from
`firstmate` (MIT license, commit `30d6d15ec07e1efaeb3c795f0a24e817251d0718`). Everything was
rewritten for this package's ledger and skill except the agent co-author trailer list in
`adapters/workspace_treehouse.py`, which reproduces firstmate's data and cites it. No firstmate
code or state format is used.

| Guarantee here | Conceptual source in firstmate |
|---|---|
| Keyed open questions and blockers that only an answer closes; decisions scoped to the work they gate | `bin/fm-classify-lib.sh` decision fold, `.agents/skills/captain-hold-lifecycle/`, `docs/architecture.md` |
| One recovery ask per expectation, then escalation | `bin/fm-pending-reply-lib.sh` |
| Declared waits with owner, clearing condition and recheck time | `docs/architecture.md`, `bin/fm-classify-lib.sh` |
| No coordinator turn ends unwatched; a bounded watcher with a beacon | `docs/turnend-guard.md`, `docs/supervision-protocols/` |
| Fleet sweeps with backoff; an unchanged fleet is not news | `bin/fm-watch.sh`, `bin/fm-inactive-reconcile.sh`, `AGENTS.md` section 8 |
| The stuck-owner ladder and verifying an owner's claim before acting | `.agents/skills/stuck-crewmate-recovery/` |
| Durable instruction bodies sent as a pointer | `bin/fm-task-inbox-lib.sh`, `bin/fm-send.sh` |
| The user's words kept apart from the coordinator's spec; verbatim amendments | `bin/fm-brief.sh`, `bin/fm-dod-lib.sh`, `AGENTS.md` sections 7 and 11 |
| The implementation owner never decides its own scope-expanding finding | `.agents/skills/ask-user-authority/`, `AGENTS.md` section 7 |
| Scout reports as evidence, decision inventory, promotion; the diagnosis brief | `.agents/skills/scout-completion/`, `.agents/skills/diagnostic-reasoning/` |
| Merge only the verified head when green; landing proven, never assumed | `bin/fm-pr-merge.sh`, `bin/fm-teardown.sh`, `.agents/skills/ship-landing/` |
| Agent co-author trailers refused | `bin/fm-git-strip-ai-trailers.sh` (trailer list reproduced) |
| Escape as the Codex and Claude interrupt; dialogs read before typing | `bin/fm-control-lib.sh`, `docs/agent-control.md`, `bin/fm-busy-lib.sh` |
| Workers never administer worktrees or session managers; no `cd` prefixes | firstmate fix history (worktree removal loop, cd guard) |
| Pinned time zone and locale for process identity | firstmate fix history (recycled-pid tombstones) |

The Ctrl+C exit risk on an idle Codex composer is this package's inference; firstmate records
only that Escape is the verified interrupt.

## Peer Sessions

`adapters/peer_sessions_receipt.py` consumes `muratkeremozcan/peer-sessions` at commit
`3c7ad363a7baf37f331b2b095bf1d754d8c2ce23`, contract `docs/coordination-contract-v1.md`
(SHA-256 `df8e6344d125968718419261dff08ad0cb66ea2e14da95a098c7141efd946322`). Its example
launch receipts are copied unchanged into `tests/fixtures/peer-sessions/`.

## License

MIT. See `LICENSE`. firstmate's MIT notice also applies to the reproduced trailer list.
