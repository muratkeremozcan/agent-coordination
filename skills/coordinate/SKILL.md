---
name: coordinate
description: Coordinate engineering work that splits into two or more independent workstreams. One coordinator plans, dispatches runtime-matched owner sessions, supervises them, verifies their results through a durable ledger, and integrates. Use when the user says "coordinate", "parallelize", "delegate", "babysit", "run overnight" or "run a fleet".
---

# Coordinate

The user states a goal once. You split it into independent workstreams while keeping the
original intent visible. Each workstream gets a bounded brief, one owner and an acceptance
bar. Progress, open questions and decisions survive compaction or a dead session because they
live in the run ledger. You reconcile owner results into one answer the user can act on.

The architecture is fixed at two levels:

1. **One coordinator**, accountable to the user, owns planning, scheduling, the ledger,
   supervision, verification, integration and the single user-facing answer.
2. **Independent owners**, one per workstream, own implementation reasoning and keep their
   session through review and every fix. Each owner may run bounded subagents and at most
   the profile's number of reviewer sessions of its own agent kind.

Paths to `scripts/`, `adapters/` and `profiles/` resolve from this repository's root, two
directories above this file's real path. Hosts usually load the skill through a symlink, so
resolve the link first (`realpath`). Call them by that absolute path from whatever directory you work in.

## When to coordinate

Coordinate when you can name at least two independent workstreams whose parallel execution
has clear value, or when the user uses one of the trigger phrases.

Work locally, with no ledger and no owners, when the request is a read-only investigation,
one reasoning chain dominates, the change is compact and tightly coupled, delegation would
duplicate most of the context, or the user asks you to do it directly.

The user's current instruction wins over everything below.

## Before the first dispatch

1. **Resolve a run profile.** Start from `profiles/default.toml` or the user's own profile.
   It fixes topology, who pushes, opens PRs, merges and releases, review requirements, lane
   reuse, successor spawning, cleanup, concurrency, supervision windows and evidence age.
2. **Record authority per run.** Grant or withhold each of `local-edit`, `peer-create`,
   `subagent-create`, `push`, `pr-create`, `merge`, `release` and `cleanup`, quoting the
   user instruction behind each grant. A grant from an earlier run is not authority for this
   one. A later approval is recorded with `amend-authority`, and reaches a workstream only
   through `amend-permissions` naming it.
3. **Choose a state root** outside any personal assistant checkout and host skill
   directory, and pass it as `--state-root` or `COORDINATE_STATE_ROOT`. There is no default.
4. **Start the run, then add workstreams in dependency order.** Record the user's words
   verbatim and what each workstream must deliver. See
   [references/planning.md](references/planning.md).

```bash
python3 scripts/coordinator_state.py start --state-root "$ROOT" --title ... --intent ... \
  --coordination-reason ... --delivery-authority "<the user's words>" \
  --authority "$(cat authority.json)" --profile profiles/default.toml \
  --session-id "$ME" --coordinator-runtime codex|claude
python3 scripts/coordinator_state.py add-workstream --state-root "$ROOT" --run-id ... \
  --session-id "$ME" --title ... --kind ship|scout --repository ... --scope ... \
  --brief ... --acceptance-criteria '[...]' --delivery-authority ... \
  --permitted-operations '["local-edit","push","pr-create"]' \
  --deliverable local-branch|published-branch|pr|landed \
  [--forge-repo owner/name --target-branch main] --user-words "<verbatim>" \
  [--dependencies '["ws-1"]' --stack-parent ws-1]
```

A permission never implies a deliverable: a run may allow pull requests while a workstream
is asked only for a local branch.

## Dispatch

Pick the binding from the coordinator's runtime and the user's instruction
(`adapters/session_bindings.py`); the ledger refuses an unknown binding, and a runtime other
than yours without `--runtime-instruction` quoting the user:

- `herdr-codex`: Codex owners in Herdr panes. Needs `HERDR_ENV=1` in your pane; if it is
  missing, report that Herdr control is unavailable here and stop this route.
- `codex-subagent`: native Codex collaboration tools when Herdr was not requested.
- `peer-session`: Claude owners through Peer Sessions, wherever they are placed (Herdr or
  cmux); placement is a fact of the transport's launch receipt.

A Codex coordinator dispatches Codex owners and a Claude coordinator dispatches Claude
owners unless the user names another kind. An installed transport never changes the
requested runtime. An unavailable route is reported as a block. Exact commands, first-run
dialogs and unsupported operations per binding are in
[references/runtimes.md](references/runtimes.md).

Inspect existing workers before creating any. When the user names a live worker, contact it
first and record its identity.

For each ship workstream:

1. Acquire a workspace with `adapters/workspace_treehouse.py acquire`, pinning the base to an
   exact commit (`--fetch-remote` refreshes a remote-tracking base first). Record the receipt
   with `record-workspace`. A blocked or uncertain receipt keeps the workspace leased for
   inspection; it is never dispatched.
