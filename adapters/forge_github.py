#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Read-only GitHub evidence for publication and landing, through `gh api` only.

Two commands, each printing one evidence object (`forge-evidence.v1`):

- `pr-evidence` reads the PR a workstream published and everything its completion predicate
  needs: canonical repository and target branch identity, PR state, draft flag, head and base,
  mergeability, every required check (classic branch protection and repository rulesets,
  with the producing app where one is required), every check run and status context on the
  head with the latest run per producer, and unresolved review threads.
- `landing-evidence` reads a merged PR: merged flag and time, merge commit, the source head
  that was merged, and whether the target branch contains the merge commit (GitHub's compare
  endpoint, so no local Git is involved).

Nothing here writes anything, locally or remotely. Every read that fails, is denied or is
incomplete makes the evidence `uncertain` with the reason in `problems`; the ledger refuses
uncertain evidence. One documented response is not a failure: a readable branch whose classic
protection endpoint answers 404 `Branch not protected` has no classic protection.

The ledger checks this object's shape, identity, freshness, chronology and head binding. It
cannot prove the JSON came from GitHub; the coordinator runs this reader itself.

Usage:
    forge_github.py pr-evidence --repo owner/name --pr 12 --target main --expect-head <40-hex>
    forge_github.py landing-evidence --repo owner/name --pr 12 --target main --expect-head <40-hex>

`COORDINATE_GH_BIN` overrides the `gh` executable (tests use a fake).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime

GH_TIMEOUT_SECONDS = 60
MAX_PAGES = 50
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

PR_FACTS = """query PrFacts($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequest(number: $number) {
      number state isDraft headRefOid headRefName baseRefName mergeable mergeStateStatus
      merged mergedAt mergeCommit { oid }
    }
  }
}"""

CHECKS = """query Checks($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      commits(last: 1) { nodes { commit { oid statusCheckRollup { contexts(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          __typename
          ... on CheckRun { name status conclusion startedAt checkSuite { app { databaseId slug } } }
          ... on StatusContext { context state createdAt }
        }
      } } } } }
    }
  }
}"""

THREADS = """query Threads($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) { pageInfo { hasNextPage endCursor } nodes { isResolved } }
    }
  }
}"""


class GhError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _gh_bin() -> str:
    explicit = os.environ.get("COORDINATE_GH_BIN")
    if explicit:
        return explicit
    found = shutil.which("gh")
    if not found:
        raise GhError("gh is not installed")
    return found


