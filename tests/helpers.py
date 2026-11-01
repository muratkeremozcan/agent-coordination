"""Shared fixtures for driving the public command interfaces against temporary roots."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import _guard  # noqa: F401  (installs the real-state guard before anything else)
import coordinator_state as cs

REPO_ROOT = _guard.REPO_ROOT
DEFAULT_PROFILE = REPO_ROOT / "profiles" / "default.toml"
EXAMPLE_PROFILE = REPO_ROOT / "profiles" / "murat.example.toml"
SESSION = "coordinator-1"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def all_authority(granted: bool = True, **overrides: bool) -> list[dict]:
    return [
        {
            "action": action,
            "granted": overrides.get(action.replace("-", "_"), granted),
            "instruction": f"fixture instruction for {action}",
        }
        for action in cs.AUTHORITY_ACTIONS
    ]


def cli(root: Path | None, args: list[str], env: dict | None = None) -> tuple[int, str, str]:
    """Run the ledger CLI in-process with `--state-root` placed after the subcommand."""
    argv = list(args)
    if root is not None:
        argv[1:1] = ["--state-root", str(root)]
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cs.main(argv)
    return code, out.getvalue(), err.getvalue()


def ok(root: Path, args: list[str]) -> dict:
    code, out, err = cli(root, args)
    assert code == 0, f"exit {code}: {err}"
    return json.loads(out)


def fails(root: Path, args: list[str], code: int = 1) -> str:
    actual, _, err = cli(root, args)
    assert actual == code, f"expected exit {code}, got {actual}: {err}"
    return err


def iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(UTC) + delta).isoformat(timespec="seconds")


class Ledger:
    """A run in a temporary state root, with terse helpers for the common commands."""

    def __init__(self, root: Path, *, profile: Path = DEFAULT_PROFILE, authority: list[dict] | None = None):
        self.root = root
        record = ok(
            root,
            [
                "start",
                "--title",
                "Fixture run",
                "--intent",
                "Exercise the ledger contract.",
                "--coordination-reason",
                "two independent workstreams",
                "--delivery-authority",
                "ship and merge within the fixture repository",
                "--authority",
                json.dumps(authority if authority is not None else all_authority()),
                "--profile",
                str(profile),
                "--session-id",
                SESSION,
                "--coordinator-runtime",
                "codex",
            ],
        )
        self.run_id = record["run_id"]

    def args(self, command: str, *rest: str, session: str = SESSION) -> list[str]:
        return [command, "--run-id", self.run_id, "--session-id", session, *rest]

    def do(self, command: str, *rest: str, session: str = SESSION) -> dict:
        return ok(self.root, self.args(command, *rest, session=session))

    def refuse(self, command: str, *rest: str, session: str = SESSION, code: int = 1) -> str:
        return fails(self.root, self.args(command, *rest, session=session), code)

    def snapshot(self) -> dict:
        return ok(self.root, ["snapshot", "--run-id", self.run_id])

    def ws(self, ws_id: str) -> dict:
        return next(w for w in self.snapshot()["workstreams"] if w["id"] == ws_id)

    def add(
        self,
        kind: str = "scout",
        deps: list[str] | None = None,
        stack_parent: str | None = None,
        permitted: list[str] | None = None,
        title: str = "Workstream",
        *extra: str,
    ) -> dict:
        rest = [
            "--title",
            title,
            "--kind",
            kind,
            "--repository",
            "fixture-repo",
            "--scope",
            "fixture scope",
            "--brief",
            "fixture brief",
            "--acceptance-criteria",
            json.dumps(["it works"]),
            "--delivery-authority",
            "within the run's authority",
            "--permitted-operations",
            json.dumps(permitted if permitted is not None else ["local-edit"]),
            "--dependencies",
            json.dumps(deps or []),
        ]
        if stack_parent:
            rest += ["--stack-parent", stack_parent]
        if kind == "ship" and "--deliverable" not in extra:
            rest += ["--deliverable", "local-branch"]
        return self.do("add-workstream", *rest, *extra)

    def assign(self, ws_id: str, owner: str = "worker-1", *extra: str) -> dict:
        return self.do(
            "assign",
            "--id",
            ws_id,
            "--binding",
            "herdr-codex",
            "--owner-type",
            "collaborator",
            "--owner-id",
            owner,
            *extra,
        )

    def status(self, ws_id: str, status: str, *extra: str) -> dict:
        return self.do("update-workstream", "--id", ws_id, "--status", status, *extra)

    def issue(self, ws_id: str, request_id: str, purpose: str = "brief", *extra: str) -> dict:
        body = (
            []
            if purpose in {"brief", "report-recovery"} or "--body" in extra
            else ["--body", f"{purpose} for {request_id}"]
        )
        return self.do("issue-request", "--id", ws_id, "--request-id", request_id, "--purpose", purpose, *body, *extra)

    def result(self, ws_id: str, request_id: str = "req-1", *, issue: bool = True, **fields) -> dict:
        """Issue the request (unless told not to) and record the owner's reply to it."""
        if issue:
            self.issue(ws_id, request_id)
        return self.do("record-result", "--id", ws_id, "--result", json.dumps(result_payload(request_id, **fields)))

    def receipt(self, ws_id: str, **fields) -> dict:
        return self.do(
            "record-workspace", "--id", ws_id, "--receipt", json.dumps(receipt_payload(self.run_id, ws_id, **fields))
        )

    def ship_ready(self, ws_id: str, base: str = SHA_A, worktree: str | None = None) -> None:
        """Assign a ship workstream with a ready workspace receipt on `base`."""
        worktree = worktree or f"/fixture/worktrees/{ws_id}"
        self.assign(ws_id, "worker-1", "--worktree", worktree, "--branch", f"story-{ws_id}", "--base-sha", base)
        self.receipt(ws_id, path=worktree, branch=f"story-{ws_id}", expected_base_sha=base, observed_head_sha=base)

    def complete(
        self, ws_id: str, *, head: str = SHA_B, base: str | None = SHA_A, request_id: str = "req-done"
    ) -> dict:
        """Drive an assigned, dispatched workstream through result, review-ready and completed."""
        self.result(ws_id, request_id, head_sha=head, base_sha=base, generation=self.ws(ws_id)["assignment_generation"])
        self.status(ws_id, "review-ready")
        return self.status(ws_id, "completed", "--verification", verification(head))


