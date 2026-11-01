# Compatibility

## Runtimes targeted

| Component | Version | Notes |
|---|---|---|
| Python | 3.11+ | stdlib only (`tomllib` needs 3.11) |
| TreeHouse | v2.0.0 | `get --lease --lease-holder`, `return <path>`; no `update`, no newer flags |
| Herdr | 0.8.2 | binding `herdr-codex` requires `HERDR_ENV=1`; nothing here calls Herdr. `agent prompt --wait` does not track turns; `send-keys` takes `esc`; `pane process-info --pane` reads liveness |
| Peer Sessions | `muratkeremozcan/peer-sessions@3c7ad363a7baf37f331b2b095bf1d754d8c2ce23` | binding `peer-session`; contract v1 pending; launch receipts normalized by `adapters/peer_sessions_receipt.py` (see `transport-contract.md`) |
| GitHub CLI | `gh api` | `adapters/forge_github.py` only; REST and GraphQL reads, no writes |
| Claude Code | 2.1.312 | a background task's exit re-invokes the session (smoke-tested, below) |

## Capability gaps surfaced

- TreeHouse v2.0.0 prints a human `status` table only. The adapter reports
  `lease_observed: false` and infers the lease from a successful `get --lease` whose stdout
  path validates as a worktree of the intended repository. Post-return pool state is
  likewise unobservable; the return receipt records the exit status.
- Unaccounted writers are detected only through processes the coordinator recorded as owned
  (pid plus `ps` start time).
- `peer-session` has no targeted interrupt in the pinned transport, and text-correlated
  replies prove neither sender nor time. `herdr-codex` exposes no provider session id or
  process start identity.
- The ledger checks forge evidence's shape, identity, freshness, chronology and head binding;
  it cannot prove the evidence came from GitHub.

## Ledger schema v3

Field `schema_version` is `3`. The v3 reader refuses v1 and v2 records for reads and writes
and leaves their bytes untouched; `import-v1` and `migrate-v2` read a copy and write a new v3
record. Result payloads are `result-receipt.v2` (owner, source, decision inventory); owner
events, delivery receipts and forge evidence carry their own schema versions.

Added run fields: `coordinator_runtime`, `authority.amendments`, `supervision` (`parked`,
`due_acks`, `next_sweep_at`, `sweep_interval_minutes`), `migrated_from`. Decisions gain
`responsibility`, `origin_workstream`, `origin_event`, `affected_workstreams`, `gates`,
`defer_until`, `scope_history`, `answer_request_id` and a structured `resolution`.

Added workstream fields: `verified_head`, `wait`, `wait_history`, `base_history`,
`pending_admission`, `deliverable`, `delivery_contract`, `pr`, `publication`,
`landing_pending`, `user_words`, `user_references`, `amendments`, `permission_history`,
`events`, `observations`, `deliveries`, `owner_identity`, `runtime`, `disposition`,
`supersedes`, `superseded_by`, `from_scout`, `result_reconciliations`. Requests gain
`body_digest`, `root_request_id`, `answers`, `expect_ack_by`, `expect_result_by`,
`acked_at`, `progress_at`, `result_at`.

Changed rules:

- Only the latest substantive request of the current generation and attempt governs; its
  chain's first accepted terminal result is canonical, a differing recovery reply is a
  `conflict` until `reconcile-result`, and a later request, failure, base advance or intent
  amendment supersedes an older result.
- Replies carry their owner and source. Text-correlated replies are `uncertain` until
  `reconcile-evidence` binds them to the assignment's recorded runtime identity.
- Readiness and attempt admission are separate: a same-owner resume works at the cap, and a
  replacement spends exactly one attempt. Every start, resume and running replacement checks
  local-edit authority, open edit denials and dispatch gates; a parked run admits no new
  execution.
- A different reply to a request that already has one never replaces it; it is kept as a
  duplicate of the same outcome or a conflict to reconcile.
- Bases move only through `advance-base` (ancestry and containment evidence) or `restack`;
  `assign` sets a base only before one is established. Stack validity is derived and
  transitive, so a completed child of a rewritten parent, and anything stacked on it, stops
  satisfying dependents and cannot land. A successor must carry the same kind, repository,
  intent, acceptance and delivery contract; superseded work cannot land.
