# Delivery, review and integration

## Findings

A real defect found by review, CI or the automated reviewer, in or around the workstream,
is fixed in that workstream's pull request before it lands, including a pre-existing defect
or a flake the owner hit. Reviewers are briefed for material defects only: wrong behavior, a
test that cannot fail, a broken acceptance criterion. From the profile's
`regressions_only_from_round`, review looks only for regressions against earlier fixes.

The implementation owner never decides a finding that would expand scope. Restoring behavior
the user already accepted is the owner's job, however much work it takes. A finding that would
add behavior, change a public interface, alter security posture or data handling, or add cost
comes to the coordinator, and from there to the user when it is the user's call. A label such
as "security" or "architecture" on a finding is evidence, never authority. A legitimate
re-plan is explicit: the coordinator records it as a decision with its reason.

## Review

Independent review is the profile's `required` list. Reviewers may be a test-architecture
review, a reviewer session of the owner's agent kind, subagent review layers, or an automated
PR reviewer, as the repository and profile configure. Each owner may start at most
`max_reviewer_sessions_per_owner` reviewer sessions. Every reviewer signals only processes it
started. A reviewer that returned nothing is a missing pass, recorded as `not-run` or
`unavailable`.

## Deliverables and publication

Each workstream names what it must produce, and completion needs the matching evidence:

| Deliverable | Completion evidence |
|---|---|
| `report` (scouts) | the report artifact and a full decision inventory |
| `local-branch` | the result head and coordinator verification on it |
| `published-branch` | `record-publication` with `{schema: 1, kind: branch, source: git, remote, remote_ref, oid, read_at}` naming the head |
| `pr` | `record-publication` with `adapters/forge_github.py pr-evidence` output |
| `landed` | as `pr`, then a proven landing before the run closes |

PR evidence passes only when every condition holds: the delivery contract's repository and
target branch, the bound PR, the workstream's branch and the result head; open, not a draft,
not conflicting; every required check passing from its required producer; nothing failing or
pending; no unresolved review thread; read within `evidence_max_age_minutes`. An empty check
list passes only for a workstream declared `--no-ci` with no required check. The ledger checks
the evidence object; it cannot prove the JSON came from GitHub, so the coordinator runs the
reader itself.

While the profile's `forbid_agent_coauthors` holds (the default), ship completion also needs
a coordinator verification entry from `adapters/workspace_treehouse.py check-coauthors
--repo-path <worktree> --base <base> --head <head>` with exit 0: no agent co-author trailer in
the range. Human co-authors pass.

Record completion with `update-workstream --status completed`, passing each check you ran
yourself as one `--verification` JSON object `{command, exit_code, head_sha}`; the
`check-coauthors` output is already such an entry.

## Pull requests

Owners run the repository's own focused gates, then push and open the pull request as ready
when the profile assigns those actions to owners and the run grants them. CI carries the full
chain. An owner owns its pull request until every check is green and every automated review
finding is fixed or answered with a stated reason and its thread resolved.

## Integration

Name the landing order when branches share files and land one at a time; branches behind
hold until their turn. Bind review and test evidence to the exact head and base. When the
base moves, follow the rebase sequence: `advance-base` with evidence, a `rebase` request, the
owner's result at the new base. A stacked child of a rewritten parent is followed with
`restack`; a completed child whose parent moved stops satisfying its dependents and cannot
land until a successor (`add-workstream --supersedes`) carries it.

Landing waits for the workstream's verified `completed`, needs the run's `merge` grant and
the workstream's permission (`amend-permissions` after a later approval), and is executed by
whoever the profile names. For a GitHub delivery:

1. Read `pr-evidence` immediately before merging; it must pass the publication predicate.
2. Merge with `gh pr merge <n> --match-head-commit <verified head> --squash|--merge|--rebase`
   as the repository does. Never `--admin` or `--delete-branch` unless the user names them.
3. If the outcome is queued or unknown, `record-landing-pending` and re-read; never merge again
   blindly.
4. Read `landing-evidence` and record the landing with both reads: the merged head is the
   verified head, the target contains the merge commit, and
   `pre_merge.read_at <= merged_at <= landing.read_at`.

An infrastructure failure before any test runs is rerun once, and the robustness gap that
let it fail is fixed in the current pull request. A rerun on a head that has since been
replaced is cancelled.

## Dispositions

Every completed ship workstream gets `record-disposition` before the run closes:
`left-for-review` (the user asked for a PR without a merge), `landed`, `not-landing`, or
`superseded` by a named successor, with the artifact and who follows it up. An instruction
the user already gave settles it; no new question is needed.

## Cleanup

Owners are retained through integration and fixes. Reviewer sessions are closed after their
evidence is accepted only when the profile says so and the run grants cleanup. Before a
squash or rebase landing's workspace is returned, keep the tested commits with
`workspace_treehouse.py retain-head`, which fetches the PR's pull ref from the verified remote
and writes a create-only ref; it grants no cleanup authority. Workspaces are returned only
through `workspace_treehouse.py release`, which requires `eligible` evidence in the ledger
(`update-workstream --cleanup-disposition eligible --cleanup-evidence` with
`{clean_worktree: true, landed_or_retained: true, detail, checked_at, worktree, head_sha}`, where
`detail` names the exact check you ran on the worktree yourself),
the `cleanup` grant, and a fresh preservation check: no uncommitted, untracked or valuable
ignored files, the head landed or kept on a retained ref, and no live owned process. A run may
finish with retained workspaces and explicit follow-up ownership.
