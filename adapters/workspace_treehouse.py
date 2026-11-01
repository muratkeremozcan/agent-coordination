#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Thin TreeHouse workspace adapter: lease, verify, provision-check, and guarded return.

TreeHouse owns the worktree pool and its leases. This adapter owns what TreeHouse does not
prove: that a leased path is the expected managed worktree of the intended repository,
that the story branch sits on the pinned base commit, that the project is actually ready
after the branch is selected, and that a return never destroys unretained work.

Targets TreeHouse v2.0.0, whose contract is narrow: `get --lease --lease-holder <label>`
prints only the leased path on stdout (banners go to stderr), post-create hook failures
are logged without failing `get`, `return <path>` terminates lingering processes and
resets the worktree, and `status` prints a human table with no machine-readable form.
Lease observation is therefore reported as a capability gap.
guessed format.

Every acquisition is journaled under the state root before TreeHouse is called. A lost
response leaves the journal entry `uncertain`, and a later acquire for the same holder is
refused until `reconcile` records what was actually found, so a retry never allocates a
second lease blindly. Any provisioning failure leaves the workspace leased and `blocked`
with a recovery action; nothing here returns a workspace on its own initiative.

Usage:
    workspace_treehouse.py acquire --state-root ... --run-id ... --workstream ws-1 \\
        --repo <checkout> --base <ref> --branch <new-branch> \\
        --readiness-cmd '["npm","test","--","--version"]' [--lockfile package-lock.json]
    workspace_treehouse.py reconcile --state-root ... --run-id ... --workstream ws-1 \\
        (--path <found path> | --no-lease) --evidence "<what was checked>"
    workspace_treehouse.py check-release --state-root ... --run-id ... --workstream ws-1 \\
        --target-ref origin/main [--retained-ref ...] [--owned-process '{"pid":1,"started":"..."}']
    workspace_treehouse.py release --state-root ... --run-id ... --workstream ws-1 \\
        --session-id ... --target-ref origin/main [--retained-ref ...]
    workspace_treehouse.py reconcile-return --state-root ... --run-id ... --workstream ws-1 \\
        --found returned|still-leased --evidence "<what was checked>"
