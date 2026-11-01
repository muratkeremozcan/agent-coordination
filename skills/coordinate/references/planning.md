# Planning workstreams and lanes

## Workstreams

One story, one workstream, one writable workspace, even when one owner holds several. Two
stories in one workstream make `completed` ambiguous across two pull requests, and two
branches sharing a directory means a rebase of the first lands underneath work in flight on
the second. An owner can hold several workstreams; give each its own workspace and split at a
clean point, with in-flight work committed first and no subagent writing.

For every workstream, record:

- the user's words, verbatim (`--user-words`), and any specification or artifact the user
  pointed at, with its digest (`--user-references`); later asks are appended with
  `amend-intent`, never folded into your own text;
- the story or spec reference and the brief (the ledger stores it and its digest);
- acceptance criteria, which never widen the user's words;
- the deliverable (`report`, `local-branch`, `published-branch`, `pr` or `landed`), and for a
  pull request the forge repository and target branch it must land against;
- dependencies, and at most one stack parent among them;
- the files and generated outputs it is expected to touch;
- permitted operations, a subset of the run's granted authority;
- the integration target.

Add workstreams in dependency order; ids are assigned in creation order and a forward
reference is refused.

## Dependencies and stacking

A dependency is satisfied only when that workstream is verified `completed` and still valid:
a completed child whose stack parent's head has since moved does not count, while a successor
that superseded it does. A stack parent is a different predicate: the child branches from the
parent's verified result head, which can exist while the parent is still in review. When the
parent's head changes, follow the child with `restack`. Assign the child with `--base-sha` equal to
that head; the ledger refuses a dispatch on any other base. A workstream can depend on
several workstreams for ordering but stacks on exactly one branch, and multi-parent
integration needs an explicit integration workstream. Dispatch in topological waves.

## Lanes

A plan of many stories can run as lanes when the profile's topology is `lanes`. Each lane is
a serial cluster: stories that regenerate the same output (a generator, a snapshot, a shared
fixture) go in one lane; a plain text overlap in a changelog or tracker does not force
serialization, because a rebase keeps both sides. Each cluster with no hard edge to another
is a candidate lane, capped by the profile's concurrency limit and machine load. Name an
ordering reason next to every constraint so a later rebalance can tell a real edge from
habit.

Before adding lanes mid-run, have one subagent do a read-only edge check of the remaining
backlog: declared dependencies, files and generated outputs per story, and the proposed
split. Rebalance by remaining stories as lanes drain.

A lane checkout is reused for the next story only at a verified boundary: the previous
workstream is terminal, its cleanup evidence proves the worktree clean and its head landed
or retained, no subagent is writing, and the new story gets a new workstream and assignment
generation. The ledger enforces this when the profile allows `verified-boundary` reuse.

An outgoing lane owner starts the next story's owner only when the profile says
`owner-with-grant` and the run grants `peer-create`. Owners report to the coordinator; the
coordinator is the only writer of run state.

## Decisions

A choice that gates work is a decision scoped to it: `open-decision --affected '[...]' --gates
'["dispatch"|"completion"|"landing"]'`, or the scope given when an owner's question is
recorded. Unaffected work keeps moving. A decision that belongs to the user is resolved only
with the user's own words; a tactical choice inside the run's authority is yours, recorded with
its reasoning. A deferred decision stays open and surfaces at its deferral time.

## The brief

Write briefs from [worker-brief.md](worker-brief.md), or [scout-brief.md](scout-brief.md) for
knowledge work. The process half is shared; the
story half (workspace, base, story text, precedent, decisions already made, known plan
defects) is yours to compose. Anticipating the two or three obvious ambiguities saves the
round trip a question costs.