- Decisions gate dispatch, completion or landing of the workstreams they name; a user's
  decision is resolved only with the user's words; scouts inventory every decision they raised.
- `assign` refuses unknown bindings and other runtimes without the user's instruction.
- `pr` and `landed` deliverables need fresh PR evidence meeting the publication predicate;
  GitHub landings need pre-merge and post-merge evidence with a consistent chronology; ship
  completion needs a passing agent co-author check.
- Only `pr` and `landed` deliverables land; a migrated workstream sets its deliverable first.
- `close` needs an integration disposition for every completed ship workstream.
- Profiles gain optional `[supervision]` keys and `[delivery]` `evidence_max_age_minutes` and
  `forbid_agent_coauthors`; omitted keys resolve to documented defaults and the resolved
  profile is what a run freezes.

## Migrating v2 records

`migrate-v2 --source <copy>` never invents an attestation. v2 replies carry no owner or
source, so they become `historical` evidence that cannot govern; a working workstream needs a
fresh v3 request and result. A completed workstream keeps the head v2 verified it on as
`verified_head`. `coordinator_runtime`, decision responsibility and ship deliverables stay
empty until the coordinator records them (`set-deliverable`); completion and landing refuse until
then. The frozen profile is resolved through the defaulting rule and re-digested, with the
original profile and digest kept under `migrated_from` beside the source claim history; the
migrating session holds the new record's claim.

## Wake path smoke test

On Claude Code 2.1.312, `scripts/coordinator_watch.py --max-wait 20 --interval 2` against a
run with one working workstream was started as a background task. While it ran,
`turnend-check` reported `watched` (exit 0). It exited 20 seconds later with a `heartbeat`,
the host re-invoked the session with the task's completion, the beacon read `retired`, and
`turnend-check` then reported the run unwatched (exit 2). Codex hosts use a foreground
watcher within their per-call limit; no Stop hook configuration ships with this package.

## Ledger schema v2 (superseded)

Field: `schema_version` (unchanged spelling), now `2`. The v2 reader refuses every other
version, for reads and writes alike, and leaves such files untouched.

Added run fields: `authority.grants` (every action granted or withheld with its instruction),
`profile` and `profile_digest`, `denials`, `claim_history`, and `claimed_by.generation`.
Imported runs carry `imported_from`.

Added workstream fields: `brief_digest`, `stack_parent`, `permitted_operations`,
`assignment_generation`, `assignment_history` (with handover evidence), `base_sha`,
`workspace` (receipt), `requests`, `results`, `dispatch_attempts`, `landing`.

Changed transitions:

- `working -> completed` is removed; every completion passes through `review-ready`.
- `queued|review-ready -> working` checks owner, dependency completion, stack-parent head,
  ship workspace receipt, concurrency and attempt budget.
- `-> review-ready` needs an accepted completed result answering a request issued for the
  current generation and dispatch attempt.
- A stacked child starts only from a parent result whose artifacts, checks and review
  already satisfy completion.
- Replacing an active owner requires a handover reason and quiescence evidence.
- `-> completed` needs that result's evidence, the review policy and coordinator
  verification entries that name a command, exit 0 and, for ship work, the result head.
- `eligible` cleanup evidence must be timezone-qualified, recent, after the latest result or
  landing, and bound to the worktree and verified head.
- Every mutation must come from the claim holder; `release-claim` and forced handover with an
  expected holder replace the old unconditional `--force`.

## Reading v1 data

`import-v1 --source <copy>` reads a schema v1 record and writes a new v2 record into the
candidate root. The source is never written and is refused when it lies in a protected
location. Structured authority starts fully withheld because v1 free text cannot be parsed
into grants; the original wording is kept in `delivery_authority`. Workstreams get empty
results, so an imported working workstream must receive a fresh result before it can
advance.

## How the v1 reader treats a v2 file

From reading the v1 source (not executed): `_load_locked` accepts a v2 record because v2
keeps every v1 required key; `_validate_record` then raises `unsupported schema_version 2`,
so every v1 mutating command, including `abandon`, exits 1 without writing. v1 `list`
reports the file as `corrupt`. v1 `snapshot` does not call `_validate_record` and prints the
v2 record unchanged. A downgraded reader therefore refuses to overwrite v2 state.