"""

import argparse
import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import coordinator_state as ledger  # noqa: E402
import process_identity as process_identity_module  # noqa: E402
from state_root import StateRootError, canonicalize, resolve_state_root, within_root  # noqa: E402

TREEHOUSE_BIN_ENV = "COORDINATE_TREEHOUSE_BIN"
TREEHOUSE_CONTRACT = "v2.0.0"
DEFAULT_GET_TIMEOUT = 900
DEFAULT_READINESS_TIMEOUT = 900
RELEASE_POLICY = "retain-until-verified-and-authorized"
_WS_ID_RE = re.compile(r"^ws-[0-9]+$")
CAPABILITY_GAPS = [
    "treehouse v2.0.0 has no machine-readable lease status; lease_observed stays false and "
    "the lease is inferred from `get --lease` succeeding with a validated path",
    "treehouse v2.0.0 has no machine-readable pool state after `return`; the return receipt "
    "records the exit status only",
    "unaccounted writers are detected only through the recorded owned processes",
]


class AdapterError(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _git(cwd: Path | str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if check and result.returncode != 0:
        raise AdapterError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result


def _treehouse_bin() -> str:
    return os.environ.get(TREEHOUSE_BIN_ENV) or "treehouse"


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


# --- journal ------------------------------------------------------------------------


def _holder(run_id: str, ws_id: str) -> str:
    if not ledger._RUN_ID_RE.match(run_id) or not _WS_ID_RE.match(ws_id):
        raise AdapterError(f"invalid run or workstream id {run_id!r}/{ws_id!r}")
    return f"{run_id}/{ws_id}"


def _workspaces_dir(root: Path, *, create: bool) -> Path:
    directory = within_root(root, root / "workspaces", purpose="workspace journal directory")
    if create:
        directory.mkdir(parents=True, exist_ok=True)
        within_root(root, directory, purpose="workspace journal directory")
    return directory


def _journal_path(root: Path, run_id: str, ws_id: str) -> Path:
    _holder(run_id, ws_id)
    return within_root(root, _workspaces_dir(root, create=False) / f"{run_id}__{ws_id}.json", purpose="journal")


@contextlib.contextmanager
def _holder_lock(root: Path, run_id: str, ws_id: str):
    """Serialize acquire, reconcile and release for one holder across processes.

    Two concurrent acquisitions for the same holder would both call `get --lease` and the
    journal would keep only one receipt, leaving an untracked lease. Contention fails fast.
    """
    _holder(run_id, ws_id)
    lock = within_root(root, _workspaces_dir(root, create=True) / f"{run_id}__{ws_id}.lock", purpose="holder lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AdapterError(f"another workspace operation for {run_id}/{ws_id} is in progress") from None
        yield
    finally:
        os.close(fd)


def _read_journal(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_journal(path: Path, entry: dict) -> None:
    ledger._atomic_write_text(path, json.dumps(entry, indent=2, sort_keys=True) + "\n")


# --- acquisition ----------------------------------------------------------------------


def _fetch_remote_base(repo: Path, base: str) -> str:
    """Refresh a remote-tracking base before it is pinned; refuse anything else."""
    full = _git(repo, "rev-parse", "--symbolic-full-name", base, check=False).stdout.strip()
    match = re.fullmatch(r"refs/remotes/([^/]+)/(.+)", full)
    if not match:
        raise AdapterError(f"--fetch-remote needs a remote-tracking base such as origin/main, not {base!r}")
    remote, branch = match.groups()
    fetched = _git(repo, "fetch", "--no-tags", remote, f"+refs/heads/{branch}:{full}", check=False)
    if fetched.returncode != 0:
        raise AdapterError(f"fetching {base} failed; nothing was reserved: {fetched.stderr.strip()}")
    return _now_iso()


def _primary_state(repo: Path) -> dict:
    return {
        "branch": _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip() or None,
        "head": _git(repo, "rev-parse", "HEAD", check=False).stdout.strip() or None,
        "dirty_entries": len(_git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines()),
    }


def _preflight(repo: Path, base: str, branch: str) -> tuple[Path, Path, str, int]:
    """Everything checkable before any side effect: repository, base commit, branch name."""
    if not repo.is_dir():
        raise AdapterError(f"repository {repo} does not exist")
    toplevel = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip())
    common = canonicalize(Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()))
    base_sha = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}").stdout.strip()
    if _git(repo, "check-ref-format", "--branch", branch, check=False).returncode != 0:
        raise AdapterError(f"{branch!r} is not a valid branch name")
    if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise AdapterError(f"branch {branch!r} already exists; a story branch must be new")
    dirty = _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines()
    return canonicalize(toplevel), common, base_sha, len(dirty)


def _validate_leased_path(raw: str, repo_top: Path, common: Path) -> tuple[Path, list[str]]:
    problems = []
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        return Path(), [f"expected exactly one path on stdout, got {len(lines)} line(s)"]
    path = Path(lines[0].strip())
    if not path.is_absolute():
        return path, [f"leased path {path} is not absolute"]
    if not path.is_dir():
        return path, [f"leased path {path} does not exist"]
    path = canonicalize(path)
    if path == repo_top or repo_top in path.parents:
        return path, [f"leased path {path} is the root checkout or inside it"]
    probe = _git(
        path, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir", "--git-common-dir", check=False
    )
    lines = probe.stdout.strip().splitlines()
    if probe.returncode != 0 or len(lines) != 3:
        return path, [f"leased path {path} is not a git worktree"]
    toplevel, git_dir, path_common = (canonicalize(Path(line)) for line in lines)
    if path_common != common:
        problems.append(f"leased path belongs to {path_common}, not the intended repository {common}")
    if toplevel != path:
        problems.append(f"leased path {path} is inside the worktree {toplevel}, not its top level")
    if git_dir == path_common:
        problems.append(f"leased path {path} is a main checkout, not a linked worktree")
    return path, problems


def _run_readiness(path: Path, argv: list[str], timeout: int, log_path: Path) -> dict:
    try:
        result = subprocess.run(
            argv, cwd=path, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
        exit_code, stdout, stderr = result.returncode, result.stdout, result.stderr
    except FileNotFoundError as exc:
        exit_code, stdout, stderr = 127, "", str(exc)
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = f"readiness check timed out after {timeout}s"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(f"$ {json.dumps(argv)}\n--- stdout\n{stdout}\n--- stderr\n{stderr}\n", encoding="utf-8")
    return {
        "command": argv,
        "exit_code": exit_code,
        "artifact": str(log_path),
        "artifact_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
    }


def acquire(
    root: Path,
    *,
    run_id: str,
    ws_id: str,
    repo: Path,
    base: str,
    branch: str,
    readiness_cmd: list[str],
    lockfiles: list[str],
    get_timeout: int = DEFAULT_GET_TIMEOUT,
    readiness_timeout: int = DEFAULT_READINESS_TIMEOUT,
    fetch_remote: bool = False,
) -> dict:
    holder = _holder(run_id, ws_id)
    if not readiness_cmd or not all(isinstance(a, str) and a for a in readiness_cmd):
        raise AdapterError("--readiness-cmd must be a non-empty JSON array of strings")
    with _holder_lock(root, run_id, ws_id):
        extras = {"base_fetched_at": _fetch_remote_base(canonicalize(repo), base) if fetch_remote else None}
        if canonicalize(repo).is_dir():
            top = Path(_git(canonicalize(repo), "rev-parse", "--show-toplevel").stdout.strip())
            extras["primary_checkout"] = {"repository": str(canonicalize(top)), **_primary_state(top)}
        receipt = _acquire_locked(
            root, holder, run_id, ws_id, repo, base, branch, readiness_cmd, lockfiles, get_timeout, readiness_timeout
        )
        # Facts gathered outside the lease itself, added to the receipt and its journal entry.
        journal = _journal_path(root, run_id, ws_id)
        entry = _read_journal(journal)
        if entry and entry.get("receipt"):
            entry["receipt"].update(extras)
            _write_journal(journal, entry)
        if isinstance(receipt, dict):
            receipt.update(extras)
        return receipt


def _acquire_locked(
    root: Path,
    holder: str,
    run_id: str,
    ws_id: str,
    repo: Path,
    base: str,
    branch: str,
    readiness_cmd: list[str],
    lockfiles: list[str],
    get_timeout: int,
    readiness_timeout: int,
) -> dict:
    journal = _journal_path(root, run_id, ws_id)
    existing = _read_journal(journal)
    if existing is not None:
        raise AdapterError(
            f"holder {holder} already has a {existing['state']!r} workspace entry; reconcile or "
            "release it before acquiring again"
        )
    if shutil.which(_treehouse_bin()) is None:
        raise AdapterError(f"treehouse executable {_treehouse_bin()!r} was not found; nothing was reserved")
    repo_top, common, base_sha, root_dirty = _preflight(canonicalize(repo), base, branch)
    entry = {
        "lease_holder": holder,
        "state": "uncertain",
        "intent": {
            "repository": str(repo_top),
            "repository_common_dir": str(common),
            "base": base,
            "expected_base_sha": base_sha,
            "branch": branch,
            "recorded_at": _now_iso(),
        },
        "receipt": None,
    }
    # Exclusive creation is the reservation: it either lands whole or finds an entry.
    if not ledger._link_new_record(journal.parent, journal, entry):
        raise AdapterError(f"holder {holder} already has a workspace entry")

    try:
        return _lease_and_provision(
            root,
            journal,
            entry,
            holder,
            run_id,
            ws_id,
            repo_top,
            common,
            base_sha,
            branch,
            root_dirty,
            readiness_cmd,
            lockfiles,
            get_timeout,
            readiness_timeout,
        )
    except Exception as exc:
        # Whatever failed after the reservation, the journal keeps every fact known so far,
        # including a validated path, and the holder stays reserved until reconciled.
        receipt = entry.get("receipt") or _skeleton_receipt(holder, common, base_sha, branch, root_dirty)
        receipt["provisioning"]["problems"].append(f"adapter error after reservation: {exc}")
        if receipt.get("path"):
            receipt["provisioning"]["status"] = "blocked"
            entry["state"] = "blocked"
        else:
            receipt["provisioning"]["status"] = "uncertain"
            entry["state"] = "uncertain"
        entry["receipt"] = receipt
        _write_journal(journal, entry)
        if isinstance(exc, AdapterError):
            raise
        raise AdapterError(f"acquisition for {holder} failed after reservation: {exc}") from exc


def _skeleton_receipt(holder: str, common: Path, base_sha: str, branch: str, root_dirty: int) -> dict:
    return {
        "manager": "treehouse",
        "manager_contract": TREEHOUSE_CONTRACT,
        "repository_common_dir": str(common),
        "path": "",
        "lease_holder": holder,
        "lease_observed": False,
        "expected_base_sha": base_sha,
        "observed_head_sha": "",
        "branch": branch,
        "provisioning": {"status": "uncertain", "problems": [], "readiness": None, "lockfiles": {}},
        "created_by_run": True,
        "release_policy": RELEASE_POLICY,
        "root_dirty_entries": root_dirty,
        "primary_checkout": None,
        "base_fetched_at": None,
        "capability_gaps": CAPABILITY_GAPS,
        "stderr_tail": "",
        "acquired_at": _now_iso(),
    }


def _lease_and_provision(
    root: Path,
    journal: Path,
    entry: dict,
    holder: str,
    run_id: str,
    ws_id: str,
    repo_top: Path,
    common: Path,
    base_sha: str,
    branch: str,
    root_dirty: int,
    readiness_cmd: list[str],
    lockfiles: list[str],
    get_timeout: int,
    readiness_timeout: int,
) -> dict:
    receipt = _skeleton_receipt(holder, common, base_sha, branch, root_dirty)
    entry["receipt"] = receipt
    try:
        got = subprocess.run(
            [_treehouse_bin(), "get", "--lease", "--lease-holder", holder],
            cwd=repo_top,
            capture_output=True,
            text=True,
            timeout=get_timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        receipt["provisioning"]["problems"].append(
            f"treehouse get timed out after {get_timeout}s; a lease may exist for {holder}. "
            "Inspect `treehouse status` and run reconcile before any retry"
        )
        entry["receipt"] = receipt
        _write_journal(journal, entry)
        return receipt
    receipt["stderr_tail"] = _tail(got.stderr)
    if got.returncode != 0:
        receipt["provisioning"]["problems"].append(
            f"treehouse get exited {got.returncode}; a lease may or may not exist for {holder}. "
            "Inspect `treehouse status` and run reconcile before any retry"
        )
        entry["receipt"] = receipt
        _write_journal(journal, entry)
        return receipt
    path, problems = _validate_leased_path(got.stdout, repo_top, common)
    receipt["path"] = str(path) if str(path) != "." else ""
    if problems:
        receipt["provisioning"]["problems"].extend(problems)
        if receipt["path"]:
            # A path came back, so the lease exists; keep it and block.
            receipt["provisioning"]["status"] = "blocked"
            entry["state"] = "blocked"
        entry["receipt"] = receipt
        _write_journal(journal, entry)
        return receipt
    entry["state"] = "leased"
    _provision(receipt, path, base_sha, branch, readiness_cmd, lockfiles, readiness_timeout, root, run_id, ws_id)
    entry["state"] = "ready" if receipt["provisioning"]["status"] == "ready" else "blocked"
    entry["receipt"] = receipt
    _write_journal(journal, entry)
    return receipt


def _provision(
    receipt: dict,
    path: Path,
    base_sha: str,
    branch: str,
    readiness_cmd: list[str],
    lockfiles: list[str],
    readiness_timeout: int,
    root: Path,
    run_id: str,
    ws_id: str,
) -> None:
    provisioning = receipt["provisioning"]
    leftovers = _git(path, "status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines()
    if leftovers:
        provisioning["status"] = "blocked"
        provisioning["problems"].append(
            f"leased worktree has {len(leftovers)} uncommitted or untracked entr(y/ies) before "
            "branch selection; retained for inspection"
        )
        receipt["observed_head_sha"] = _git(path, "rev-parse", "HEAD").stdout.strip()
        return
    switched = _git(path, "switch", "--no-track", "-c", branch, base_sha, check=False)
    head = _git(path, "rev-parse", "HEAD", check=False).stdout.strip()
    current = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
    receipt["observed_head_sha"] = head
    if switched.returncode != 0:
        provisioning["problems"].append(f"could not create {branch} at {base_sha}: {switched.stderr.strip()}")
    if head != base_sha:
        provisioning["problems"].append(f"HEAD {head} does not match the pinned base {base_sha}")
    if current != branch:
        provisioning["problems"].append(f"checked-out branch is {current or '(detached)'}, not {branch}")
    for name in lockfiles:
        lock = path / name
        if lock.is_file():
            provisioning["lockfiles"][name] = hashlib.sha256(lock.read_bytes()).hexdigest()
        else:
            provisioning["problems"].append(f"lockfile {name} is missing on the story branch")
    if provisioning["problems"]:
        provisioning["status"] = "blocked"
        return
    log_path = within_root(root, journal_dir(root) / f"{run_id}__{ws_id}.readiness.log", purpose="readiness log")
    readiness = _run_readiness(path, readiness_cmd, readiness_timeout, log_path)
    provisioning["readiness"] = readiness
    if readiness["exit_code"] != 0:
        provisioning["status"] = "blocked"
        provisioning["problems"].append(
            f"readiness check exited {readiness['exit_code']}; hook output is in stderr_tail and "
            f"the check output in {readiness['artifact']}"
        )
        return
    provisioning["status"] = "ready"


def journal_dir(root: Path) -> Path:
    return _workspaces_dir(root, create=True)


def reconcile(root: Path, *, run_id: str, ws_id: str, path: Path | None, evidence: str) -> dict:
    """Record what an operator found after an uncertain acquisition."""
    if not evidence.strip():
        raise AdapterError("--evidence must say what was checked")
    with _holder_lock(root, run_id, ws_id):
        return _reconcile_locked(root, run_id, ws_id, path, evidence)


def _reconcile_locked(root: Path, run_id: str, ws_id: str, path: Path | None, evidence: str) -> dict:
    journal = _journal_path(root, run_id, ws_id)
    entry = _read_journal(journal)
    if entry is None or entry["state"] != "uncertain":
        raise AdapterError("reconcile applies only to an uncertain acquisition")
    intent = entry["intent"]
    receipt = entry["receipt"] or _skeleton_receipt(
        entry["lease_holder"], Path(intent["repository_common_dir"]), intent["expected_base_sha"], intent["branch"], 0
    )
    reconciliation = {"evidence": evidence, "at": _now_iso()}
    if path is None:
        entry["state"] = "no-lease"
        reconciliation["found"] = "no lease"
        entry["reconciliation"] = reconciliation
        _write_journal(journal, entry)
        # The journal entry is closed so a fresh acquire may proceed.
        journal.rename(journal.with_name(journal.name + f".no-lease-{int(datetime.now().timestamp())}"))
        return entry
    leased, problems = _validate_leased_path(
        str(path), Path(intent["repository"]), Path(intent["repository_common_dir"])
    )
    receipt.update(
        {
            "path": str(leased),
            "observed_head_sha": _git(leased, "rev-parse", "HEAD", check=False).stdout.strip() if not problems else "",
        }
    )
    receipt["provisioning"]["status"] = "blocked"
    receipt["provisioning"]["problems"].append(
        "reconciled after an uncertain acquisition; provisioning was not completed by the adapter. "
        "Verify the branch and readiness, or release the lease through the authorized path"
    )
    receipt["provisioning"]["problems"].extend(problems)
    reconciliation["found"] = str(leased)
    entry["state"] = "blocked"
    entry["receipt"] = receipt
    entry["reconciliation"] = reconciliation
    _write_journal(journal, entry)
    return entry


# --- release ------------------------------------------------------------------------------


# Process identity lives in scripts/process_identity.py so the watcher shares the same pin.
PROCESS_IDENTITY_FORMAT = process_identity_module.PROCESS_IDENTITY_FORMAT
ProcessProbeError = process_identity_module.ProcessProbeError
process_identity = process_identity_module.process_identity


def _process_started(pid: int) -> str | None:
    return process_identity_module.process_started(pid)


def _matches_any(relative: str, patterns: list[str]) -> bool:
    """Match a file or any of its parent directories against the retention globs."""
    parts = relative.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    return any(fnmatch.fnmatch(candidate, pattern) for candidate in candidates for pattern in patterns)


def _independent_ref(
    path: Path, ref: str, own_branch: str, problems: list[str], label: str, *, allow_local_branch: bool
) -> bool:
    full = _git(path, "rev-parse", "--symbolic-full-name", ref, check=False).stdout.strip()
    if ref == "HEAD" or full == "HEAD" or ref == own_branch or full == f"refs/heads/{own_branch}":
        problems.append(f"{label} {ref!r} is the workspace's own branch or HEAD, which the return resets")
        return False
    if not allow_local_branch and full.startswith("refs/heads/"):
        problems.append(
            f"{label} {ref!r} is a local branch; keep the original head on a remote-tracking ref or a ref "
            "outside refs/heads/"
        )
        return False
    return True


def check_release(
    receipt: dict,
    *,
    target_ref: str,
    retained_ref: str | None,
    owned_processes: list[dict],
    valuable_ignored: list[str],
) -> dict:
    """Preservation report: may this workspace be returned without losing anything?"""
    problems: list[str] = []
    checks: dict[str, object] = {}
    path = Path(receipt["path"])
    if not path.is_dir():
        return {"releasable": False, "problems": [f"workspace {path} no longer exists"], "checks": checks}
    if canonicalize(path) != Path(receipt["path"]):
        problems.append(f"workspace path now resolves to {canonicalize(path)}, not {receipt['path']}")
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False).stdout.strip()
    if not common or canonicalize(Path(common)) != Path(receipt["repository_common_dir"]):
        problems.append("workspace no longer belongs to the recorded repository")
        return {"releasable": False, "problems": problems, "checks": checks}
    branch = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
    checks["branch"] = branch
    if branch != receipt["branch"]:
        problems.append(f"checked-out branch is {branch or '(detached)'}, not {receipt['branch']}")
    status = _git(path, "status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines()
    checks["uncommitted_or_untracked"] = len(status)
    if status:
        problems.append(f"{len(status)} uncommitted or untracked entr(y/ies) would be destroyed")
    # List every ignored file individually: `status --ignored` collapses an ignored directory
    # into one entry and quotes unusual names, which hid valuable files inside it.
    ignored = [
        name
        for name in _git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").stdout.split("\0")
        if name
    ]
    valuable = [p for p in ignored if _matches_any(p, valuable_ignored)]
    checks["valuable_ignored"] = valuable
    if valuable:
        problems.append(f"valuable ignored artifact(s) would be destroyed: {valuable}")
    head = _git(path, "rev-parse", "HEAD").stdout.strip()
    checks["head"] = head
    # A ref that lives only in this workspace proves nothing: the return resets it.
    own_branch = receipt["branch"]
    target_ok = _independent_ref(path, target_ref, own_branch, problems, "target_ref", allow_local_branch=True)
    retained_ok = retained_ref is not None and _independent_ref(
        path, retained_ref, own_branch, problems, "retained_ref", allow_local_branch=False
    )
    retained_by = None
    if target_ok and _git(path, "merge-base", "--is-ancestor", head, target_ref, check=False).returncode == 0:
        retained_by = f"ancestor of {target_ref}"
    elif retained_ok:
        ref_sha = _git(
            path, "rev-parse", "--verify", "--quiet", f"{retained_ref}^{{commit}}", check=False
        ).stdout.strip()
        if ref_sha and _git(path, "merge-base", "--is-ancestor", head, ref_sha, check=False).returncode == 0:
            retained_by = f"contained in {retained_ref} ({ref_sha})"
    checks["retained_by"] = retained_by
    if retained_by is None:
        problems.append(
            f"head {head} is neither an ancestor of {target_ref} nor contained in a retained ref; "
            "a squash merge needs the original head kept on a verified ref"
        )
    live, unknown = [], []
    for proc in owned_processes:
        if proc.get("identity_format") != PROCESS_IDENTITY_FORMAT:
            # A string captured under another time zone or locale cannot prove a recycled pid.
            unknown.append(proc.get("pid"))
            problems.append(
                f"owned process {proc.get('pid')} was recorded without the {PROCESS_IDENTITY_FORMAT} "
                "identity format; re-capture it with process-identity or reconcile it by hand"
            )
            continue
        try:
            started = _process_started(int(proc["pid"]))
        except ProcessProbeError as exc:
            unknown.append(proc["pid"])
            problems.append(f"owned process {proc['pid']} could not be probed ({exc}); its absence is unproven")
            continue
        if started is not None and started == proc.get("started"):
            live.append(proc["pid"])
    checks["live_owned_processes"] = live
    checks["unknown_owned_processes"] = unknown
    recorded = receipt.get("primary_checkout")
    if recorded and Path(recorded["repository"]).is_dir():
        # A diagnostic only: another authorized session may change the primary checkout.
        now = _primary_state(Path(recorded["repository"]))
        checks["primary_checkout_drift"] = {
            k: {"at_acquire": recorded[k], "now": now[k]} for k in now if now[k] != recorded[k]
        }
    if live:
        problems.append(f"owned process(es) {live} are still running in the workspace")
    return {"releasable": not problems, "problems": problems, "checks": checks}


# Agent attribution, after firstmate's bin/fm-git-strip-ai-trailers.sh (MIT, commit 30d6d15):
# an address is listed only when a runtime was seen emitting it, so a human co-author who works
# at a vendor, or whose name merely contains "ai", is kept.
AGENT_COAUTHOR_EMAILS = (
    "noreply@anthropic.com",
    "noreply@openai.com",
    "copilot@github.com",
    "noreply@commandcode.ai",
    "noreply@snowflake.com",
)
AGENT_COAUTHOR_EMAIL_PREFIXES = ("cursoragent@",)
AGENT_COAUTHOR_NAMES = {
    "cursor",
    "cursor agent",
    "claude",
    "claude code",
    "github copilot",
    "copilot",
    "codex",
    "chatgpt",
    "gemini",
    "google gemini",
    "grok",
    "openai",
    "commandcodebot",
    "snowflake coco",
    "cortex code",
}


def _agent_coauthor(line: str) -> bool:
    raw = line.strip()
    if not raw.lower().startswith("co-authored-by:"):
        return False
    rest = raw.split(":", 1)[1].strip()
    name, email = rest, ""
    match = re.match(r"(.*?)<([^>]*)>", rest)
    if match:
        name, email = match.group(1).strip(), match.group(2).strip().lower()
    if email in AGENT_COAUTHOR_EMAILS or email.startswith(AGENT_COAUTHOR_EMAIL_PREFIXES):
        return True
    return name.lower() in AGENT_COAUTHOR_NAMES


def check_coauthors(repo_path: Path, base: str, head: str) -> dict:
    """A coordinator verification entry: no agent co-author trailer in base..head."""
    resolved = _git(repo_path, "rev-parse", "--verify", f"{head}^{{commit}}").stdout.strip()
    log = _git(repo_path, "log", "--format=%H%x00%B%x1e", f"{base}..{resolved}").stdout
    offending = []
    for record in filter(None, (r.strip() for r in log.split("\x1e"))):
        sha, _, body = record.partition("\x00")
        offending += [f"{sha[:12]}: {line.strip()}" for line in body.splitlines() if _agent_coauthor(line)]
    return {
        "command": f"git log --format=%B {base}..{resolved}",
        "check": "agent-coauthors",
        "exit_code": 1 if offending else 0,
        "head_sha": resolved,
        "offending": offending,
    }


def retain_head(
    root: Path,
    *,
    run_id: str,
    ws_id: str,
    repo_path: Path,
    remote: str,
    expected_remote_url: str,
    pr: int,
    expect_head: str,
) -> dict:
    """Keep a PR's original tested commits reachable on a create-only ref.

    Needs the run's cleanup grant, because it exists only to make cleanup safe, and grants
    nothing: it writes no ledger state and never makes a workspace eligible for return. The
    remote must be the workstream's forge repository, the pull ref must hold the verified
    head, and an existing ref holding anything else is left untouched.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", expect_head):
        raise AdapterError("--expect-head must be a full 40-hex commit id")
    record = ledger._load_snapshot(root, run_id)
    if record["status"] != "active":
        raise AdapterError(f"run {run_id} is not active")
    if "cleanup" not in ledger._granted_actions(record):
        raise AdapterError("the run withholds cleanup; retained refs are written only under that grant")
    ws = ledger._find_workstream(record, ws_id)
    if ws.get("verified_head") != expect_head:
        raise AdapterError(f"{expect_head} is not the verified head of {ws_id} ({ws.get('verified_head')!r})")
    contract = ws.get("delivery_contract") or {}
    forge_repo = contract.get("repo")
    if not forge_repo:
        raise AdapterError(f"{ws_id} records no forge repository")
    if not re.search(rf"[:/]{re.escape(forge_repo)}(\.git)?/?$", expected_remote_url, re.IGNORECASE):
        raise AdapterError(f"remote URL {expected_remote_url!r} does not name {forge_repo}")
    repo_path = canonicalize(repo_path)
    common = canonicalize(
        Path(_git(repo_path, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    )
    recorded_common = (ws.get("workspace") or {}).get("repository_common_dir")
    if recorded_common != str(common):
        raise AdapterError(f"{repo_path} is not the workstream's repository ({recorded_common})")
    actual_url = _git(repo_path, "remote", "get-url", remote, check=False).stdout.strip()
    if actual_url != expected_remote_url:
        raise AdapterError(f"remote {remote!r} points at {actual_url!r}, not {expected_remote_url!r}")
    pull_ref = f"refs/pull/{int(pr)}/head"
    listed = _git(repo_path, "ls-remote", remote, pull_ref, check=False).stdout.split()
    if not listed or listed[0] != expect_head:
        raise AdapterError(f"{pull_ref} on {remote} holds {listed[0] if listed else 'nothing'}, not {expect_head}")
    fetched = _git(repo_path, "fetch", "--no-tags", "--no-write-fetch-head", remote, pull_ref, check=False)
    if (
        fetched.returncode != 0
        or _git(repo_path, "cat-file", "-e", f"{expect_head}^{{commit}}", check=False).returncode
    ):
        raise AdapterError(f"could not fetch {expect_head} from {remote}: {fetched.stderr.strip()}")
    ref = (
        f"refs/coordinate/retained/{run_id}/{ws_id}/"
        f"g{ws['assignment_generation']}-a{ws['dispatch_attempts']}-{expect_head}"
    )
    current = _git(repo_path, "rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()
    if current == expect_head:
        return {"ref": ref, "sha": expect_head, "created": False}
    if current:
        raise AdapterError(f"{ref} already holds {current}; it is left unchanged")
    created = _git(repo_path, "update-ref", ref, expect_head, "0" * 40, check=False)
    if created.returncode != 0:
        raise AdapterError(f"could not create {ref}: {created.stderr.strip()}")
    return {"ref": ref, "sha": expect_head, "created": True}


def release(
    root: Path,
    *,
    run_id: str,
    ws_id: str,
    session_id: str,
    target_ref: str,
    retained_ref: str | None,
    owned_processes: list[dict],
    valuable_ignored: list[str],
) -> dict:
    """Return a workspace only when the ledger authorizes it and preservation holds."""
    with _holder_lock(root, run_id, ws_id):
        return _release_locked(
            root, run_id, ws_id, session_id, target_ref, retained_ref, owned_processes, valuable_ignored
        )


RELEASABLE_STATES = {"ready", "blocked"}


def _release_locked(
    root: Path,
    run_id: str,
    ws_id: str,
    session_id: str,
    target_ref: str,
    retained_ref: str | None,
    owned_processes: list[dict],
    valuable_ignored: list[str],
) -> dict:
    journal = _journal_path(root, run_id, ws_id)
    entry = _read_journal(journal)
    if entry is None or entry.get("receipt") is None or not entry["receipt"].get("path"):
        raise AdapterError(f"no leased workspace recorded for {run_id}/{ws_id}")
    if entry["state"] == "returned":
        raise AdapterError("workspace was already returned")
    if entry["state"] not in RELEASABLE_STATES:
        raise AdapterError(
            f"workspace state is {entry['state']!r}; reconcile current ownership before any return "
            "(reconcile-return after an interrupted or failed return)"
        )
    record = ledger._load_snapshot(root, run_id)
    holder = record.get("claimed_by")
    if not holder or holder["session_id"] != session_id:
        raise AdapterError(f"session {session_id!r} does not hold run {run_id}")
    if "cleanup" not in ledger._granted_actions(record):
        raise AdapterError("the run's authority withholds cleanup; the workspace is retained")
    ws = ledger._find_workstream(record, ws_id)
    if ws["status"] not in ledger.TERMINAL_WORKSTREAM_STATUSES:
        raise AdapterError(f"workstream {ws_id} is {ws['status']!r}; its owner may still be writing")
    if ws.get("cleanup_disposition") != "eligible":
        raise AdapterError(
            f"workstream {ws_id} cleanup disposition is {ws.get('cleanup_disposition')!r}; "
            "record verified eligible evidence in the ledger first"
        )
    if (ws.get("workspace") or {}).get("path") != entry["receipt"]["path"]:
        raise AdapterError("the ledger's workspace receipt does not match this lease")
    report = check_release(
        entry["receipt"],
        target_ref=target_ref,
        retained_ref=retained_ref,
        owned_processes=owned_processes,
        valuable_ignored=valuable_ignored,
    )
    entry["release_check"] = {**report, "at": _now_iso()}
    if not report["releasable"]:
        _write_journal(journal, entry)
        raise AdapterError("workspace retained: " + "; ".join(report["problems"]))
    # Record the intent durably first: a crash or lost response after this point leaves the
    # entry `returning`, which refuses another return until ownership is reconciled.
    entry["return_intent"] = {"previous_state": entry["state"], "at": _now_iso()}
    entry["state"] = "returning"
    _write_journal(journal, entry)
    returned = subprocess.run(
        [_treehouse_bin(), "return", entry["receipt"]["path"]],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    entry["return"] = {
        "exit_code": returned.returncode,
        "stderr_tail": _tail(returned.stderr),
        "at": _now_iso(),
    }
    entry["state"] = "returned" if returned.returncode == 0 else "return-failed"
    _write_journal(journal, entry)
    if returned.returncode != 0:
        raise AdapterError(f"treehouse return exited {returned.returncode}; the lease state is uncertain")
    return entry


def reconcile_return(root: Path, *, run_id: str, ws_id: str, found: str, evidence: str) -> dict:
    """Record what an operator found after an interrupted or failed return."""
    if found not in {"returned", "still-leased"}:
        raise AdapterError("--found must be 'returned' or 'still-leased'")
    if not evidence.strip():
        raise AdapterError("--evidence must say what was checked")
    with _holder_lock(root, run_id, ws_id):
        journal = _journal_path(root, run_id, ws_id)
        entry = _read_journal(journal)
        if entry is None or entry["state"] not in {"returning", "return-failed"}:
            raise AdapterError("reconcile-return applies only after an interrupted or failed return")
        entry["return_reconciliation"] = {"found": found, "evidence": evidence, "at": _now_iso()}
        # A lease found intact goes back to blocked so its state is re-verified before release.
        entry["state"] = "returned" if found == "returned" else "blocked"
        _write_journal(journal, entry)
        return entry


# --- CLI ------------------------------------------------------------------------------------


def _json_list(raw: str, flag: str) -> list:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"{flag} must be valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise AdapterError(f"{flag} must be a JSON array")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="workspace_treehouse.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--state-root", default=None)
        sp.add_argument("--run-id", required=True)
        sp.add_argument("--workstream", required=True)

    a = sub.add_parser("acquire")
    common(a)
    a.add_argument("--repo", required=True)
    a.add_argument("--base", required=True)
    a.add_argument("--branch", required=True)
    a.add_argument("--readiness-cmd", required=True, help="JSON argv run in the worktree")
    a.add_argument("--lockfile", action="append", default=[])
    a.add_argument("--get-timeout", type=int, default=DEFAULT_GET_TIMEOUT)
    a.add_argument("--readiness-timeout", type=int, default=DEFAULT_READINESS_TIMEOUT)
    a.add_argument("--fetch-remote", action="store_true", help="fetch a remote-tracking base before pinning it")

    r = sub.add_parser("reconcile")
    common(r)
    found = r.add_mutually_exclusive_group(required=True)
    found.add_argument("--path", default=None)
    found.add_argument("--no-lease", action="store_true")
    r.add_argument("--evidence", required=True)

    rr = sub.add_parser("reconcile-return")
    common(rr)
    rr.add_argument("--found", required=True, choices=["returned", "still-leased"])
    rr.add_argument("--evidence", required=True)

    for name in ("check-release", "release"):
        c = sub.add_parser(name)
        common(c)
        c.add_argument("--target-ref", required=True)
        c.add_argument("--retained-ref", default=None)
        c.add_argument(
            "--owned-process",
            action="append",
            default=[],
            help="JSON identity printed by process-identity, repeatable",
        )
        c.add_argument("--valuable-ignored", action="append", default=[], help="glob, repeatable")
        if name == "release":
            c.add_argument("--session-id", required=True)

    pi = sub.add_parser("process-identity", help="capture an owned process identity in the pinned format")
    pi.add_argument("--pid", type=int, required=True)

    cc = sub.add_parser("check-coauthors", help="print a verification entry: no agent co-author trailers")
    cc.add_argument("--repo-path", required=True)
    cc.add_argument("--base", required=True)
    cc.add_argument("--head", required=True)

    rh = sub.add_parser("retain-head", help="keep a PR's tested head on a create-only ref")
    common(rh)
    rh.add_argument("--repo-path", required=True)
    rh.add_argument("--remote", required=True)
    rh.add_argument("--expected-remote-url", required=True)
    rh.add_argument("--pr", type=int, required=True)
    rh.add_argument("--expect-head", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check-coauthors":
        try:
            entry = check_coauthors(Path(args.repo_path), args.base, args.head)
        except AdapterError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(entry, indent=2, sort_keys=True))
        return entry["exit_code"]
    if args.command == "process-identity":
        try:
            print(json.dumps(process_identity(args.pid), indent=2, sort_keys=True))
        except ProcessProbeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        root = resolve_state_root(args.state_root)
        if args.command == "acquire":
            out = acquire(
                root,
                run_id=args.run_id,
                ws_id=args.workstream,
                repo=Path(args.repo),
                base=args.base,
                branch=args.branch,
                readiness_cmd=_json_list(args.readiness_cmd, "--readiness-cmd"),
                lockfiles=args.lockfile,
                get_timeout=args.get_timeout,
                readiness_timeout=args.readiness_timeout,
                fetch_remote=args.fetch_remote,
            )
        elif args.command == "retain-head":
            out = retain_head(
                root,
                run_id=args.run_id,
                ws_id=args.workstream,
                repo_path=Path(args.repo_path),
                remote=args.remote,
                expected_remote_url=args.expected_remote_url,
                pr=args.pr,
                expect_head=args.expect_head,
            )
        elif args.command == "reconcile-return":
            out = reconcile_return(
                root, run_id=args.run_id, ws_id=args.workstream, found=args.found, evidence=args.evidence
            )
        elif args.command == "reconcile":
            out = reconcile(
                root,
                run_id=args.run_id,
                ws_id=args.workstream,
                path=None if args.no_lease else Path(args.path),
                evidence=args.evidence,
            )
        else:
            processes = [json.loads(p) for p in args.owned_process]
            entry = _read_journal(_journal_path(root, args.run_id, args.workstream))
            if args.command == "check-release":
                if entry is None or not (entry.get("receipt") or {}).get("path"):
                    raise AdapterError("no leased workspace recorded for this holder")
                out = check_release(
                    entry["receipt"],
                    target_ref=args.target_ref,
                    retained_ref=args.retained_ref,
                    owned_processes=processes,
                    valuable_ignored=args.valuable_ignored,
                )
            else:
                out = release(
                    root,
                    run_id=args.run_id,
                    ws_id=args.workstream,
                    session_id=args.session_id,
                    target_ref=args.target_ref,
                    retained_ref=args.retained_ref,
                    owned_processes=processes,
                    valuable_ignored=args.valuable_ignored,
                )
        print(json.dumps(out, indent=2, sort_keys=True))
    except StateRootError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (AdapterError, ledger.CoordinatorError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if isinstance(out, dict) and out.get("provisioning", {}).get("status") in {"blocked", "uncertain"}:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