def verification(head: str | None = SHA_B, **fields) -> str:
    """One passing coordinator check bound to `head`; it also stands in for the co-author check."""
    entry = {
        "command": "workspace_treehouse.py check-coauthors",
        "check": "agent-coauthors",
        "exit_code": 0,
        "head_sha": head,
    }
    entry.update(fields)
    return json.dumps(entry)


def result_payload(request_id: str = "req-1", **fields) -> dict:
    payload = {
        "request_id": request_id,
        "generation": 1,
        "owner": "worker-1",
        "source": {"kind": "reply-correlated"},
        "decision_inventory": [],
        "outcome": "completed",
        "summary": "done",
        "head_sha": SHA_B,
        "base_sha": SHA_A,
        "artifacts": ["https://example.invalid/pr/1"],
        "verification": [{"command": "python -m unittest", "exit_code": 0}],
        "review": [{"reviewer": "independent", "status": "passed"}],
    }
    payload.update(fields)
    return payload


def receipt_payload(run_id: str, ws_id: str, **fields) -> dict:
    payload = {
        "manager": "treehouse",
        "repository_common_dir": "/fixture/repo/.git",
        "path": f"/fixture/worktrees/{ws_id}",
        "lease_holder": f"{run_id}/{ws_id}",
        "lease_observed": False,
        "expected_base_sha": SHA_A,
        "observed_head_sha": SHA_A,
        "branch": f"story-{ws_id}",
        "provisioning": {"status": "ready", "problems": []},
        "created_by_run": True,
        "release_policy": "retain-until-verified-and-authorized",
    }
    payload.update(fields)
    return payload


def clean_evidence(worktree: str, head: str = SHA_B, **fields) -> str:
    evidence = {
        "clean_worktree": True,
        "landed_or_retained": True,
        "checked_at": iso(),
        "detail": "git status --porcelain empty; merge-base --is-ancestor head origin/main exit 0",
        "worktree": worktree,
        "head_sha": head,
    }
    evidence.update(fields)
    return json.dumps(evidence)


class TempRoot:
    """A temporary directory holding a state root, removed with the test."""

    def __init__(self, test):
        self._tmp = tempfile.TemporaryDirectory(prefix="coordinate-state-")
        test.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "state"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path: Path, files: dict[str, str] | None = None) -> str:
    """Create an independent disposable repository and return its HEAD commit."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for name, text in (files or {"README.md": "fixture\n", ".gitignore": ".deps-ready\nnode_modules/\n"}).items():
        (path / name).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "fixture base")
    return git(path, "rev-parse", "HEAD")