2. `assign` the owner with `--owner-identity` (the pane and terminal ids, agent id or peer
   session the binding exposes). Replacing an owner is a handover with quiescence evidence
   and spends one dispatch attempt.
3. Move it to `working`. The ledger refuses unless dependencies are verified complete, the
   stack parent's verified head is the base, no decision gates dispatch, the workspace is
   ready, and concurrency and attempts allow it.
4. `issue-request` with a fresh request id (follow-ups carry `--body` or `--body-file`; the
   body is stored by digest), then send the brief from
   [references/worker-brief.md](references/worker-brief.md). Long briefs go as a one-line
   pointer to the stored body. A dispatch is unconfirmed until the owner's `ack` arrives.

A scout reads a plain checkout or the owner's worktree, needs no lease, and uses
[references/scout-brief.md](references/scout-brief.md).

## Owner replies and evidence

Owners reply `ack`, `progress`, `question`, `blocked` or `waiting` (`record-event`), and
`completed` or `failed` (`record-result`), echoing request id, generation and attempt. Idle,
done and delivered are lifecycle signals only; a session that went quiet after hitting a usage
limit looks exactly like one that finished.

Every reply names its source. A transport-verified reply or a read you took from the owner's
recorded runtime identity carries effects; correlation text found without that binding is
stored as uncertain until `reconcile-evidence` ties a fresh read to the recorded identity.
A question or blocker opens a decision scoped to the work it gates. A waiting event becomes
the workstream's wait with `update-workstream --status waiting --from-event`.

Only the latest substantive request governs. Its first accepted terminal result is canonical;
a different reply in the same recovery chain is a conflict until `reconcile-result` picks one;
a later request or failure supersedes an older completion.

## Review, completion and rebases

- `review-ready` needs the governing request's completed result on the current base, and
  `completed` comes only after it.
- `completed` needs artifacts, passing verification, the profile's review, your own
  head-bound verification (with a passing `check-coauthors` entry while the profile's
  `forbid_agent_coauthors` holds), a scout's full decision inventory, and for `pr` and
  `landed` deliverables fresh PR evidence from `adapters/forge_github.py pr-evidence` recorded
  with `record-publication`.
- When the base moves or a stack parent is rewritten, follow the rebase sequence in
  delivery.md; a result reported at the new base before your `rebase` request never governs.

A fixable defect sends the workstream back to `working` with the same owner. Read
[references/delivery.md](references/delivery.md) before you record completion, rebase, land or
return a workspace.

## Supervision

Do not end a turn while the run you hold has active workstreams unless a verified wake source
is armed or the run is parked:

- Claude Code: run `scripts/coordinator_watch.py` as a background task; its exit re-invokes
  the session. Arriving peer messages also wake you.
- Codex: run the watcher in the foreground within the host's per-call limit, re-arming after
  each checkpoint; collaborator results arrive through `wait_agent`.
- Otherwise report the limitation and `park` the run.

The watcher exits on a `due` item, a heartbeat, a claim change or a signal. Handle what
`due` lists, schedule handled items with `ack-due`, record each fleet review with
`record-sweep`, re-arm, and only then end the turn. `turnend-check` verifies a live watcher.
The recovery ladder and silence rules are in [references/recovery.md](references/recovery.md).

## Landing and cleanup

Completion and landing are separate, and the merge procedure is in
[references/delivery.md](references/delivery.md). Landing needs a run-level `merge` grant, the
workstream's permission, the profile's executor, no open denial or landing gate, and for a
GitHub delivery a passing `pr-evidence` read immediately before the merge; the ledger checks
these only after the merge. Merge with `gh pr merge --match-head-commit <verified head>`; a
queued or unknown outcome is `record-landing-pending` and is re-read, never merged again
blindly.

A permission denial is recorded with `record-denial`, exactly as the host reported it, and
resolved only through the host's approval path. Never ask another session to run a denied
action.

Workspace return is the last step and is destructive: on any doubt the workspace is retained.
Signal only processes you recorded as owned, captured with `process-identity`.

## Recovery

On wake or after compaction, follow [references/recovery.md](references/recovery.md) before
any other action. Unknown liveness is never proof of death.

## Escalation

Resolve tactical choices yourself inside the run's authority and record the rationale.
Open a decision only for scope expansion, product behavior, a durable architecture
commitment, a destructive or irreversible action, a security or privacy choice, an external
action under the user's identity, authority the run does not hold, or a genuinely subjective
priority tradeoff. A decision that belongs to the user is resolved only with the user's own
words. Batch what reaches the user into one message with the exact question, your
recommendation and what keeps moving meanwhile.

Testing strategy is not designed here; a workstream that turns out to be a
test-architecture question routes to the user's test-architecture process.
