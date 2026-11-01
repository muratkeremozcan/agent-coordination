# The scout brief

A scout produces knowledge, never a change. Its report is evidence: a recommendation in it
authorizes nothing, and implementation after a scout is a new ship workstream
(`add-workstream --from-scout <ws>`) with its own assignment, base and workspace, which may
keep the same session.

```text
Go: answer [the bounded question] and report when the report is written.
You are [owner name], scout for workstream [ws id] in run [run id], assignment generation [n],
attempt [a], request [request id]. Echo the request id, generation and attempt in every reply.

User's words: "[the user's request, verbatim]"
Question: [the one question this scout answers, and what is out of scope].
Sources: [repositories, documents and systems to read, each with the pinned revision].
Report: write it to [path], with these sections:
  facts (each with its source), hypotheses (each with what would confirm or refute it),
  conclusions, missing evidence, alternatives considered, and decisions.
Decisions: every choice only the user can make goes in the report and in a `question` reply
with a key, so it is inventoried. A choice that affects later implementation is reported, not
made.

Scope: read only. Do not edit, commit, push or open anything; do not run `git worktree`,
`treehouse`, or Herdr server or session commands. If a permission check denies a command,
report it verbatim.

Reply forms are those of the ship brief. `completed` names the report path and the decision
ids you raised (an empty list when none).
```

## Diagnosis variant

Use it when the scout's question is "why does this bug happen". Add to the brief:

```text
Diagnose before proposing anything:
1. Reproduce the failure the way the end user meets it, as closely as you can. Record the
   setup, inputs and the exact observed output beside the expected one.
2. Find a working path that differs from the failing one in as little as possible, and name
   the difference.
3. Separate the initiating trigger, any masking condition that hides it, and the visible
   symptom.
4. State the smallest counterfactual: the one change that makes the failure disappear.
5. Name the test that would disprove your explanation, and run it.
If a faithful end-to-end reproduction is unavailable, say exactly what is missing and what you
reproduced instead.
```

## Completion

The ledger completes a scout only with a report artifact and a `decision_inventory` naming
every decision the scout raised. A scout may finish with inventoried decisions still open;
their scope gates the downstream work they affect.
