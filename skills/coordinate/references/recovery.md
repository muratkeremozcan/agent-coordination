# Recovery, supervision and handover

The ledger is current state; a compacted summary is not. After any compaction, wake or
restart:

1. `list` the state root and `snapshot` each active run; run `due` for each.
2. Claim each run. Claiming a run you already hold is a no-op. If another session holds it,
   check that session's liveness through its recorded binding (`herdr pane process-info` for
   `herdr-codex`, the host agent list for `codex-subagent`, the peer address lookup for
   `peer-session`).
3. A live holder hands over with `release-claim`, after which you claim normally. A dead
   holder is replaced with `claim --force --expected-holder <id> --reason ...
   --liveness-evidence ...`; the expected holder makes the takeover a compare-and-swap, so
   two successors cannot both win. When liveness is unknown, report the run and act on the
   user's instruction. A parked run needs the user's go-ahead before dispatch resumes.
4. Reconcile before rebriefing: actual owner sessions and their identities, workspace
   journal entries and receipts, branch heads, PR state and artifacts. An uncertain
   workspace acquisition is reconciled with `workspace_treehouse.py reconcile` before any
   retry.
5. Rebrief each owner in full, self-contained, from the stored bodies
   (`snapshot --request <id> --id <ws>`). An owner whose name survived a restart may hold none
   of its assignment.

A coordinator handing over says which session id holds each run so the successor can tell
a dead claim from a live one.

## Supervision loop

Every request carries an acknowledgement and a result deadline. `due` lists only what is
outstanding now:

- `ack-overdue` and `result-overdue` for the governing request of a working workstream that
  has no terminal result; a waiting workstream is governed by its wait instead;
- `wait-check` when a wait's bounded next check passes;
- `decision-deferral` and `decision-overdue`;
- `recovery-exhausted`, `conflicting-result`, `restack-required`, `missing-body`,
  `missing-disposition`, `landing-pending`, `dispatchable`, and `sweep`.

Handle each item, then either resolve what it reports or schedule its next look with
`ack-due --key <key> --next-check-at <bounded>`. A scheduled item stays quiet until then and
resurfaces on time, so the watcher never loops on it.

On a `sweep`, review the whole fleet: compare each working branch or PR head with the last
recorded result, re-check queued work for readiness, and record `record-sweep --changed
true|false`. The interval doubles while nothing changes, up to `heartbeat_max_minutes`. An
unchanged fleet is not news for the user. A new head with no reply is a reason to inspect and
ask for the report; it proves nothing by itself.

Before ending a turn with active workstreams, arm the watcher (see SKILL.md) or park the run.
`turnend-check` passes only for a running, live, recently cycling watcher of the current holder.

## Owners that go quiet

An idle owner is unverified. A session that filled its context compacts and returns to an
empty prompt holding none of the instruction it was executing, and a session that hit a
usage limit answers with the limit notice and goes idle. Work the ladder in order:

1. Peek through the binding's observe operation and record what you saw with `observe`.
   Check whether a message you sent is still unread.
2. If the brief already answers the owner's question, answer it in one line.
3. If the owner is confused or looping, interrupt (see runtimes.md) and send one corrective
   line.
4. If it is wedged, replace it in the same workspace after quiescence evidence: a fresh
   session with the stored brief, amendments and a progress note. This spends one attempt.
   Use a host's native resume only where the binding's semantics are verified.
5. When attempts are spent, open a decision and report the preserved work.

A low context reading is not wedging. An owner's claim that shared tooling is down comes from
one timed-out call: verify it from the authoritative source, and never restart a shared
service on an owner's claim. After three unchanged observations of the same silent owner, read
its transcript before any further ping. A usage limit is a `waiting` workstream with
`wait-on quota` and a bounded next check, recorded from the owner's warning or the limit
notice you observed; it is never a failure.

Unanswered results get one `report-recovery` request (`--root <request>`) asking for the
existing outcome without repeating the work. If that is also overdue, `due` reports
`recovery-exhausted`; open a decision. A brief that may never have arrived is a delivery
question: read the runtime before resending anything.

## Mutations and retries

Every mutation carries the holder's session id; a stale coordinator is refused. Pass
`--base-revision` when acting on a snapshot you did not lock, and prefer one `apply` batch
when one logical step touches several fields. Status repeats, identical result and event
retries and same-holder claims are idempotent no-ops, so a retry after a lost response is
safe. These checks stop accidental stale writes; a process with equal file access can still
bypass them.

## Bounded retries

`max_dispatch_attempts` caps new executions of a workstream: each move from `queued` or
`review-ready` to `working`, and each owner replacement, spends one. Resuming a declared wait
under the same owner continues the current attempt. When attempts are spent, open a decision.
`max_active_workstreams` caps concurrent owners; additional review layers run sequentially
inside an owner.