def _gh(args: list[str]) -> tuple[int, object]:
    """Run `gh api -i ...` and return (HTTP status, parsed body)."""
    try:
        done = subprocess.run(
            [_gh_bin(), "api", "-i", *args],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(f"gh api {args[0]} could not run: {exc}") from exc
    head, _, body = done.stdout.replace("\r\n", "\n").partition("\n\n")
    match = re.match(r"HTTP/[\d.]+ (\d{3})", head)
    if not match:
        raise GhError(f"gh api {args[0]} returned no HTTP status: {done.stderr.strip() or done.stdout[:200]}")
    try:
        parsed = json.loads(body) if body.strip() else None
    except json.JSONDecodeError as exc:
        raise GhError(f"gh api {args[0]} returned unparseable JSON") from exc
    return int(match.group(1)), parsed


def _graphql(query: str, **variables: object) -> dict:
    args = ["graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is not None:
            args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    status, body = _gh(args)
    if status != 200 or not isinstance(body, dict) or body.get("errors") or "data" not in body:
        raise GhError(
            f"GraphQL query failed with HTTP {status}: {body.get('errors') if isinstance(body, dict) else body}"
        )
    return body["data"]


def _required_checks(repo: str, target: str, problems: list[str]) -> list[dict]:
    required: list[dict] = []
    status, body = _gh([f"repos/{repo}/branches/{target}/protection"])
    if status == 200 and isinstance(body, dict):
        for check in (body.get("required_status_checks") or {}).get("checks") or []:
            required.append({"context": check["context"], "app_id": check.get("app_id"), "source": "classic"})
    elif status == 404 and isinstance(body, dict) and body.get("message") == "Branch not protected":
        pass
    else:
        problems.append(f"classic branch protection unreadable (HTTP {status})")
    for page in range(1, MAX_PAGES + 1):
        status, body = _gh([f"repos/{repo}/rules/branches/{target}?per_page=100&page={page}"])
        if status != 200 or not isinstance(body, list):
            problems.append(f"repository rulesets unreadable (HTTP {status})")
            break
        for rule in body:
            if rule.get("type") == "required_status_checks":
                for check in (rule.get("parameters") or {}).get("required_status_checks") or []:
                    required.append(
                        {"context": check["context"], "app_id": check.get("integration_id"), "source": "ruleset"}
                    )
        if len(body) < 100:
            break
    else:
        problems.append("repository rulesets exceeded the page limit")
    return required


def _connection(page: object, path: tuple[str, ...]) -> object:
    """Walk to a connection, taking the first element of a list on the way; None when absent."""
    cursor = page
    for key in path:
        if isinstance(cursor, list):
            cursor = cursor[0] if cursor else None
        cursor = cursor.get(key) if isinstance(cursor, dict) else None
    return cursor


def _require_connection(connection: object, label: str) -> tuple[list[dict], bool, str | None]:
    """Nodes and paging of a connection; anything missing or mistyped is a failed read, never empty."""
    if not isinstance(connection, dict):
        raise GhError(f"{label} were not returned")
    nodes, info = connection.get("nodes"), connection.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(info, dict) or not isinstance(info.get("hasNextPage"), bool):
        raise GhError(f"{label} came back without nodes or page information")
    cursor = info.get("endCursor")
    if info["hasNextPage"] and not isinstance(cursor, str):
        raise GhError(f"{label} report another page without a cursor")
    return nodes, info["hasNextPage"], cursor


def _check_nodes(expect_head: str, problems: list[str], **variables) -> list[dict]:
    """Every check context on the PR's head commit, each page bound to that commit."""
    nodes: list[dict] = []
    after = None
    for _ in range(MAX_PAGES):
        data = _graphql(CHECKS, after=after, **variables)
        commit = _connection(data, ("repository", "pullRequest", "commits", "nodes", "commit"))
        if not isinstance(commit, dict) or not isinstance(commit.get("oid"), str):
            raise GhError("the checks query returned no head commit")
        if commit["oid"] != expect_head:
            raise GhError(f"checks were read for commit {commit['oid']}, not {expect_head}")
        if commit.get("statusCheckRollup") is None:
            return nodes  # GitHub reports no checks or statuses at all for this commit
        page, more, after = _require_connection(commit["statusCheckRollup"].get("contexts"), "check contexts")
        nodes.extend(page)
        if not more:
            return nodes
    problems.append("checks exceeded the page limit")
    return nodes


def _thread_nodes(problems: list[str], **variables) -> list[dict]:
    nodes: list[dict] = []
    after = None
    for _ in range(MAX_PAGES):
        data = _graphql(THREADS, after=after, **variables)
        page, more, after = _require_connection(
            _connection(data, ("repository", "pullRequest", "reviewThreads")), "review threads"
        )
        nodes.extend(page)
        if not more:
            return nodes
    problems.append("review threads exceeded the page limit")
    return nodes


def _normalize_run(node: dict) -> dict | None:
    if node.get("__typename") == "CheckRun":
        app = ((node.get("checkSuite") or {}).get("app") or {}).get("databaseId")
        done = node.get("status") == "COMPLETED"
        conclusion = (node.get("conclusion") or "").upper()
        if not done:
            state = "pending"
        elif conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            state = "passing"
        else:
            state = "failing"
        return {"name": node.get("name"), "app_id": app, "state": state, "at": node.get("startedAt")}
    if node.get("__typename") == "StatusContext":
        raw = (node.get("state") or "").upper()
        state = "passing" if raw == "SUCCESS" else "pending" if raw in {"PENDING", "EXPECTED"} else "failing"
        return {"name": node.get("context"), "app_id": None, "state": state, "at": node.get("createdAt")}
    return None


def _check_entries(nodes: list[dict]) -> list[dict]:
    """One entry per (name, producer); a passing run hides others only when provably newer.

    A run that is not passing wins unless a passing run of the same name and producer started
    strictly later. An undated or tied non-passing run is never hidden, so the result does not
    depend on the order GitHub lists the runs in.
    """
    groups: dict[tuple[str, object], list[dict]] = {}
    for node in nodes:
        run = _normalize_run(node)
        if run is not None:
            groups.setdefault((run["name"], run["app_id"]), []).append(run)
    entries = []
    for (name, app), runs in sorted(groups.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))):
        passing = sorted((r["at"] for r in runs if r["state"] == "passing" and r["at"]), reverse=True)
        blocking = [r for r in runs if r["state"] != "passing"]
        newest_pass = passing[0] if passing else None
        if not blocking:
            state = "passing"
        elif newest_pass and all(r["at"] and r["at"] < newest_pass for r in blocking):
            state = "passing"
        elif any(r["state"] == "pending" for r in blocking):
            state = "pending"
        else:
            state = "failing"
        entries.append({"name": name, "app_id": app, "state": state})
    return entries


