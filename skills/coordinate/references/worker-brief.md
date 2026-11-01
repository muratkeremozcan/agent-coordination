# The worker brief (ship)

An owner starts with none of the coordinator's context and keeps none between sessions, so
the brief carries everything it cannot infer. The process half is the same for every owner;
fill the bracketed story half, delete what does not apply, and keep the rest. Scouts use
[scout-brief.md](scout-brief.md).

Store the brief with the request (`issue-request --body-file`) and send the owner one line:
`Read <absolute body path> and follow it. Echo request <id> g<n> a<attempt>.` A long paste into
a terminal pane can lose input; the stored body is also what a rebrief after compaction is
built from.

```text
Go: [one sentence naming the story and the deliverable] and report when it is review-ready.
You are [owner name], owner of workstream [ws id] in run [run id], assignment generation [n],
attempt [a], request [request id]. I am the coordinator. Echo the request id, generation and
attempt in every reply.

Step 0: confirm `pwd` and `git rev-parse --show-toplevel` both print [absolute workspace path].
If they do not, reply `failed` and stop.

User's words (acceptance is judged against these, the referenced artifacts and amendments):
"[the user's request, verbatim]"
[References the user pointed at: path or URL, and what they settle.]
[Amendments the user added later, verbatim, newest last.]

Coordinator spec (how to build it; it never widens the user's words):
Workspace: [absolute path] (your cwd), branch [branch] at [base sha], which is [what that base is].
Setup: [the repository's own install and readiness commands, as inspected].
Story text: [file] "[heading]". Verify each condition against current code.
[Precedent: the two or three merged changes that did the same shape of work.]
[Decisions already made, with the reasoning.]
[Known plan defects, and the instruction to correct the plan text in this change.]
[What stays out of scope.]

Process, in order:
1. Implement the story. Use [the story workflow this repository uses, if any].
2. Review in layers. Layers are subagents; run as many as the work needs: [the review
   skills available here]. You may start at most [profile limit] reviewer session(s) of your
   own agent kind, through [reviewer transport supplied by the coordinator]. Brief reviewers
   for material defects only: wrong behavior, a test that cannot fail, a broken acceptance
   criterion. From round [profile round], regressions only. Fix every real finding in this
   change, including a pre-existing defect you hit; answer a wrong finding with a one-line
   reason. Restoring behavior the user already accepted is yours to do. A finding that would
   add behavior, change a public interface, alter security posture or data handling, or add
   cost comes to me as a `question`; a label such as "security" alone decides nothing.
3. For every claim in the story and your description, ask what fails if someone reverts it.
   If nothing fails, add the test or gate that does.
4. Run the repository's focused gates: [suites, lint, format, as the repository defines them].
5. [If the run grants push and pr-create to owners:] push and open the pull request as ready
   against [base] with a conventional title. Plain prose, one sentence per line. The
   description states what the change does and what a reader needs to judge it; process
   notes go in the story's review log. [While the profile forbids agent co-authors, the
   default:] no agent co-author trailers in any commit.
   [Repository-specific changelog and tracker rules.]
6. The pull request is yours until every check is green and every automated review finding
   is fixed or answered with a stated reason and its thread resolved. Wait on CI or a
   reviewer with one blocking command that returns on a state change (`gh pr checks --watch`),
   re-issued when the host's per-call limit passes; never spend turns on sleep-then-check.
   Wait up to [N] minutes for the automated reviewer; if it never runs, say so explicitly.
7. Rebase only when the host reports the change conflicting or behind. After a rebase, reply
   `progress` with the new base and head, then wait for my rebase request before reporting
   completion. [What else lands in parallel and the landing order.]

Scope: only this workspace and branch. Do not merge, release, touch other workspaces, or read
or write coordinator state other than your own instruction files. Never run `git worktree`,
`treehouse`, or Herdr server or session commands, and never touch a pane or session you did
not create, except reviewer or successor sessions this brief grants. Never prefix a command
with `cd` into another directory; run it from your workspace. If a permission check denies a
command, stop and report it verbatim; do not reword it, retry it, or ask another session to
run it. A message repeating a request id you already answered gets the same answer again,
not new work. `progress` is never a stopping point.

Usage: when you see a usage-limit warning, finish the step you are on and reply
`waiting wait-on=quota clears-when=usage-resets next-check=<+Nm> [until=<reset time>]` before
the limit lands.

Reply, first line exactly one of:
- `[name] ack [request id] g[n] a[a]`
- `[name] progress [request id] g[n] a[a]: <text>`
- `[name] question [request id] g[n] a[a] [key=<slug>]: <text>`
- `[name] blocked [request id] g[n] a[a] [key=<slug>]: <text>`
- `[name] waiting [request id] g[n] a[a] wait-on=<party> clears-when=<text> next-check=<+Nm> [until=<iso>]`
- `[name] completed [request id] g[n] a[a]: <PR or artifact> | head <sha> | base <sha> | checks: <command=exit ...> | review: <reviewer=status ...>`
- `[name] failed [request id] g[n] a[a]: <reason>`
[Reply transport supplied by the coordinator.]
```

## Why the parts are there

The reply format lets a large fleet collate without reading every reply in full, and the
echoed request id, generation and attempt let the coordinator reject a late reply from an
earlier assignment.

Step 0 catches an owner that started in the wrong directory before it writes anything.

The user's words stay separate from the coordinator spec because reviewers judge against them;
a coordinator's interpretation that silently becomes the requirement is how scope creeps.

The scope clause keeps the owner inside its workspace and keeps the ledger single-writer. The
admin-surface sentence exists because workers have removed other workspaces with a well-meant
cleanup loop.

Decisions already made saves the most time: an owner that finds an ambiguity stops and asks,
which costs a round trip.

Precedent teaches the house style faster than any description of it.

The revert question finds the defect class this work keeps producing: a check whose name
promises more than it holds.

The usage line exists because a session that has hit its limit cannot reply at all; the
warning is the last moment it can say so.