def _match_required(required: list[dict], entries: list[dict]) -> tuple[list[str], list[str], list[str]]:
    passing, failing_or_pending, missing = [], [], []
    for req in required:
        any_app = req["app_id"] in (None, -1)
        matches = [e for e in entries if e["name"] == req["context"] and (any_app or e["app_id"] == req["app_id"])]
        label = req["context"] if any_app else f"{req['context']} (app {req['app_id']})"
        if not matches:
            missing.append(label)
        elif all(e["state"] == "passing" for e in matches):
            passing.append(label)
        else:
            failing_or_pending.append(label)
    return passing, failing_or_pending, missing


_PR_FIELDS = ("state", "isDraft", "headRefOid", "headRefName", "baseRefName", "mergeable", "mergeStateStatus")


def _pr_facts(owner: str, name: str, pr: int) -> dict:
    repository = _graphql(PR_FACTS, owner=owner, name=name, number=pr).get("repository")
    facts = repository.get("pullRequest") if isinstance(repository, dict) else None
    if not isinstance(facts, dict) or any(key not in facts for key in _PR_FIELDS):
        raise GhError(f"pull request {owner}/{name}#{pr} was not returned in full")
    return repository


def _identity(repo: str, target: str, problems: list[str]) -> str | None:
    status, body = _gh([f"repos/{repo}"])
    if status != 200 or not isinstance(body, dict):
        problems.append(f"repository {repo} unreadable (HTTP {status})")
        return None
    status, branch = _gh([f"repos/{repo}/branches/{target}"])
    if status != 200:
        problems.append(f"target branch {target} unreadable (HTTP {status})")
    return body.get("full_name")


def pr_evidence(repo: str, pr: int, target: str, expect_head: str) -> dict:
    problems: list[str] = []
    owner, name = repo.split("/", 1)
    evidence: dict = {"schema": 1, "kind": "pr", "source": "gh", "repo": repo, "pr": pr, "target": target}
    try:
        canonical = _identity(repo, target, problems)
        required = _required_checks(repo, target, problems) if canonical else []
        first = _pr_facts(owner, name, pr)
        facts = first["pullRequest"]
        check_nodes = _check_nodes(expect_head, problems, owner=owner, name=name, number=pr)
        threads = _thread_nodes(problems, owner=owner, name=name, number=pr)
        last = _pr_facts(owner, name, pr)["pullRequest"]
    except GhError as exc:
        problems.append(str(exc))
        evidence.update(read_at=_now(), uncertain=True, complete=False, problems=problems)
        return evidence
    entries = _check_entries(check_nodes)
    passing, not_passing, missing = _match_required(required, entries)
    head_stable = facts["headRefOid"] == last["headRefOid"]
    if not head_stable:
        problems.append("the PR head moved between reads")
    if facts["headRefOid"] != expect_head:
        problems.append(f"PR head {facts['headRefOid']} is not the expected head {expect_head}")
    complete = not any("page limit" in p or "unreadable" in p for p in problems)
    evidence.update(
        {
            "read_at": _now(),
            "repo": first.get("nameWithOwner") or canonical or repo,
            "state": facts["state"],
            "draft": facts["isDraft"],
            "head_oid": facts["headRefOid"],
            "head_ref": facts["headRefName"],
            "base_ref": facts["baseRefName"],
            "mergeable": facts["mergeable"],
            "merge_state": facts["mergeStateStatus"],
            "required": required,
            "checks": {
                "count": len(entries),
                "required_passing": passing,
                "required_not_passing": not_passing,
                "required_missing": missing,
                "failing": sorted(e["name"] for e in entries if e["state"] == "failing"),
                "pending": sorted(e["name"] for e in entries if e["state"] == "pending"),
            },
            "review_threads": {"unresolved": sum(1 for t in threads if not t.get("isResolved"))},
            "head_stable": head_stable,
            "complete": complete,
            "uncertain": not complete or not head_stable,
            "problems": problems,
        }
    )
    return evidence


def landing_evidence(repo: str, pr: int, target: str, expect_head: str) -> dict:
    problems: list[str] = []
    owner, name = repo.split("/", 1)
    evidence: dict = {"schema": 1, "kind": "landing", "source": "gh", "repo": repo, "pr": pr, "target": target}
    try:
        canonical = _identity(repo, target, problems)
        data = _pr_facts(owner, name, pr)
        facts = data["pullRequest"]
        merge_commit = (facts.get("mergeCommit") or {}).get("oid")
        contained = False
        if facts.get("merged") and merge_commit:
            status, body = _gh([f"repos/{repo}/compare/{merge_commit}...{target}"])
            if status == 200 and isinstance(body, dict):
                contained = body.get("status") in {"identical", "ahead"}
            else:
                problems.append(f"compare of the merge commit with {target} unreadable (HTTP {status})")
    except GhError as exc:
        problems.append(str(exc))
        evidence.update(read_at=_now(), uncertain=True, problems=problems)
        return evidence
    if facts["headRefOid"] != expect_head:
        problems.append(f"merged head {facts['headRefOid']} is not the expected head {expect_head}")
    evidence.update(
        {
            "read_at": _now(),
            "repo": data.get("nameWithOwner") or canonical or repo,
            "merged": bool(facts.get("merged")),
            "merged_at": facts.get("mergedAt"),
            "merge_commit": merge_commit,
            "source_head": facts["headRefOid"],
            "base_ref": facts["baseRefName"],
            "target_contains_merge_commit": contained,
            "uncertain": any("unreadable" in p for p in problems),
            "problems": problems,
        }
    )
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("pr-evidence", "landing-evidence"):
        sp = sub.add_parser(command)
        sp.add_argument("--repo", required=True)
        sp.add_argument("--pr", type=int, required=True)
        sp.add_argument("--target", required=True)
        sp.add_argument("--expect-head", required=True)
    args = parser.parse_args(argv)
    if not _REPO.match(args.repo) or not _FULL_SHA.match(args.expect_head):
        print("error: --repo must be owner/name and --expect-head a full 40-hex commit id", file=sys.stderr)
        return 2
    read = pr_evidence if args.command == "pr-evidence" else landing_evidence
    evidence = read(args.repo, args.pr, args.target, args.expect_head)
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 3 if evidence.get("uncertain") else 0


if __name__ == "__main__":
    sys.exit(main())
