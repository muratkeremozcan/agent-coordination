#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Coordinator run ledger: the durable state utility behind the `coordinate` skill.

Owns run, workstream, decision, denial and claim state on disk under an explicitly
configured state root (`--state-root` or `COORDINATE_STATE_ROOT`; there is no default).
Nothing here dispatches workers or runs Git. The coordinator supplies evidence; this
module refuses transitions the evidence does not support, so the orchestration contract
holds at the boundary:

- a workstream starts only when its dependencies are verified complete and its stack
  parent has produced the commit it branches from;
- review readiness and completion need a substantive result for the current assignment
  generation, with artifacts, passing verification commands and a satisfied review
  policy; stale and duplicate results are retained but cannot advance anything;
- every mutation must come from the session that holds the run's claim, and a claim
  moves between coordinators only through a release or a recorded forced handover;
- landing needs a run-level merge grant, a workstream permission, and no unresolved
  denial for that action;
- cleanup evidence must be recent, timezone-qualified and bound to the workstream's
  worktree and head.

Every mutating command re-reads the run record under an OS-level lock on the record's own
file, applies one typed operation (or, for `apply`, an atomic batch) to an in-memory copy,
validates the result, and only then commits through a temp-file, fsync, atomic-rename
sequence with backup rotation. A failed validation or a failed op leaves the on-disk
record untouched.

Usage (every command also takes --state-root and --format):
    coordinator_state.py start --title ... --intent ... --coordination-reason ... \\
        --delivery-authority ... --authority '<json grants>' --profile <file.toml> \\
        --session-id ...
    coordinator_state.py add-workstream --run-id ... --session-id ... --title ... \\
        --kind ship|scout --repository ... --scope ... --brief ... \\
        --acceptance-criteria '[...]' --delivery-authority ... \\
        --permitted-operations '[...]' [--dependencies '[...]' --stack-parent ws-1]
    coordinator_state.py assign --run-id ... --session-id ... --id ws-1 --binding ... \\
        --owner-type ... --owner-id ... [--worktree ... --branch ... --base-sha ...]
    coordinator_state.py record-workspace --run-id ... --session-id ... --id ws-1 \\
        --receipt '<json>'
    coordinator_state.py issue-request --run-id ... --session-id ... --id ws-1 \\
        --request-id ... --purpose brief|follow-up|repair
    coordinator_state.py record-result --run-id ... --session-id ... --id ws-1 \\
        --result '<json>'
    coordinator_state.py update-workstream --run-id ... --session-id ... --id ws-1 \\
        --status working
    coordinator_state.py record-landing --run-id ... --session-id ... --id ws-1 \\
        --landing '<json>'
    coordinator_state.py record-denial / resolve-denial / open-decision / resolve-decision
    coordinator_state.py apply --ops-file batch.json
    coordinator_state.py snapshot --run-id ...
    coordinator_state.py close --run-id ... --session-id ... --outcome ...
    coordinator_state.py abandon --run-id ... --session-id ... --reason ...
    coordinator_state.py claim --run-id ... --session-id ... [--force --expected-holder ... \\
        --reason ... --liveness-evidence ...]
    coordinator_state.py release-claim --run-id ... --session-id ... --reason ...
    coordinator_state.py import-v1 --source <v1 fixture copy> --profile ... --session-id ...
    coordinator_state.py list
"""

import argparse
import contextvars
import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters"))
from process_identity import identity_is_live  # noqa: E402
from run_profile import ProfileError, canonical_json, load_profile, validate_profile  # noqa: E402
from session_bindings import BINDINGS, check_session_receipt  # noqa: E402
from state_root import StateRootError, check_not_protected, resolve_state_root, within_root  # noqa: E402

SCHEMA_VERSION = 3
V2_SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.05
MAX_RUN_ID_ATTEMPTS = 8
CLOCK_SKEW_SECONDS = 300

RUN_STATUSES = {"active", "completed", "abandoned"}
WORKSTREAM_KINDS = {"ship", "scout"}
WORKSTREAM_STATUSES = {
    "queued",
    "working",
    "waiting",
    "blocked",
    "review-ready",
    "completed",
    "failed",
}
WORKSTREAM_TRANSITIONS = {
    # Every non-terminal status can reach "failed", because a workstream can be cancelled or
    # superseded before anyone touches it. A move to "failed" from an untouched state
    # requires a stated blocker.
    "queued": {"working", "failed"},
    # Schema v1 allowed working -> completed with verification in the same call. Schema v2
    # routes every completion through review-ready, because that is where a substantive
    # result for the current generation is required, including for coordinator-run work.
    "working": {"waiting", "blocked", "review-ready", "failed"},
    "waiting": {"working", "failed"},
    "blocked": {"working", "failed"},
    "review-ready": {"completed", "failed", "working"},
    "completed": set(),
    "failed": set(),
}
TERMINAL_WORKSTREAM_STATUSES = {"completed", "failed"}
ACTIVE_WORKSTREAM_STATUSES = {"working", "waiting", "blocked", "review-ready"}
RESULT_ACCEPTING_STATUSES = {"working", "waiting", "blocked"}
# Terminal workstreams remain immutable except for appending verification evidence, a
# correction, a stale result, a landing record or a cleanup disposition.
RESTRICTED_ON_TERMINAL_WORKSTREAM = {"status", "blocker", "clear_blocker", "deliverables"}
WAIT_PARTIES = {"owner", "user", "ci", "reviewer", "quota"}
DECISION_STATUSES = {"open", "resolved", "superseded"}
DENIAL_STATUSES = {"open", "resolved"}
CLEANUP_DISPOSITIONS = {"retained", "eligible", "removed-externally"}
AUTHORITY_ACTIONS = (
    "local-edit",
    "peer-create",
    "subagent-create",
    "push",
    "pr-create",
    "merge",
    "release",
    "cleanup",
)
REVIEW_STATUSES = {"passed", "findings", "blocked", "not-run", "unavailable"}
RESULT_OUTCOMES = {"completed", "failed"}
EVIDENCE_SOURCES = {"reply-correlated", "runtime-read", "text-correlated", "coordinator-observed"}
DECISION_GATES = {"dispatch", "completion", "landing"}
DECISION_RESPONSIBILITY = {"user", "coordinator"}
DECISION_PROVENANCE = {"user-words", "tactical", "moot"}
DELIVERABLES = {"report", "local-branch", "published-branch", "pr", "landed"}
FORGES = {"github", "none"}
DISPOSITIONS = {"left-for-review", "landed", "not-landing", "superseded"}
COORDINATOR_RUNTIMES = {"codex", "claude"}
EVENT_KINDS = {"ack", "progress", "question", "blocked", "waiting"}
LIFECYCLE_OBSERVATIONS = {"working", "idle", "done", "unknown"}
# A substantive request asks the owner for work; the latest one issued in the current
# generation and attempt governs what a terminal result must answer. A report-recovery
# request only asks the owner to repost the outcome of its root request.
SUBSTANTIVE_PURPOSES = {"brief", "follow-up", "repair", "answer", "rebase", "amend"}
RECOVERY_PURPOSE = "report-recovery"
REQUEST_PURPOSES = SUBSTANTIVE_PURPOSES | {RECOVERY_PURPOSE}
PROVISIONING_STATUSES = {"ready", "blocked", "uncertain"}
LANDING_METHODS = {"merge", "squash", "rebase"}
BATCH_ALLOWED_OPS = {
    "add-workstream",
    "assign",
    "record-workspace",
    "issue-request",
    "record-result",
    "update-workstream",
    "record-landing",
    "record-denial",
    "resolve-denial",
    "open-decision",
    "scope-decision",
    "resolve-decision",
    "reconcile-result",
    "record-event",
    "reconcile-evidence",
    "record-delivery",
    "record-publication",
    "observe",
    "ack-due",
    "record-sweep",
    "advance-base",
    "restack",
    "amend-intent",
    "amend-permissions",
    "record-disposition",
    "set-next-action",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SLUG_MAX_LEN = 60
# Gates every run_id that arrives from outside before it is turned into a path, so an
# absolute or traversing string can never resolve outside the runs directory.
_RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class CoordinatorError(Exception):
    """A command-level failure reported to the caller without a stack trace."""


class CorruptRecordError(CoordinatorError):
    def __init__(self, path: Path):
        super().__init__(
            f"run record at {path} failed to parse and is quarantined; recover by "
            "restoring the rotated `.bak` file or run `abandon` with a reason"
        )
        self.path = path


class LockTimeoutError(CoordinatorError):
    pass


class RevisionMismatchError(CoordinatorError):
    pass


# The state root of the transaction being applied, so operation handlers can check request
# bodies stored beside the record without threading the root through every handler.
_TRANSACTION_ROOT: contextvars.ContextVar[Path | None] = contextvars.ContextVar("transaction_root", default=None)


class _NoChange(Exception):
    """An idempotent repeat: the requested state already holds, so nothing is written."""


# --- paths -------------------------------------------------------------------------


def runs_dir(root: Path, *, create: bool) -> Path:
    d = within_root(root, root / "runs", purpose="runs directory")
    if create:
        d.mkdir(parents=True, exist_ok=True)
        within_root(root, d, purpose="runs directory")
    return d


def _plain_file_path(root: Path, name: str, purpose: str) -> Path:
    """A path directly inside the runs directory that is not itself a symlink.

    Resolving a record symlink would hand back another run's file under the requested id,
    so a symlinked entry is refused outright instead of followed.
    """
    path = runs_dir(root, create=False) / name
    if path.is_symlink():
        raise CoordinatorError(f"{purpose} {path} is a symlink; refusing to follow it")
    within_root(root, path, purpose=purpose)
    return path


def run_path(root: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise CoordinatorError(f"invalid run id {run_id!r}")
    return _plain_file_path(root, f"{run_id}.json", "run record")


def backup_path(root: Path, run_id: str) -> Path:
    return _plain_file_path(root, f"{run_id}.json.bak", "run backup")


def bodies_dir(root: Path, run_id: str, *, create: bool) -> Path:
    run_path(root, run_id)  # validates the id before it becomes a path
    runs = runs_dir(root, create=create)
    directory = runs / run_id / "bodies"
    for path in (runs / run_id, directory):
        if path.is_symlink():
            raise CoordinatorError(f"request body directory {path} is a symlink; refusing to follow it")
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return within_root(root, directory, purpose="request body directory")


def body_path(root: Path, run_id: str, digest: str, *, create: bool = False) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise CoordinatorError(f"invalid body digest {digest!r}")
    path = bodies_dir(root, run_id, create=create) / f"{digest}.md"
    if path.is_symlink():
        raise CoordinatorError(f"request body {path} is a symlink; refusing to follow it")
    return path


def store_body(root: Path, run_id: str, text: str) -> str:
    """Write an instruction body once, content-addressed, before the ledger refers to it.

    The name is the body's SHA-256, so a concurrent writer of the same body creates the same
    file and an existing file is accepted only after its content is verified.
    """
    if not isinstance(text, str) or not text.strip():
        raise CoordinatorError("a request body must be non-empty text")
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    path = body_path(root, run_id, digest, create=True)
    if not path.exists():
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        finally:
            os.unlink(tmp)
        _fsync_dir(path.parent)
    if body_problem(root, run_id, digest):
        raise CoordinatorError(f"request body {path} exists with different content")
    return digest


def body_problem(root: Path, run_id: str, digest: str) -> str | None:
    try:
        data = body_path(root, run_id, digest).read_bytes()
    except FileNotFoundError:
        return f"request body {digest} is missing"
    if hashlib.sha256(data).hexdigest() != digest:
        return f"request body {digest} does not match its digest"
    return None


def _slugify(title: str) -> str:
    slug = _SLUG_RE.sub("-", title.strip().lower()).strip("-")
    return (slug or "run")[:_SLUG_MAX_LEN]


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# --- locking and durable writes -----------------------------------------------------


def _flock_with_timeout(fd: int, path: Path, *, exclusive: bool = True) -> None:
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise LockTimeoutError(f"could not acquire the lock on {path} within {LOCK_TIMEOUT_SECONDS}s") from None
            time.sleep(LOCK_POLL_SECONDS)
        else:
            return


def _fd_matches_path(fd: int, path: Path) -> bool:
    try:
        path_stat = path.stat()
    except FileNotFoundError:
        return False
    fd_stat = os.fstat(fd)
    return (fd_stat.st_dev, fd_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)


def _open_locked(path: Path, *, exclusive: bool = True) -> int:
    """Open, lock, and confirm the fd still matches the path's current inode.

    `os.replace` swaps the path to a fresh inode on every commit, but an fd opened before
    that swap keeps its flock bound to the old, now-unlinked inode: a waiter that acquires
    that lock after the holder closes it is locking nothing live, while a fresh opener on
    the path sails through uncontended, and two writers can commit on top of each other
    with no error. Retrying converges because only the current lock holder can replace the
    file, so once fstat confirms this fd matches the path, nothing can swap the file out
    from under it before it is released.
    """
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        fd = os.open(path, os.O_RDWR if exclusive else os.O_RDONLY)
        try:
            _flock_with_timeout(fd, path, exclusive=exclusive)
            if _fd_matches_path(fd, path):
                return fd
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        if time.monotonic() >= deadline:
            raise LockTimeoutError(
                f"could not acquire a stable lock on {path} within {LOCK_TIMEOUT_SECONDS}s "
                "(the file kept being replaced by another writer while waiting)"
            )


def _fsync_dir(directory: Path) -> None:
    dir_fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _serialize(record: dict) -> str:
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


def _commit(root: Path, path: Path, record: dict, *, previous_text: str | None) -> None:
    if previous_text is not None:
        _atomic_write_text(backup_path(root, record["run_id"]), previous_text)
    _atomic_write_text(path, _serialize(record))


def _link_new_record(directory: Path, path: Path, record: dict) -> bool:
    """Create `path` holding the complete record, or return False if it already exists.

    A hard link either fully succeeds, so the target holds the complete fsynced content
    with no window where it exists but is empty, or fails with nothing written.
    """
    tmp = directory / f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_serialize(record))
        f.flush()
        os.fsync(f.fileno())
    try:
        os.link(tmp, path)
    except FileExistsError:
        return False
    finally:
        os.unlink(tmp)
    _fsync_dir(directory)
    return True


# --- schema validation ---------------------------------------------------------------


def _validate_record(record: dict) -> None:
    if record.get("status") not in RUN_STATUSES:
        raise CoordinatorError(f"invalid run status {record.get('status')!r}")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise CoordinatorError(
            f"unsupported schema_version {record.get('schema_version')!r}; this ledger reads "
            f"version {SCHEMA_VERSION} only (use import-v1 or migrate-v2 on a copy of an older record)"
        )
    revision = record.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise CoordinatorError(f"invalid revision {revision!r}")
    ws_ids = [w["id"] for w in record["workstreams"]]
    if len(ws_ids) != len(set(ws_ids)):
        raise CoordinatorError("duplicate workstream ids detected")
    for w in record["workstreams"]:
        if w["status"] not in WORKSTREAM_STATUSES:
            raise CoordinatorError(f"invalid workstream status {w['status']!r}")
        if w["kind"] not in WORKSTREAM_KINDS:
            raise CoordinatorError(f"invalid workstream kind {w['kind']!r}")
        disposition = w.get("cleanup_disposition")
        if disposition is not None and disposition not in CLEANUP_DISPOSITIONS:
            raise CoordinatorError(f"invalid cleanup disposition {disposition!r}")
    dec_ids = [d["id"] for d in record["decisions"]]
    if len(dec_ids) != len(set(dec_ids)):
        raise CoordinatorError("duplicate decision ids detected")
    for d in record["decisions"]:
        if d["status"] not in DECISION_STATUSES:
            raise CoordinatorError(f"invalid decision status {d['status']!r}")
    for d in record["denials"]:
        if d["status"] not in DENIAL_STATUSES:
            raise CoordinatorError(f"invalid denial status {d['status']!r}")


def _require_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CoordinatorError(f"{key!r} is required and must be a non-empty string")
    return value


def _require_int(data: dict, key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise CoordinatorError(f"{key!r} is required and must be an integer")
    return value


def _require_sha(data: dict, key: str) -> str:
    value = _require_str(data, key)
    if not _SHA_RE.match(value):
        raise CoordinatorError(f"{key!r} must be a lowercase hex commit id, got {value!r}")
    return value


def _require_full_sha(data: dict, key: str) -> str:
    value = _require_str(data, key)
    if not _FULL_SHA_RE.match(value):
        raise CoordinatorError(f"{key!r} must be a full lowercase commit id, got {value!r}")
    return value


def _require_record_entry(value: object, field: str) -> None:
    if not isinstance(value, dict):
        raise CoordinatorError(f"each entry in {field!r} must be a JSON object, got {type(value).__name__}: {value!r}")


def _json_arg(raw: str | None, flag: str):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CoordinatorError(f"{flag} must be valid JSON: {exc}") from exc


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CoordinatorError(f"{field} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CoordinatorError(f"{field} {value!r} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CoordinatorError(f"{field} {value!r} must carry a timezone offset")
    return parsed


_RELATIVE_RE = re.compile(r"^\+(\d+)([mh])$")


def _parse_when(value: object, field: str) -> datetime:
    """An ISO-8601 timestamp with an offset, or `+Nm` / `+Nh` from now."""
    if isinstance(value, str):
        match = _RELATIVE_RE.match(value.strip())
        if match:
            amount, unit = int(match.group(1)), match.group(2)
            return _now() + (timedelta(minutes=amount) if unit == "m" else timedelta(hours=amount))
    return _parse_timestamp(value, field)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _require_active(record: dict) -> None:
    if record["status"] != "active":
        raise CoordinatorError(f"run {record['run_id']} is not active (status={record['status']!r})")


def _new_id(prefix: str, existing: set[str]) -> str:
    n = len(existing) + 1
    while f"{prefix}-{n}" in existing:
        n += 1
    return f"{prefix}-{n}"


def _find(items: list[dict], item_id: str, kind: str, run_id: str) -> dict:
    for item in items:
        if item["id"] == item_id:
            return item
    raise CoordinatorError(f"no {kind} {item_id} in run {run_id}")


def _find_workstream(record: dict, ws_id: str) -> dict:
    return _find(record["workstreams"], ws_id, "workstream", record["run_id"])


def _granted_actions(record: dict) -> set[str]:
    return {g["action"] for g in record["authority"]["grants"] if g["granted"] is True}


def _validate_authority(grants: object) -> list[dict]:
    """Every action is explicitly granted or withheld, each tied to its instruction.

    The free-text delivery authority stays the source of meaning; these structured grants
    bound the operations the ledger itself gates (landing, workstream permissions).
    """
    if not isinstance(grants, list):
        raise CoordinatorError("--authority must be a JSON array of grant objects")
    seen: dict[str, dict] = {}
    for grant in grants:
        _require_record_entry(grant, "authority")
        action = _require_str(grant, "action")
        if action not in AUTHORITY_ACTIONS:
            raise CoordinatorError(f"unknown authority action {action!r}; expected one of {list(AUTHORITY_ACTIONS)}")
        if action in seen:
            raise CoordinatorError(f"authority action {action!r} is listed twice")
        if not isinstance(grant.get("granted"), bool):
            raise CoordinatorError(f"authority {action!r} needs a boolean 'granted'")
        _require_str(grant, "instruction")
        seen[action] = {
            "action": action,
            "granted": grant["granted"],
            "instruction": grant["instruction"],
            "target": grant.get("target"),
            "limits": grant.get("limits"),
        }
    missing = [a for a in AUTHORITY_ACTIONS if a not in seen]
    if missing:
        raise CoordinatorError(f"authority must explicitly grant or withhold every action; missing {missing}")
    return [seen[a] for a in AUTHORITY_ACTIONS]


# --- run creation ----------------------------------------------------------------------


def _new_run_record(
    *,
    run_id: str,
    title: str,
    intent: str,
    coordination_reason: str,
    delivery_authority: str,
    grants: list[dict],
    profile: dict,
    profile_digest: str,
    session_id: str,
    next_action: str,
    coordinator_runtime: str | None = None,
) -> dict:
    now = _now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "title": title,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "intent": intent,
        "coordination_reason": coordination_reason,
        "delivery_authority": delivery_authority,
        "authority": {"grants": grants, "amendments": []},
        "coordinator_runtime": coordinator_runtime,
        "supervision": _new_supervision(),
        "profile": profile,
        "profile_digest": profile_digest,
        "workstreams": [],
        "decisions": [],
        "denials": [],
        "next_action": next_action,
        "outcome": None,
        "revision": 1,
        # Claimed by its creator from the instant it exists, so no second session can
        # claim a run its creator is still dispatching into.
        "claimed_by": {"session_id": session_id, "claimed_at": now, "generation": 1},
        "claim_history": [{"event": "claim", "session_id": session_id, "generation": 1, "at": now}],
    }


def _new_supervision() -> dict:
    return {"parked": None, "due_acks": {}, "next_sweep_at": None, "sweep_interval_minutes": None}


def create_run(
    root: Path,
    *,
    title: str,
    intent: str,
    coordination_reason: str,
    delivery_authority: str,
    authority: object,
    profile_path: str,
    session_id: str,
    next_action: str = "",
    coordinator_runtime: str = "",
) -> dict:
    if coordinator_runtime not in COORDINATOR_RUNTIMES:
        raise CoordinatorError(f"coordinator_runtime must be one of {sorted(COORDINATOR_RUNTIMES)}")
    for key, value in (
        ("title", title),
        ("intent", intent),
        ("coordination_reason", coordination_reason),
        ("delivery_authority", delivery_authority),
        ("session_id", session_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise CoordinatorError(f"{key} is required and must be a non-empty string")
    grants = _validate_authority(authority)
    profile, profile_digest = load_profile(profile_path)
    directory = runs_dir(root, create=True)
    slug = _slugify(title)
    for _ in range(MAX_RUN_ID_ATTEMPTS):
        run_id = f"{slug}-{secrets.token_hex(3)}"
        record = _new_run_record(
            run_id=run_id,
            title=title,
            intent=intent,
            coordination_reason=coordination_reason,
            delivery_authority=delivery_authority,
            grants=grants,
            profile=profile,
            profile_digest=profile_digest,
            session_id=session_id,
            next_action=next_action,
            coordinator_runtime=coordinator_runtime,
        )
        _validate_record(record)
        if _link_new_record(directory, directory / f"{run_id}.json", record):
            return record
    raise CoordinatorError(f"could not allocate a unique run id after {MAX_RUN_ID_ATTEMPTS} attempts")


# --- schema v1 import -------------------------------------------------------------------

_V1_REQUIRED_KEYS = {
    "schema_version",
    "run_id",
    "title",
    "status",
    "created_at",
    "updated_at",
    "intent",
    "coordination_reason",
    "delivery_authority",
    "workstreams",
    "decisions",
    "next_action",
    "outcome",
    "revision",
    "claimed_by",
}


def import_v1(root: Path, *, source: str, profile_path: str, session_id: str) -> dict:
    """Read a schema v1 fixture copy and write a v2 record into this root.

    The source is only ever read. It is refused when it lives inside a protected location,
    because the only v1 records there are a live assistant's active runs, which are not
    migrated before cutover. Structured authority is not inferable from v1 free text, so
    every action starts withheld and the original wording is preserved for re-granting.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise CoordinatorError("session_id is required and must be a non-empty string")
    source_path = Path(source).expanduser()
    if not source_path.is_absolute():
        raise CoordinatorError("--source must be an absolute path")
    check_not_protected(source_path, purpose="import source")
    raw = source_path.read_bytes()
    try:
        legacy = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoordinatorError(f"{source_path} is not a parseable JSON record") from exc
    if not isinstance(legacy, dict) or legacy.get("schema_version") != LEGACY_SCHEMA_VERSION:
        version = legacy.get("schema_version") if isinstance(legacy, dict) else None
        raise CoordinatorError(f"import-v1 reads schema_version 1 only, got {version!r}")
    missing = _V1_REQUIRED_KEYS - set(legacy)
    if missing:
        raise CoordinatorError(f"v1 record is missing key(s): {sorted(missing)}")
    run_id = legacy["run_id"]
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise CoordinatorError(f"v1 record carries an invalid run id {run_id!r}")
    profile, profile_digest = load_profile(profile_path)
    grants = [
        {
            "action": action,
            "granted": False,
            "instruction": "not carried over from schema v1; re-grant from delivery_authority",
            "target": None,
            "limits": None,
        }
        for action in AUTHORITY_ACTIONS
    ]
    record = _new_run_record(
        run_id=run_id,
        title=legacy["title"],
        intent=legacy["intent"],
        coordination_reason=legacy["coordination_reason"],
        delivery_authority=legacy["delivery_authority"],
        grants=grants,
        profile=profile,
        profile_digest=profile_digest,
        session_id=session_id,
        next_action=legacy.get("next_action") or "",
    )
    record.update(
        {
            "status": legacy["status"],
            "created_at": legacy["created_at"],
            "outcome": legacy["outcome"],
            "decisions": copy.deepcopy(legacy["decisions"]),
            "imported_from": {
                "schema_version": LEGACY_SCHEMA_VERSION,
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "source_revision": legacy["revision"],
                "source_claimed_by": legacy["claimed_by"],
                "imported_at": _now_iso(),
            },
        }
    )
    for legacy_ws in legacy["workstreams"]:
        ws = {**_v3_workstream_fields(), **copy.deepcopy(legacy_ws)}
        owner = ws.get("owner_id")
        ws.update(
            {
                "stack_parent": None,
                "brief_digest": hashlib.sha256(str(ws.get("brief", "")).encode("utf-8")).hexdigest(),
                "permitted_operations": [],
                "assignment_generation": 1 if owner else 0,
                "assignment_history": [],
                "base_sha": None,
                "workspace": None,
                "requests": [],
                "results": [],
                "dispatch_attempts": 0,
                "landing": None,
            }
        )
        ws.setdefault("deliverables", [])
        ws.setdefault("verification", [])
        ws.setdefault("corrections", [])
        ws.setdefault("cleanup_disposition", None)
        ws.setdefault("cleanup_evidence", None)
        record["workstreams"].append(ws)
    _validate_record(record)
    directory = runs_dir(root, create=True)
    if not _link_new_record(directory, directory / f"{run_id}.json", record):
        raise CoordinatorError(f"run {run_id} already exists in this state root; refusing to overwrite")
    return record


def migrate_v2(root: Path, *, source: str, session_id: str) -> dict:
    """Read a schema v2 record copy and write a v3 record into this root.

    The source is only read, and refused inside a protected location. Nothing v3 needs is
    invented: v2 replies carry no source or owner, so they become `historical` evidence that
    cannot govern; a working workstream needs a fresh v3 request and result. A completed
    workstream keeps the head v2 verified it on, because v2 recorded that fact.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise CoordinatorError("session_id is required and must be a non-empty string")
    source_path = Path(source).expanduser()
    if not source_path.is_absolute():
        raise CoordinatorError("--source must be an absolute path")
    check_not_protected(source_path, purpose="migration source")
    raw = source_path.read_bytes()
    try:
        old = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoordinatorError(f"{source_path} is not a parseable JSON record") from exc
    if not isinstance(old, dict) or old.get("schema_version") != V2_SCHEMA_VERSION:
        version = old.get("schema_version") if isinstance(old, dict) else None
        raise CoordinatorError(f"migrate-v2 reads schema_version 2 only, got {version!r}")
    v2_record_keys = _REQUIRED_RECORD_KEYS - {"coordinator_runtime", "supervision"}
    v2_ws_keys = _REQUIRED_WORKSTREAM_KEYS - set(_v3_workstream_fields())
    missing = v2_record_keys - set(old)
    if missing or not isinstance(old.get("workstreams"), list):
        raise CoordinatorError(f"v2 record is missing key(s): {sorted(missing) or ['workstreams']}")
    run_id = old["run_id"]
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise CoordinatorError(f"v2 record carries an invalid run id {run_id!r}")
    record = copy.deepcopy(old)
    try:
        record["profile"] = validate_profile(copy.deepcopy(old["profile"]))
    except ProfileError as exc:
        raise CoordinatorError(f"the v2 record's frozen profile does not validate: {exc}") from exc
    record["profile_digest"] = hashlib.sha256(canonical_json(record["profile"]).encode("utf-8")).hexdigest()
    record["schema_version"] = SCHEMA_VERSION
    record["revision"] = 1
    record["coordinator_runtime"] = None
    record["supervision"] = _new_supervision()
    record["authority"].setdefault("amendments", [])
    now = _now_iso()
    record["claimed_by"] = {"session_id": session_id, "claimed_at": now, "generation": 1}
    record["claim_history"] = [{"event": "claim", "session_id": session_id, "generation": 1, "at": now}]
    record["migrated_from"] = {
        "schema_version": V2_SCHEMA_VERSION,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_revision": old["revision"],
        "source_claimed_by": old["claimed_by"],
        "source_claim_history": old["claim_history"],
        "source_profile": old["profile"],
        "source_profile_digest": old["profile_digest"],
        "migrated_at": now,
    }
    for decision in record["decisions"]:
        if not isinstance(decision, dict):
            raise CoordinatorError("v2 decisions must be objects")
        for key, value in (
            ("responsibility", None),
            ("origin_workstream", None),
            ("origin_event", None),
            ("affected_workstreams", []),
            ("gates", []),
            ("defer_until", None),
            ("scope_history", []),
            ("answer_request_id", None),
        ):
            decision.setdefault(key, value)
    for ws in record["workstreams"]:
        if not isinstance(ws, dict) or not v2_ws_keys.issubset(ws):
            raise CoordinatorError("a v2 workstream is missing required keys")
        verified = None
        if ws["status"] == "completed":
            accepted = [r for r in ws["results"] if r.get("status") == "accepted" and r.get("outcome") == "completed"]
            verified = accepted[-1].get("head_sha") if accepted else None
        for result in ws["results"]:
            if result.get("status") == "accepted":
                result["status"] = "historical"
                result["rejected_reason"] = "recorded under schema v2 without source or owner; cannot govern"
        for request in ws["requests"]:
            for key in (
                "root_request_id",
                "answers",
                "expect_ack_by",
                "expect_result_by",
                "acked_at",
                "progress_at",
                "result_at",
            ):
                request.setdefault(key, None)
        for key, value in _v3_workstream_fields().items():
            ws.setdefault(key, value)
        ws["verified_head"] = verified
    _validate_record(record)
    directory = runs_dir(root, create=True)
    if not _link_new_record(directory, directory / f"{run_id}.json", record):
        raise CoordinatorError(f"run {run_id} already exists in this state root; refusing to overwrite")
    return record


# --- operation helpers -------------------------------------------------------------------


def _require_holder(record: dict, op: dict) -> None:
    """Refuse a mutation from any session other than the current claim holder.

    This stops a stale or second coordinator from writing over the holder's decisions. It is
    a guard against accidental stale writes; a process with equal file access can still bypass it.
    """
    session_id = _require_str(op, "session_id")
    holder = record.get("claimed_by")
    if not holder:
        raise CoordinatorError(f"run {record['run_id']} has no claim holder; claim it before mutating")
    if holder["session_id"] != session_id:
        raise CoordinatorError(
            f"session {session_id!r} does not hold run {record['run_id']} (held by "
            f"{holder['session_id']!r}, generation {holder['generation']}); a stale "
            "coordinator must not write"
        )


def _current_results(ws: dict) -> list[dict]:
    return [
        r
        for r in ws["results"]
        if r["status"] in {"accepted", "conflict", "duplicate"}
        and r["generation"] == ws["assignment_generation"]
        and r["dispatch_attempt"] == ws["dispatch_attempts"]
    ]


def _current_requests(ws: dict) -> list[dict]:
    return [
        r
        for r in ws["requests"]
        if r["generation"] == ws["assignment_generation"] and r["dispatch_attempt"] == ws["dispatch_attempts"]
    ]


def _governing_request(ws: dict) -> dict | None:
    """The latest substantive request of the current generation and attempt."""
    substantive = [r for r in _current_requests(ws) if r.get("purpose") in SUBSTANTIVE_PURPOSES]
    return substantive[-1] if substantive else None


def _chain_root(ws: dict, request_id: str) -> str:
    request = next((r for r in ws["requests"] if r["request_id"] == request_id), None)
    return (request or {}).get("root_request_id") or request_id


def _chain_request_ids(ws: dict, root_id: str) -> set[str]:
    return {root_id} | {r["request_id"] for r in ws["requests"] if r.get("root_request_id") == root_id}


def _chain_results(ws: dict, root_id: str) -> list[dict]:
    chain = _chain_request_ids(ws, root_id)
    return [r for r in _current_results(ws) if r["request_id"] in chain]


def _chain_choice(ws: dict, root_id: str) -> dict | None:
    choices = [c for c in ws["result_reconciliations"] if c["root_request_id"] == root_id]
    return choices[-1] if choices else None


def _canonical_result(ws: dict, root_id: str) -> tuple[dict | None, bool]:
    """The one terminal result of a chain, and whether an unreconciled conflict hides it.

    The first accepted terminal result is canonical. A later reply in the same chain that
    differs (compared without its carrier request id) is a conflict and blocks the chain
    until the coordinator picks one with its evidence.
    """
    results = _chain_results(ws, root_id)
    choice = _chain_choice(ws, root_id)
    conflicts = [ws["results"].index(r) for r in results if r["status"] == "conflict"]
    if choice is not None and all(i < choice["results_before"] for i in conflicts):
        # A choice settles only the conflicts recorded before it; a newer one reopens the chain.
        chosen = next((r for r in results if r["normalized_digest"] == choice["chosen"]), None)
        return chosen, False
    if conflicts:
        return None, True
    accepted = [r for r in results if r["status"] == "accepted"]
    return (accepted[0] if accepted else None), False


def _amended_after(ws: dict, result: dict) -> bool:
    """Whether an accepted amendment was recorded after this result (by position, not clock)."""
    if not ws["amendments"]:
        return False
    return ws["results"].index(result) < ws["amendments"][-1]["results_before"]


def _governing_problems(ws: dict) -> tuple[dict | None, list[str]]:
    """The canonical terminal result answering the governing request, or why there is none."""
    governing = _governing_request(ws)
    if governing is None:
        return None, [f"no substantive request issued for generation {ws['assignment_generation']}"]
    root = governing["request_id"]
    result, conflicted = _canonical_result(ws, root)
    if conflicted:
        return None, [f"request {root} has conflicting replies; reconcile-result must choose one"]
    if result is None:
        return None, [f"request {root} has no accepted terminal result"]
    problems = []
    if ws["kind"] == "ship" or ws.get("base_sha"):
        if result.get("base_sha") != governing.get("target_base_sha"):
            problems.append(
                f"result base {result.get('base_sha')!r} is not request {root}'s target "
                f"{governing.get('target_base_sha')!r}"
            )
        if result.get("base_sha") != ws.get("base_sha"):
            problems.append(f"result base {result.get('base_sha')!r} is not the current base {ws.get('base_sha')!r}")
    if _amended_after(ws, result):
        problems.append("an accepted intent amendment postdates this result")
    return result, problems


def _terminal_result(ws: dict) -> dict | None:
    result, problems = _governing_problems(ws)
    return None if problems else result


def _completed_result(ws: dict) -> dict | None:
    result = _terminal_result(ws)
    return result if result is not None and result["outcome"] == "completed" else None


def _verified_head(ws: dict) -> str | None:
    """The head a completed workstream was verified on, frozen at completion."""
    if ws["status"] == "completed":
        return ws.get("verified_head")
    result = _completed_result(ws)
    return result.get("head_sha") if result else None


def _stack_parent_head(record: dict, ws: dict) -> str | None:
    """The commit a stacked child branches from: its parent's authoritative verified head.

    The parent may still be in review, but its result must already carry the evidence a
    completion would need (artifacts, passing checks, satisfied review), so a child never
    branches from a head whose own checks failed. A landed parent's source head stays
    authoritative.
    """
    parent = _effective(record, _find_workstream(record, ws["stack_parent"]))
    if parent["status"] == "completed":
        # A completed parent that must itself restack offers no valid head to build on.
        return None if _restack_required(record, parent) else parent.get("verified_head")
    if parent["status"] != "review-ready":
        return None
    result = _completed_result(parent)
    if result is None or not result.get("head_sha") or _result_problems(record, parent, result):
        return None
    return result["head_sha"]


def _restack_required(record: dict, ws: dict) -> bool:
    """Derived, never stored: the stack parent's authoritative head moved under this child."""
    if not ws.get("stack_parent") or ws.get("landing") or not ws.get("base_sha"):
        return False
    parent = _effective(record, _find_workstream(record, ws["stack_parent"]))
    if parent["status"] == "completed" and _restack_required(record, parent):
        return True
    head = _stack_parent_head(record, ws)
    return head is not None and head != ws.get("base_sha")


def _dependency_satisfied(record: dict, dep_id: str, seen: set[str] | None = None) -> bool:
    """A dependency counts once it is verified complete and still valid, or its successor is."""
    seen = seen or set()
    if dep_id in seen:
        return False
    seen.add(dep_id)
    dep = _find_workstream(record, dep_id)
    if dep.get("superseded_by"):
        return _dependency_satisfied(record, dep["superseded_by"], seen)
    return dep["status"] == "completed" and not _restack_required(record, dep)


def _open_gating_decisions(record: dict, ws_id: str, gate: str) -> list[str]:
    return [
        d["id"]
        for d in record["decisions"]
        if d["status"] == "open" and gate in (d.get("gates") or []) and ws_id in (d.get("affected_workstreams") or [])
    ]


def _check_gate(record: dict, ws: dict, gate: str, action: str) -> None:
    blocking = _open_gating_decisions(record, ws["id"], gate)
    if blocking:
        raise CoordinatorError(
            f"workstream {ws['id']} cannot {action}: decision(s) {blocking} gate {gate} and are open"
        )


def _base_reconciled(ws: dict, receipt: dict) -> bool:
    """Whether recorded base advances lead from the workspace's acquisition base to the current one."""
    current = receipt["expected_base_sha"]
    last_head = None
    for entry in ws["base_history"]:
        if entry["old"] == current:
            current, last_head = entry["new"], entry.get("workspace_head")
    return current == ws.get("base_sha") and bool(last_head)


def _check_dispatch_ready(record: dict, ws: dict) -> None:
    """Dependencies, stack parent, decision gates, workspace receipt and concurrency.

    Every real restart of execution runs these checks; the attempt budget is admission's
    concern (_admit_attempt), so a same-owner resume at the cap is still ready.
    """
    if not ws.get("owner_id"):
        raise CoordinatorError(f"workstream {ws['id']} has no assigned owner; assign before dispatch")
    if ws["kind"] == "ship":
        if "local-edit" not in _granted_actions(record):
            raise CoordinatorError(f"workstream {ws['id']} cannot run: the run withholds local-edit")
        if "local-edit" not in ws["permitted_operations"]:
            raise CoordinatorError(f"workstream {ws['id']} cannot run: it is not permitted local-edit")
        denied = _open_denials(record, "local-edit", ws["id"])
        if denied:
            raise CoordinatorError(f"workstream {ws['id']} cannot run: local-edit denial(s) {denied} are unresolved")
    ordering = [d for d in ws["dependencies"] if d != ws.get("stack_parent")]
    unmet = [d for d in ordering if not _dependency_satisfied(record, d)]
    if unmet:
        raise CoordinatorError(
            f"workstream {ws['id']} cannot start: dependency workstream(s) {unmet} are not verified complete"
        )
    _check_gate(record, ws, "dispatch", "start")
    expected_base = ws.get("base_sha")
    if ws["kind"] == "ship" and not expected_base:
        raise CoordinatorError(
            f"ship workstream {ws['id']} has no pinned base; assign --base-sha or record its workspace receipt"
        )
    if ws.get("stack_parent"):
        parent_head = _stack_parent_head(record, ws)
        if parent_head is None:
            raise CoordinatorError(
                f"workstream {ws['id']} cannot start: stack parent {ws['stack_parent']} has "
                "not produced a verified result commit to branch from"
            )
        if expected_base != parent_head:
            raise CoordinatorError(
                f"workstream {ws['id']} is assigned base {expected_base!r} but its stack "
                f"parent's verified head is {parent_head!r}; restack onto that head"
            )
    if ws["kind"] == "ship":
        receipt = ws.get("workspace")
        if not receipt:
            raise CoordinatorError(f"ship workstream {ws['id']} cannot start without a workspace receipt")
        if receipt["provisioning"]["status"] != "ready":
            raise CoordinatorError(
                f"ship workstream {ws['id']} cannot start: workspace provisioning is "
                f"{receipt['provisioning']['status']!r}"
            )
        if expected_base and receipt["expected_base_sha"] != expected_base:
            if not _base_reconciled(ws, receipt):
                raise CoordinatorError(
                    f"workspace receipt base {receipt['expected_base_sha']!r} does not match the "
                    f"assigned base {expected_base!r} and no recorded base advance reconciles them"
                )
        elif receipt["observed_head_sha"] != receipt["expected_base_sha"]:
            raise CoordinatorError(
                f"workspace HEAD {receipt['observed_head_sha']!r} does not match its pinned "
                f"base {receipt['expected_base_sha']!r}; the workspace stays blocked"
            )
    limit = record["profile"]["limits"]["max_active_workstreams"]
    active = [
        w["id"] for w in record["workstreams"] if w["id"] != ws["id"] and w["status"] in ACTIVE_WORKSTREAM_STATUSES
    ]
    if ws["status"] not in ACTIVE_WORKSTREAM_STATUSES and len(active) >= limit:
        raise CoordinatorError(
            f"workstream {ws['id']} cannot start: {len(active)} workstream(s) already active "
            f"and the profile allows {limit}"
        )


def _admit_attempt(record: dict, ws: dict, action: str) -> None:
    """Spend one attempt for one new execution, refusing at the profile's cap."""
    attempts_limit = record["profile"]["limits"]["max_dispatch_attempts"]
    if ws["dispatch_attempts"] >= attempts_limit:
        raise CoordinatorError(
            f"workstream {ws['id']} has used {ws['dispatch_attempts']} of {attempts_limit} "
            f"dispatch attempts; open a decision before {action}"
        )
    ws["dispatch_attempts"] += 1
    ws["pending_admission"] = False


def _review_gaps(record: dict, result: dict) -> list[str]:
    review = {entry["reviewer"]: entry["status"] for entry in result["review"]}
    policy = record["profile"]["review"]
    gaps = []
    for reviewer in policy["required"]:
        if review.get(reviewer) != "passed":
            gaps.append(f"required reviewer {reviewer!r} is {review.get(reviewer, 'absent')!r}")
    for reviewer, status in review.items():
        if status in {"findings", "blocked"}:
            gaps.append(f"reviewer {reviewer!r} reports {status!r}")
        elif status in {"unavailable", "not-run"} and reviewer not in policy["required"]:
            if status == "not-run" or reviewer not in policy["allow_unavailable"]:
                gaps.append(f"reviewer {reviewer!r} is {status!r} and the profile does not allow it")
    return gaps


def _result_problems(record: dict, ws: dict, result: dict) -> list[str]:
    """Why an accepted completed result does not yet prove the workstream's deliverable."""
    problems = []
    if not result["artifacts"]:
        problems.append("no artifacts")
    if not result["verification"]:
        problems.append("no verification commands")
    failing = [v["command"] for v in result["verification"] if v["exit_code"] != 0]
    if failing:
        problems.append(f"verification command(s) exited nonzero: {failing}")
    if ws["kind"] == "ship":
        if not result.get("head_sha"):
            problems.append("no tested head_sha")
        if not ws.get("base_sha"):
            problems.append("the ship workstream has no pinned base")
    if (ws["kind"] == "ship" or ws.get("base_sha")) and result.get("base_sha") != ws.get("base_sha"):
        problems.append(f"result base {result.get('base_sha')!r} is not the current base {ws.get('base_sha')!r}")
    if ws["kind"] == "scout":
        problems.extend(_inventory_problems(record, ws, result))
    problems.extend(_review_gaps(record, result))
    return problems


def _fresh_problems(record: dict, read_at: object, label: str, *, now: datetime | None = None) -> list[str]:
    try:
        moment = _parse_timestamp(read_at, f"{label} read_at")
    except CoordinatorError as exc:
        return [str(exc)]
    now = now or _now()
    max_age = timedelta(minutes=record["profile"]["delivery"]["evidence_max_age_minutes"])
    if moment > now + timedelta(seconds=CLOCK_SKEW_SECONDS):
        return [f"{label} read_at is in the future"]
    if now - moment > max_age:
        return [f"{label} is older than {max_age}"]
    return []


def _pr_evidence_problems(record: dict, ws: dict, evidence: object, head: str | None) -> list[str]:
    """The publication predicate: every condition a PR must meet for its head to count."""
    if not isinstance(evidence, dict):
        return ["publication evidence must be a JSON object"]
    contract = ws.get("delivery_contract") or {}
    problems = []
    expect = {"schema": 1, "kind": "pr", "source": "gh", "uncertain": False, "complete": True, "head_stable": True}
    for key, value in expect.items():
        if evidence.get(key) != value:
            problems.append(f"evidence {key} is {evidence.get(key)!r}, expected {value!r}")
    if str(evidence.get("repo", "")).lower() != str(contract.get("repo", "")).lower():
        problems.append(f"evidence repository {evidence.get('repo')!r} is not {contract.get('repo')!r}")
    if not (evidence.get("target") == evidence.get("base_ref") == contract.get("target_branch")):
        problems.append(
            f"evidence base {evidence.get('base_ref')!r} is not the target {contract.get('target_branch')!r}"
        )
    if ws.get("pr") is not None and evidence.get("pr") != ws["pr"]:
        problems.append(f"evidence names PR {evidence.get('pr')!r}, not the bound PR {ws['pr']!r}")
    if ws.get("branch") and evidence.get("head_ref") != ws["branch"]:
        problems.append(f"PR head branch {evidence.get('head_ref')!r} is not {ws['branch']!r}")
    if head is not None and evidence.get("head_oid") != head:
        problems.append(f"PR head {evidence.get('head_oid')!r} is not the result head {head!r}")
    if evidence.get("state") != "OPEN":
        problems.append(f"PR state is {evidence.get('state')!r}")
    if evidence.get("draft") is not False:
        problems.append("PR is a draft")
    if evidence.get("mergeable") == "CONFLICTING":
        problems.append("PR conflicts with its base")
    problems.extend(_check_summary_problems(evidence, contract))
    threads = evidence.get("review_threads")
    unresolved = threads.get("unresolved") if isinstance(threads, dict) else None
    if not isinstance(unresolved, int) or isinstance(unresolved, bool):
        problems.append("evidence review_threads.unresolved must be an integer")
    elif unresolved != 0:
        problems.append(f"{unresolved} unresolved review thread(s)")
    return problems


_CHECK_LISTS = ("required_passing", "required_not_passing", "required_missing", "failing", "pending")


def _required_label(entry: dict) -> str:
    app = entry.get("app_id")
    return entry["context"] if app in (None, -1) else f"{entry['context']} (app {app})"


def _check_summary_problems(evidence: dict, contract: dict) -> list[str]:
    """The check summary is complete, typed, and accounts for every required check as passing."""
    checks, required = evidence.get("checks"), evidence.get("required")
    if not isinstance(checks, dict) or not isinstance(required, list):
        return ["evidence needs a checks object and a required list"]
    problems = []
    count = checks.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        problems.append("evidence checks.count must be a non-negative integer")
    for key in _CHECK_LISTS:
        if not isinstance(checks.get(key), list) or not all(isinstance(i, str) for i in checks[key]):
            problems.append(f"evidence checks.{key} must be a list of names")
    if not all(isinstance(r, dict) and isinstance(r.get("context"), str) for r in required):
        problems.append("evidence required entries need a context")
    if problems:
        return problems
    for key in ("required_not_passing", "required_missing", "failing", "pending"):
        if checks[key]:
            problems.append(f"checks {key.replace('_', ' ')}: {checks[key]}")
    labels = sorted(_required_label(r) for r in required)
    if sorted(checks["required_passing"]) != labels:
        problems.append(f"required checks {labels} are not all reported passing ({sorted(checks['required_passing'])})")
    if not count and not (contract.get("no_ci") and not required):
        problems.append("no checks ran on the head and the workstream does not declare --no-ci")
    return problems


def _branch_evidence_problems(record: dict, ws: dict, evidence: object, head: str | None) -> list[str]:
    if not isinstance(evidence, dict):
        return ["publication evidence must be a JSON object"]
    problems = []
    if (evidence.get("schema"), evidence.get("kind"), evidence.get("source")) != (1, "branch", "git"):
        problems.append("published-branch evidence must be {schema: 1, kind: branch, source: git}")
    for key in ("remote", "remote_ref"):
        if not isinstance(evidence.get(key), str) or not evidence[key]:
            problems.append(f"published-branch evidence needs {key}")
    if head is not None and evidence.get("oid") != head:
        problems.append(f"remote ref holds {evidence.get('oid')!r}, not the result head {head!r}")
    return problems


def _publication_problems(record: dict, ws: dict, head: str | None) -> list[str]:
    deliverable = ws.get("deliverable")
    contract = ws.get("delivery_contract") or {}
    if deliverable in {"pr", "landed"} and contract.get("forge") == "github":
        evidence = ws.get("publication")
        if evidence is None:
            return ["no PR evidence recorded; run forge_github.py pr-evidence and record-publication"]
        return _pr_evidence_problems(record, ws, evidence, head) + _fresh_problems(
            record, evidence.get("read_at"), "PR evidence"
        )
    if deliverable == "published-branch":
        evidence = ws.get("publication")
        if evidence is None:
            return ["no published-branch evidence recorded"]
        return _branch_evidence_problems(record, ws, evidence, head) + _fresh_problems(
            record, evidence.get("read_at"), "branch evidence"
        )
    return []


def _op_record_publication(record: dict, op: dict) -> None:
    """Record forge or remote evidence that a workstream's head is published as asked."""
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    if ws["status"] in TERMINAL_WORKSTREAM_STATUSES:
        raise CoordinatorError(f"workstream {ws['id']} is terminal")
    evidence = op.get("evidence")
    _require_record_entry(evidence, "evidence")
    if ws.get("deliverable") in {"pr", "landed"} and (ws.get("delivery_contract") or {}).get("forge") == "github":
        problems = _pr_evidence_problems(record, ws, evidence, None)
    elif ws.get("deliverable") == "published-branch":
        problems = _branch_evidence_problems(record, ws, evidence, None)
    else:
        raise CoordinatorError(f"workstream {ws['id']} delivers {ws.get('deliverable')!r}; it records no publication")
    problems += _fresh_problems(record, evidence.get("read_at"), "evidence")
    if problems:
        raise CoordinatorError(f"publication evidence for {ws['id']} fails: " + "; ".join(problems))
    if evidence.get("kind") == "pr" and ws.get("pr") is None:
        ws["pr"] = evidence["pr"]
    ws["publication"] = {**evidence, "recorded_at": _now_iso()}
    ws["updated_at"] = _now_iso()


def _coauthor_problems(record: dict, ws: dict, verifications: list, head: str | None) -> list[str]:
    if ws["kind"] != "ship" or not record["profile"]["delivery"]["forbid_agent_coauthors"]:
        return []
    for entry in verifications:
        if (
            isinstance(entry, dict)
            and entry.get("check") == "agent-coauthors"
            and entry.get("exit_code") == 0
            and entry.get("head_sha") == head
        ):
            return []
    return ["no passing agent-coauthors check on the result head (workspace_treehouse.py check-coauthors)"]


def _inventory_problems(record: dict, ws: dict, result: dict) -> list[str]:
    """A scout's report inventories every decision it raised, and only those."""
    inventory = result.get("decision_inventory")
    if inventory is None:
        return ["a scout result needs a decision_inventory (an empty list when it raised none)"]
    raised = {d["id"] for d in record["decisions"] if d.get("origin_workstream") == ws["id"]}
    problems = []
    foreign = sorted(set(inventory) - raised)
    if foreign:
        problems.append(f"decision_inventory names decision(s) {foreign} this scout did not raise")
    missing = sorted(raised - set(inventory))
    if missing:
        problems.append(f"decision_inventory omits decision(s) {missing} this scout raised")
    return problems


def _check_completion_evidence(record: dict, ws: dict, verifications: list) -> dict:
    """The owner's result proves the deliverable and the coordinator's own checks passed on it."""
    result, governing_problems = _governing_problems(ws)
    if result is None or result["outcome"] != "completed":
        reason = "; ".join(governing_problems) or f"the governing result is {result['outcome']!r}"
        raise CoordinatorError(f"workstream {ws['id']} cannot complete: {reason}")
    problems = governing_problems + _result_problems(record, ws, result)
    governing = _governing_request(ws)
    root = _TRANSACTION_ROOT.get()
    if governing and governing.get("body_digest") and root:
        problem = body_problem(root, record["run_id"], governing["body_digest"])
        if problem:
            problems.append(problem)
    if ws["kind"] == "ship" and not ws.get("deliverable"):
        problems.append("the workstream records no deliverable; set-deliverable first")
    problems += _publication_problems(record, ws, result.get("head_sha"))
    problems += _coauthor_problems(record, ws, verifications, result.get("head_sha"))
    if _restack_required(record, ws):
        problems.append("its stack parent's verified head moved; restack before completing")
    if not verifications:
        problems.append("the coordinator's own verification is required in the same call")
    for entry in verifications:
        if not isinstance(entry, dict) or not isinstance(entry.get("command"), str) or not entry["command"].strip():
            problems.append("each coordinator verification needs the command that was run")
            continue
        exit_code = entry.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code != 0:
            problems.append(f"coordinator verification {entry['command']!r} did not exit 0 (exit_code={exit_code!r})")
        if ws["kind"] == "ship" and entry.get("head_sha") != result.get("head_sha"):
            problems.append(
                f"coordinator verification {entry['command']!r} names head {entry.get('head_sha')!r}, "
                f"not the result head {result.get('head_sha')!r}"
            )
    if problems:
        raise CoordinatorError(f"workstream {ws['id']} cannot complete: " + "; ".join(problems))
    return result


def _cleanup_evidence_problems(record: dict, ws: dict, evidence: object) -> list[str]:
    """Why cleanup evidence fails to prove an `eligible` disposition, if it does."""
    if not isinstance(evidence, dict):
        return ["cleanup evidence must be a JSON object"]
    problems = []
    if evidence.get("clean_worktree") is not True:
        problems.append("clean_worktree must be exactly true")
    if evidence.get("landed_or_retained") is not True:
        problems.append("landed_or_retained must be exactly true")
    if not isinstance(evidence.get("detail"), str) or not evidence["detail"].strip():
        problems.append("detail must name the exact check that was run")
    try:
        checked_at = _parse_timestamp(evidence.get("checked_at"), "checked_at")
    except CoordinatorError as exc:
        problems.append(str(exc))
    else:
        now = _now()
        max_age = timedelta(hours=record["profile"]["limits"]["cleanup_evidence_max_age_hours"])
        if checked_at > now + timedelta(seconds=CLOCK_SKEW_SECONDS):
            problems.append("checked_at is in the future")
        elif now - checked_at > max_age:
            problems.append(f"checked_at is older than {max_age}")
        latest = [r["recorded_at"] for r in ws["results"] if r["status"] == "accepted"]
        if ws.get("landing"):
            latest.append(ws["landing"]["recorded_at"])
        if latest and checked_at < datetime.fromisoformat(max(latest)):
            problems.append("checked_at predates the workstream's latest result or landing")
    if ws.get("worktree") and evidence.get("worktree") != ws["worktree"]:
        problems.append(f"evidence worktree {evidence.get('worktree')!r} is not {ws['worktree']!r}")
    head = _verified_head(ws)
    if head and evidence.get("head_sha") != head:
        problems.append(f"evidence head_sha {evidence.get('head_sha')!r} is not the verified head {head!r}")
    return problems


# --- operation dispatch ---------------------------------------------------------------


def _op_add_workstream(record: dict, op: dict) -> None:
    _require_active(record)
    title = _require_str(op, "title")
    kind = _require_str(op, "kind")
    if kind not in WORKSTREAM_KINDS:
        raise CoordinatorError(f"kind must be one of {sorted(WORKSTREAM_KINDS)}, got {kind!r}")
    repository = _require_str(op, "repository")
    scope = _require_str(op, "scope")
    brief = _require_str(op, "brief")
    acceptance_criteria = op.get("acceptance_criteria")
    if not acceptance_criteria:
        raise CoordinatorError("acceptance_criteria is required and must be non-empty")
    delivery_authority = _require_str(op, "delivery_authority")
    permitted = op.get("permitted_operations")
    if not isinstance(permitted, list) or not all(isinstance(p, str) for p in permitted):
        raise CoordinatorError("permitted_operations must be a JSON array of action names")
    unknown_actions = [p for p in permitted if p not in AUTHORITY_ACTIONS]
    if unknown_actions:
        raise CoordinatorError(f"unknown permitted operation(s): {unknown_actions}")
    broader = sorted(set(permitted) - _granted_actions(record))
    if broader:
        raise CoordinatorError(
            f"workstream permissions {broader} exceed the run's granted authority; a workstream can only narrow it"
        )
    dependencies = op.get("dependencies") or []
    known_ids = {w["id"] for w in record["workstreams"]}
    unknown = [d for d in dependencies if d not in known_ids]
    if unknown:
        raise CoordinatorError(f"unknown dependency workstream id(s): {unknown}")
    stack_parent = op.get("stack_parent")
    if stack_parent is not None and stack_parent not in dependencies:
        raise CoordinatorError(
            f"stack parent {stack_parent!r} must also be listed in dependencies; a workstream "
            "can depend on several workstreams but stacks on exactly one"
        )
    deliverable, delivery_contract = _validate_deliverable(kind, permitted, op)
    references = op.get("user_references") or []
    if not isinstance(references, list):
        raise CoordinatorError("user_references must be a JSON array of {ref, digest} objects")
    for reference in references:
        _require_record_entry(reference, "user_references")
        _require_str(reference, "ref")
        _require_str(reference, "digest")
    if op.get("user_words") is not None:
        _require_str(op, "user_words")
    supersedes = op.get("supersedes")
    if supersedes is not None:
        old = _find_workstream(record, supersedes)
        if old["status"] != "completed" or old.get("landing") or old.get("superseded_by"):
            raise CoordinatorError(
                f"only a completed, unlanded workstream with no successor yet can be superseded; {supersedes} cannot"
            )
        _check_successor(old, kind, repository, op, permitted)
    from_scout = op.get("from_scout")
    if from_scout is not None:
        scout = _find_workstream(record, from_scout)
        if scout["kind"] != "scout" or scout["status"] != "completed":
            raise CoordinatorError(f"--from-scout must name a completed scout workstream; {from_scout} is not")
        if kind != "ship":
            raise CoordinatorError("promotion from a scout creates a ship workstream")
    now = _now_iso()
    record["workstreams"].append(
        {
            "id": _new_id("ws", known_ids),
            "title": title,
            "kind": kind,
            "repository": repository,
            "scope": scope,
            "brief": brief,
            "brief_digest": hashlib.sha256(brief.encode("utf-8")).hexdigest(),
            "acceptance_criteria": acceptance_criteria,
            "dependencies": dependencies,
            "stack_parent": stack_parent,
            "binding": None,
            "owner_type": None,
            "owner_id": None,
            "assignment_generation": 0,
            "assignment_history": [],
            "base_sha": None,
            "delivery_authority": delivery_authority,
            "permitted_operations": permitted,
            "worktree": None,
            "branch": None,
            "workspace": None,
            "status": "queued",
            "blocker": None,
            "dispatch_attempts": 0,
            "requests": [],
            "results": [],
            "deliverables": [],
            "verification": [],
            "corrections": [],
            "landing": None,
            "cleanup_disposition": None,
            "cleanup_evidence": None,
            **_v3_workstream_fields(),
            "deliverable": deliverable,
            "delivery_contract": delivery_contract,
            "user_words": op.get("user_words"),
            "user_references": references,
            "supersedes": supersedes,
            "from_scout": from_scout,
            "created_at": now,
            "updated_at": now,
        }
    )
    if supersedes:
        old = _find_workstream(record, supersedes)
        old["superseded_by"] = record["workstreams"][-1]["id"]
        # Accepted amendments are part of the intent the successor must satisfy.
        record["workstreams"][-1]["amendments"] = [
            {**copy.deepcopy(a), "inherited_from": supersedes, "results_before": 0} for a in old["amendments"]
        ]


def _check_successor(old: dict, kind: str, repository: str, op: dict, permitted: list[str]) -> None:
    """A successor carries the same work: kind, repository, intent, acceptance and delivery contract."""
    deliverable, contract = _validate_deliverable(kind, permitted, op)
    mismatched = [
        name
        for name, ours, theirs in (
            ("kind", kind, old["kind"]),
            ("repository", repository, old["repository"]),
            ("acceptance_criteria", op.get("acceptance_criteria"), old["acceptance_criteria"]),
            ("deliverable", deliverable, old.get("deliverable")),
            ("delivery_contract", contract, old.get("delivery_contract")),
            ("user_words", op.get("user_words"), old.get("user_words")),
            ("user_references", op.get("user_references") or [], old.get("user_references") or []),
        )
        if ours != theirs
    ]
    if mismatched:
        raise CoordinatorError(
            f"a successor of {old['id']} must carry the same {mismatched}; change intent with amend-intent instead"
        )


def _effective(record: dict, ws: dict) -> dict:
    """Follow successors to the workstream that currently carries this work."""
    seen = {ws["id"]}
    while ws.get("superseded_by") and ws["superseded_by"] not in seen:
        ws = _find_workstream(record, ws["superseded_by"])
        seen.add(ws["id"])
    return ws


def _validate_deliverable(kind: str, permitted: list[str], op: dict) -> tuple[str, dict | None]:
    """What a workstream must produce, which fixes the evidence its completion needs.

    A permission is never a deliverable: a run may allow pull requests while a workstream
    is asked only for a local branch.
    """
    deliverable = op.get("deliverable") or ("report" if kind == "scout" else None)
    if deliverable not in DELIVERABLES:
        raise CoordinatorError(f"deliverable must be one of {sorted(DELIVERABLES)}, got {deliverable!r}")
    if (kind == "scout") != (deliverable == "report"):
        raise CoordinatorError("a scout delivers a report, and only a scout does")
    needs = {"published-branch": {"push"}, "pr": {"push", "pr-create"}, "landed": {"push", "pr-create"}}
    missing = sorted(needs.get(deliverable, set()) - set(permitted))
    if missing:
        raise CoordinatorError(f"deliverable {deliverable!r} needs workstream permission(s) {missing}")
    contract = None
    if deliverable in {"pr", "landed"}:
        forge = op.get("forge") or "github"
        if forge not in FORGES:
            raise CoordinatorError(f"forge must be one of {sorted(FORGES)}")
        contract = {"forge": forge, "no_ci": bool(op.get("no_ci"))}
        if forge == "github":
            repo = _require_str(op, "forge_repo")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                raise CoordinatorError(f"forge_repo must be owner/name, got {repo!r}")
            contract["repo"] = repo
            contract["target_branch"] = _require_str(op, "target_branch")
    elif op.get("forge_repo") or op.get("target_branch"):
        raise CoordinatorError(f"a delivery contract applies only to pr or landed deliverables, not {deliverable!r}")
    return deliverable, contract


def _v3_workstream_fields() -> dict:
    """Workstream fields schema v3 added, with the empty values a new or migrated record starts from."""
    return {
        "verified_head": None,
        "wait": None,
        "wait_history": [],
        "base_history": [],
        "pending_admission": False,
        "deliverable": None,
        "delivery_contract": None,
        "pr": None,
        "publication": None,
        "landing_pending": None,
        "user_words": None,
        "user_references": [],
        "amendments": [],
        "permission_history": [],
        "events": [],
        "observations": [],
        "deliveries": [],
        "owner_identity": None,
        "runtime": None,
        "disposition": None,
        "supersedes": None,
        "superseded_by": None,
        "from_scout": None,
        "result_reconciliations": [],
    }


def _check_workspace_exclusive(record: dict, ws: dict, worktree: str) -> None:
    """One writable assignment per workspace; sequential reuse only at a verified boundary."""
    if ws["kind"] != "ship":
        return
    lane_reuse = record["profile"]["topology"]["lane_reuse"]
    for other in record["workstreams"]:
        if other["id"] == ws["id"] or other["kind"] != "ship" or other.get("worktree") != worktree:
            continue
        if other["status"] not in TERMINAL_WORKSTREAM_STATUSES:
            raise CoordinatorError(
                f"worktree {worktree} is already the writable workspace of active workstream "
                f"{other['id']}; concurrent writers need distinct workspaces"
            )
        if lane_reuse == "never":
            raise CoordinatorError(
                f"worktree {worktree} was used by {other['id']} and the run profile forbids lane reuse"
            )
        problems = _cleanup_evidence_problems(record, other, other.get("cleanup_evidence"))
        if problems:
            raise CoordinatorError(
                f"worktree {worktree} can be reused only after {other['id']}'s work is verified "
                "clean and retained: " + "; ".join(problems)
            )


def _op_assign(record: dict, op: dict) -> None:
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    if ws["status"] in TERMINAL_WORKSTREAM_STATUSES:
        raise CoordinatorError(f"workstream {ws['id']} is terminal and cannot be (re)assigned")
    binding = _require_str(op, "binding")
    if binding not in BINDINGS:
        raise CoordinatorError(f"unknown binding {binding!r}; expected one of {sorted(BINDINGS)}")
    runtime = BINDINGS[binding]["runtime"]
    coordinator_runtime = record.get("coordinator_runtime")
    if coordinator_runtime and runtime != coordinator_runtime and not op.get("runtime_instruction"):
        raise CoordinatorError(
            f"binding {binding!r} runs {runtime} owners but this coordinator is {coordinator_runtime}; "
            "a different runtime needs the user's instruction (--runtime-instruction)"
        )
    identity = op.get("owner_identity")
    if identity is not None:
        _require_record_entry(identity, "owner_identity")
        unknown = sorted(set(identity) - set(BINDINGS[binding]["identity_fields"]) - {"agent_id", "address"})
        if unknown:
            raise CoordinatorError(f"binding {binding!r} exposes no identity field(s) {unknown}")
    owner_type = _require_str(op, "owner_type")
    owner_id = _require_str(op, "owner_id")
    handover = None
    if ws["owner_id"] is not None and ws["status"] in ACTIVE_WORKSTREAM_STATUSES:
        # Replacing an owner mid-work is a handover: the previous owner and its subagents must
        # have stopped writing and their work must be retained, or two writers share the tree.
        if owner_id == ws["owner_id"] and binding == ws["binding"]:
            raise CoordinatorError(
                f"{owner_id} already owns {ws['id']}; the same owner keeps its assignment through repairs"
            )
        handover = {
            "reason": _require_str(op, "handover_reason"),
            "quiescence_evidence": _require_str(op, "quiescence_evidence"),
        }
        # A replacement is a new execution and spends exactly one attempt: at once when it
        # takes over running work, otherwise when the workstream next moves to working.
        if ws["status"] == "working":
            handover["admission"] = "at-assign"
        else:
            handover["admission"] = "reserved"
    if "worktree" in op:
        worktree = _require_str(op, "worktree")
        _check_workspace_exclusive(record, ws, worktree)
        if ws.get("workspace") and ws["workspace"]["path"] != worktree:
            raise CoordinatorError(
                f"workstream {ws['id']} already holds workspace {ws['workspace']['path']}; "
                "a different worktree needs a new workstream"
            )
        ws["worktree"] = worktree
    if "branch" in op:
        ws["branch"] = _require_str(op, "branch")
    if "base_sha" in op:
        base = _require_sha(op, "base_sha")
        established = ws.get("base_sha")
        if established and base != established and (ws["status"] in ACTIVE_WORKSTREAM_STATUSES or ws.get("workspace")):
            raise CoordinatorError(
                f"workstream {ws['id']} already runs on base {established}; change it with advance-base or restack"
            )
        ws["base_sha"] = base
    now = _now_iso()
    if ws["owner_id"] is not None:
        ws["assignment_history"].append(
            {
                "generation": ws["assignment_generation"],
                "binding": ws["binding"],
                "owner_type": ws["owner_type"],
                "owner_id": ws["owner_id"],
                "ended_at": now,
                "handover": handover,
            }
        )
    if handover and handover["admission"] == "at-assign":
        # The replacement starts executing at once, so it passes the same readiness as any start.
        _check_dispatch_ready(record, ws)
        if record["supervision"]["parked"]:
            raise CoordinatorError(f"run {record['run_id']} is parked; unpark it before replacing a running owner")
        _admit_attempt(record, ws, "replacing its owner again")
    elif handover:
        attempts_limit = record["profile"]["limits"]["max_dispatch_attempts"]
        if ws["dispatch_attempts"] >= attempts_limit:
            raise CoordinatorError(
                f"workstream {ws['id']} has used {ws['dispatch_attempts']} of {attempts_limit} "
                "dispatch attempts; open a decision before replacing its owner again"
            )
        ws["pending_admission"] = True
    ws["assignment_generation"] += 1
    ws["owner_identity"] = identity
    ws["runtime"] = {
        "kind": runtime,
        "model": op.get("runtime_model") or "unknown",
        "effort": op.get("runtime_effort") or "unknown",
        "instruction": op.get("runtime_instruction"),
    }
    ws["binding"] = binding
    ws["owner_type"] = owner_type
    ws["owner_id"] = owner_id
    ws["updated_at"] = now


_RECEIPT_STRINGS = (
    "manager",
    "repository_common_dir",
    "path",
    "lease_holder",
    "expected_base_sha",
    "observed_head_sha",
    "branch",
    "release_policy",
)


def _op_record_workspace(record: dict, op: dict) -> None:
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    if ws["status"] in TERMINAL_WORKSTREAM_STATUSES:
        raise CoordinatorError(f"workstream {ws['id']} is terminal")
    if ws["kind"] != "ship":
        raise CoordinatorError("only ship workstreams hold a writable workspace")
    receipt = op.get("receipt")
    _require_record_entry(receipt, "receipt")
    for key in _RECEIPT_STRINGS:
        if (
            key in {"expected_base_sha", "observed_head_sha"}
            and receipt.get("provisioning", {}).get("status") in {"blocked", "uncertain"}
            and not receipt.get(key)
        ):
            continue
        _require_str(receipt, key)
    provisioning = receipt.get("provisioning")
    _require_record_entry(provisioning, "provisioning")
    if provisioning.get("status") not in PROVISIONING_STATUSES:
        raise CoordinatorError(f"provisioning status must be one of {sorted(PROVISIONING_STATUSES)}")
    if not isinstance(receipt.get("lease_observed"), bool):
        raise CoordinatorError("receipt needs a boolean lease_observed")
    expected_holder = f"{record['run_id']}/{ws['id']}"
    if receipt["lease_holder"] != expected_holder:
        raise CoordinatorError(f"receipt lease holder {receipt['lease_holder']!r} is not {expected_holder!r}")
    if ws.get("worktree") and receipt["path"] != ws["worktree"]:
        raise CoordinatorError(f"receipt path {receipt['path']} is not the assigned worktree {ws['worktree']}")
    if ws.get("branch") and receipt["branch"] != ws["branch"]:
        raise CoordinatorError(f"receipt branch {receipt['branch']!r} is not the assigned branch {ws['branch']!r}")
    if not ws.get("worktree"):
        _check_workspace_exclusive(record, ws, receipt["path"])
        ws["worktree"] = receipt["path"]
    if not ws.get("branch"):
        ws["branch"] = receipt["branch"]
    if ws.get("base_sha") and receipt.get("expected_base_sha") and receipt["expected_base_sha"] != ws["base_sha"]:
        raise CoordinatorError(
            f"receipt base {receipt['expected_base_sha']!r} is not the assigned base {ws['base_sha']!r}"
        )
    if not ws.get("base_sha") and receipt.get("expected_base_sha"):
        ws["base_sha"] = _require_sha(receipt, "expected_base_sha")
    ws["workspace"] = {**receipt, "recorded_at": _now_iso()}
    ws["updated_at"] = _now_iso()


def _validate_source(source: object) -> dict:
    """Where a reply came from, which decides how much it can prove."""
    _require_record_entry(source, "source")
    kind = _require_str(source, "kind")
    if kind not in EVIDENCE_SOURCES:
        raise CoordinatorError(f"source kind must be one of {sorted(EVIDENCE_SOURCES)}, got {kind!r}")
    identity = source.get("identity")
    if kind == "runtime-read":
        _require_record_entry(identity, "source.identity")
        if not identity:
            raise CoordinatorError("a runtime-read source names the runtime identity it was read from")
    elif identity is not None:
        _require_record_entry(identity, "source.identity")
    return {"kind": kind, "identity": identity, "detail": source.get("detail")}


def _source_problem(ws: dict, source: dict, *, allow_coordinator: bool = False) -> str | None:
    """Why a source cannot carry authoritative effects for the current assignment, if it cannot."""
    kind = source["kind"]
    if kind == "reply-correlated":
        return None
    if kind == "runtime-read":
        recorded = ws.get("owner_identity") or {}
        if not recorded:
            return "the assignment recorded no runtime identity to bind a runtime read to"
        mismatched = sorted(k for k, v in recorded.items() if source["identity"].get(k) != v)
        return f"runtime read identity differs from the assignment on {mismatched}" if mismatched else None
    if kind == "coordinator-observed":
        return None if allow_coordinator else "an owner reply cannot be a coordinator observation"
    return "text-correlated evidence proves neither its sender nor its time; reconcile it with a runtime read"


def _validate_result_payload(payload: object) -> dict:
    _require_record_entry(payload, "result")
    request_id = _require_str(payload, "request_id")
    generation = _require_int(payload, "generation")
    outcome = _require_str(payload, "outcome")
    if outcome not in RESULT_OUTCOMES:
        raise CoordinatorError(f"result outcome must be one of {sorted(RESULT_OUTCOMES)}")
    artifacts = payload.get("artifacts", [])
    if not isinstance(artifacts, list) or not all(isinstance(a, str) and a.strip() for a in artifacts):
        raise CoordinatorError("result artifacts must be a list of non-empty strings")
    verification = payload.get("verification", [])
    if not isinstance(verification, list):
        raise CoordinatorError("result verification must be a list")
    for entry in verification:
        _require_record_entry(entry, "verification")
        _require_str(entry, "command")
        _require_int(entry, "exit_code")
    review = payload.get("review", [])
    if not isinstance(review, list):
        raise CoordinatorError("result review must be a list")
    for entry in review:
        _require_record_entry(entry, "review")
        _require_str(entry, "reviewer")
        if entry.get("status") not in REVIEW_STATUSES:
            raise CoordinatorError(f"review status must be one of {sorted(REVIEW_STATUSES)}")
    for key in ("head_sha", "base_sha"):
        if payload.get(key) is not None:
            _require_sha(payload, key)
    inventory = payload.get("decision_inventory")
    if inventory is not None and (not isinstance(inventory, list) or not all(isinstance(i, str) for i in inventory)):
        raise CoordinatorError("decision_inventory must be a list of decision ids")
    return {
        "schema": 2,
        "request_id": request_id,
        "generation": generation,
        "owner": _require_str(payload, "owner"),
        "source": _validate_source(payload.get("source")),
        "decision_inventory": inventory,
        "outcome": outcome,
        "summary": payload.get("summary"),
        "head_sha": payload.get("head_sha"),
        "base_sha": payload.get("base_sha"),
        "artifacts": artifacts,
        "verification": verification,
        "review": review,
    }


def _op_issue_request(record: dict, op: dict) -> None:
    """Record a brief or follow-up before it is sent, so its reply can be correlated exactly.

    A request belongs to one assignment generation and one dispatch attempt. A reply to a
    request from an earlier attempt or owner is kept as stale evidence, whatever generation
    the reply itself claims.
    """
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    if ws["status"] not in RESULT_ACCEPTING_STATUSES:
        raise CoordinatorError(
            f"workstream {ws['id']} is {ws['status']!r}; requests are issued only to a dispatched owner"
        )
    if ws["pending_admission"]:
        raise CoordinatorError(
            f"workstream {ws['id']} has a replacement owner awaiting admission; move it to working first"
        )
    request_id = _require_str(op, "request_id")
    purpose = _require_str(op, "purpose")
    if purpose not in REQUEST_PURPOSES:
        raise CoordinatorError(f"request purpose must be one of {sorted(REQUEST_PURPOSES)}, got {purpose!r}")
    root_request_id = None
    if purpose == RECOVERY_PURPOSE:
        root_request_id = _require_str(op, "root_request_id")
        root = next((r for r in _current_requests(ws) if r["request_id"] == root_request_id), None)
        if root is None or root.get("purpose") not in SUBSTANTIVE_PURPOSES:
            raise CoordinatorError(
                f"report-recovery needs a substantive root request of the current assignment, got {root_request_id!r}"
            )
        spent = [r["request_id"] for r in ws["requests"] if r.get("root_request_id") == root_request_id]
        if spent and request_id not in spent:
            raise CoordinatorError(
                f"request {root_request_id} already had its one report recovery ({spent[0]}); open a decision"
            )
    elif op.get("root_request_id"):
        raise CoordinatorError("only a report-recovery request names a root request")
    target_base = op.get("target_base_sha") or ws.get("base_sha")
    if target_base != ws.get("base_sha"):
        raise CoordinatorError(
            f"target base {target_base!r} is not the current base {ws.get('base_sha')!r}; record advance-base first"
        )
    answers = op.get("answers")
    if purpose == "answer":
        decision = _find(record["decisions"], _require_str(op, "answers"), "decision", record["run_id"])
        if decision["status"] != "resolved":
            raise CoordinatorError(f"decision {decision['id']} is {decision['status']!r}; resolve it before answering")
    elif answers:
        raise CoordinatorError("only an answer request names the decision it answers")
    body_digest = op.get("body_digest")
    if body_digest is not None:
        root = _TRANSACTION_ROOT.get()
        problem = body_problem(root, record["run_id"], body_digest) if root else "no state root in scope"
        if problem:
            raise CoordinatorError(problem)
    elif purpose not in {"brief", RECOVERY_PURPOSE}:
        raise CoordinatorError(f"a {purpose!r} request needs a stored body (--body-file or --body)")
    entry = {
        "request_id": request_id,
        "purpose": purpose,
        "body_digest": body_digest,
        "root_request_id": root_request_id,
        "brief_digest": op.get("brief_digest") or ws["brief_digest"],
        "target_base_sha": target_base,
        "answers": answers,
        "generation": ws["assignment_generation"],
        "dispatch_attempt": ws["dispatch_attempts"],
        "owner_id": ws["owner_id"],
    }
    for existing in ws["requests"]:
        if existing["request_id"] == request_id:
            if {k: existing.get(k) for k in entry} == entry:
                raise _NoChange()
            raise CoordinatorError(f"request id {request_id} was already issued with different details")
    now = _now()
    supervision = record["profile"]["supervision"]
    ws["requests"].append(
        {
            **entry,
            "issued_at": _iso(now),
            "expect_ack_by": _iso(
                now + timedelta(minutes=op.get("ack_window_minutes") or supervision["ack_window_minutes"])
            ),
            "expect_result_by": _iso(
                now + timedelta(minutes=op.get("result_window_minutes") or supervision["result_window_minutes"])
            ),
            "acked_at": None,
            "progress_at": None,
            "result_at": None,
        }
    )
    if answers:
        decision["answer_request_id"] = request_id
    ws["updated_at"] = _iso(now)


def _normalized_digest(payload: dict) -> str:
    """Digest of a reply's outcome, without the carrier request id or transport provenance."""
    return _digest({k: v for k, v in payload.items() if k not in {"request_id", "source"}})


def _correlation_problem(ws: dict, request: dict | None, request_id: str, generation: int, owner: str) -> str | None:
    if request is None:
        return f"request {request_id} was never issued for this workstream"
    if generation != request["generation"]:
        return (
            f"reply claims generation {generation} but request {request_id} was issued "
            f"for generation {request['generation']}"
        )
    if request["generation"] != ws["assignment_generation"] or request["dispatch_attempt"] != ws["dispatch_attempts"]:
        return (
            f"request {request_id} belongs to generation {request['generation']} attempt "
            f"{request['dispatch_attempt']}; current is generation {ws['assignment_generation']} "
            f"attempt {ws['dispatch_attempts']}"
        )
    if owner != ws["owner_id"] or owner != request["owner_id"]:
        return f"reply names owner {owner!r}, not the assigned owner {ws['owner_id']!r}"
    return None


def _op_record_result(record: dict, op: dict) -> None:
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    payload = _validate_result_payload(op.get("result"))
    digest = _digest(payload)
    if any(r["request_id"] == payload["request_id"] and r["digest"] == digest for r in ws["results"]):
        raise _NoChange()
    # A different reply to a request that already has one never replaces it; the chain rules
    # below keep it as a duplicate of the same outcome or as a conflict to reconcile.
    now = _now_iso()
    request = next((r for r in ws["requests"] if r["request_id"] == payload["request_id"]), None)
    attempt = request["dispatch_attempt"] if request else None
    entry = {
        **payload,
        "digest": digest,
        "normalized_digest": _normalized_digest(payload),
        "dispatch_attempt": attempt,
        "recorded_at": now,
    }
    stale = _correlation_problem(ws, request, payload["request_id"], payload["generation"], payload["owner"])
    if stale is None and ws["status"] not in RESULT_ACCEPTING_STATUSES:
        stale = f"workstream is {ws['status']!r} and accepts no new result"
    if stale is not None:
        entry["status"] = "stale"
        entry["rejected_reason"] = stale
    else:
        weak = _source_problem(ws, payload["source"])
        if weak is not None:
            entry["status"] = "uncertain"
            entry["rejected_reason"] = weak
        else:
            entry["status"] = _chain_status(ws, request, entry)
            if entry["status"] == "accepted":
                request["result_at"] = now
    ws["results"].append(entry)
    ws["updated_at"] = now


def _chain_status(ws: dict, request: dict, entry: dict) -> str:
    """Accepted when the chain has no canonical result yet; otherwise duplicate or conflict."""
    root = request.get("root_request_id") or request["request_id"]
    canonical, conflicted = _canonical_result(ws, root)
    if canonical is None and not conflicted:
        return "accepted"
    if canonical is not None and canonical["normalized_digest"] == entry["normalized_digest"]:
        return "duplicate"
    return "conflict"


def _wait_from_event(record: dict, ws: dict, op: dict) -> dict:
    """The same validated wait record a direct declaration produces, from an owner's waiting event."""
    if set(op) & {"wait_on", "clears_when", "source", "next_check_at", "until"}:
        raise CoordinatorError("--from-event takes its wait from the event; give no wait fields with it")
    event = next((e for e in ws["events"] if e["event_id"] == op["from_event"]), None)
    if event is None or event["kind"] != "waiting" or event["status"] != "accepted":
        raise CoordinatorError(f"{op['from_event']!r} is not an accepted waiting event of {ws['id']}")
    request = next((r for r in ws["requests"] if r["request_id"] == event["request_id"]), None)
    stale = _correlation_problem(ws, request, event["request_id"], event["generation"], event["owner"])
    if stale is None and event["attempt"] != ws["dispatch_attempts"]:
        stale = f"event belongs to attempt {event['attempt']}; current is {ws['dispatch_attempts']}"
    if stale:
        raise CoordinatorError(f"waiting event {event['event_id']} is not the current assignment's: {stale}")
    wait = dict(event["wait"])
    wait["source"] = f"event {event['event_id']}: {wait['source']}"
    return {**_declared_wait(record, ws, wait), "from_event": event["event_id"]}


def _validate_event(payload: object) -> dict:
    _require_record_entry(payload, "event")
    kind = _require_str(payload, "kind")
    if kind not in EVENT_KINDS:
        raise CoordinatorError(f"event kind must be one of {sorted(EVENT_KINDS)}, got {kind!r}")
    event = {
        "schema": 1,
        "event_id": _require_str(payload, "event_id"),
        "request_id": _require_str(payload, "request_id"),
        "generation": _require_int(payload, "generation"),
        "attempt": _require_int(payload, "attempt"),
        "owner": _require_str(payload, "owner"),
        "kind": kind,
        "text": _require_str(payload, "text"),
        "key": payload.get("key"),
        "wait": None,
        "source": _validate_source(payload.get("source")),
    }
    if kind == "waiting":
        wait = payload.get("wait")
        _require_record_entry(wait, "wait")
        event["wait"] = {k: wait.get(k) for k in ("wait_on", "clears_when", "next_check_at", "until")}
        event["wait"]["source"] = payload.get("text")
    elif payload.get("wait") is not None:
        raise CoordinatorError("only a waiting event carries a wait")
    return event


def _apply_event_effects(record: dict, ws: dict, event: dict, op: dict) -> None:
    """What an accepted owner event changes; uncertain and stale events change nothing."""
    request = next(r for r in ws["requests"] if r["request_id"] == event["request_id"])
    now = _now_iso()
    if event["kind"] == "ack" and request["acked_at"] is None:
        request["acked_at"] = now
    elif event["kind"] == "progress":
        request["progress_at"] = now
        if request["acked_at"] is None:
            request["acked_at"] = now
    elif event["kind"] in {"question", "blocked"}:
        defaults = (
            {"affected_workstreams": [], "gates": []}
            if ws["kind"] == "scout"
            else {"affected_workstreams": [ws["id"]], "gates": ["completion", "landing"]}
        )
        decision = _new_decision(
            record,
            {**op, "question": event["text"]},
            origin_workstream=ws["id"],
            origin_event=event["event_id"],
            defaults=defaults,
        )
        event["decision_id"] = decision["id"]
    elif event["kind"] == "waiting":
        # Resolve a relative next check once, at acceptance, so replaying the event cannot restart its clock.
        declared = _declared_wait(record, ws, {**event["wait"], "source": event["wait"]["source"] or event["text"]})
        event["wait"]["next_check_at"] = declared["next_check_at"]
        if declared["until"]:
            event["wait"]["until"] = declared["until"]


def _op_record_event(record: dict, op: dict) -> None:
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    event = _validate_event(op.get("event"))
    digest = _digest(event)
    for existing in ws["events"]:
        if existing["event_id"] == event["event_id"]:
            if existing["digest"] == digest:
                raise _NoChange()
            raise CoordinatorError(f"event {event['event_id']} was already recorded with a different payload")
    request = next((r for r in ws["requests"] if r["request_id"] == event["request_id"]), None)
    entry = {**event, "digest": digest, "recorded_at": _now_iso(), "decision_id": None}
    stale = _correlation_problem(ws, request, event["request_id"], event["generation"], event["owner"])
    if stale is None and request["dispatch_attempt"] != event["attempt"]:
        stale = f"event claims attempt {event['attempt']}, request was issued for attempt {request['dispatch_attempt']}"
    if stale is None and ws["status"] in TERMINAL_WORKSTREAM_STATUSES:
        stale = f"workstream is {ws['status']!r}; late evidence is kept without effect"
    if stale is not None:
        entry.update(status="stale", rejected_reason=stale)
    else:
        weak = _source_problem(ws, event["source"])
        if weak is not None:
            entry.update(status="uncertain", rejected_reason=weak)
        else:
            entry["status"] = "accepted"
            _apply_event_effects(record, ws, entry, op)
    ws["events"].append(entry)
    ws["updated_at"] = _now_iso()


def _op_reconcile_evidence(record: dict, op: dict) -> None:
    """Upgrade uncertain evidence after a fresh read bound to the assignment's recorded runtime."""
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    source = _validate_source({"kind": "runtime-read", "identity": op.get("identity")})
    problem = _source_problem(ws, source)
    if problem:
        raise CoordinatorError(problem)
    evidence = _require_str(op, "evidence")
    if op.get("event_id"):
        target = next((e for e in ws["events"] if e["event_id"] == op["event_id"]), None)
        kind = "event"
    else:
        wanted = _require_str(op, "request_id")
        target = next((r for r in ws["results"] if r["request_id"] == wanted and r["status"] == "uncertain"), None)
        kind = "result"
    if target is None or target["status"] != "uncertain":
        raise CoordinatorError(f"no uncertain {kind} matches; only uncertain evidence is reconciled")
    request = next((r for r in ws["requests"] if r["request_id"] == target["request_id"]), None)
    stale = _correlation_problem(ws, request, target["request_id"], target["generation"], target["owner"])
    if stale:
        raise CoordinatorError(f"the {kind} no longer matches the current assignment: {stale}")
    target["reconciled"] = {"from": target["source"], "evidence": evidence, "at": _now_iso()}
    target["source"] = {**source, "detail": evidence}
    target.pop("rejected_reason", None)
    if ws["status"] not in RESULT_ACCEPTING_STATUSES:
        # Reconciled late: the evidence is now attributed, but it changes nothing on a workstream
        # that has moved past accepting replies.
        target["status"] = "stale"
        target["rejected_reason"] = f"workstream is {ws['status']!r}; late evidence is kept without effect"
    elif kind == "event":
        target["status"] = "accepted"
        _apply_event_effects(record, ws, target, op)
    else:
        target["status"] = _chain_status(ws, request, target)
        if target["status"] == "accepted":
            request["result_at"] = _now_iso()
    ws["updated_at"] = _now_iso()


def _op_record_delivery(record: dict, op: dict) -> None:
    """Store a transport receipt checked against the assignment; delivery advances nothing."""
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    receipt = op.get("receipt")
    _require_record_entry(receipt, "receipt")
    problems = check_session_receipt(receipt, run_id=record["run_id"], workstream=ws)
    stale_markers = ("stale", "never issued", "earlier generation", "different run")
    if not problems:
        status = "accepted"
    elif any(marker in p for p in problems for marker in stale_markers):
        status = "stale"
    else:
        status = "uncertain"
    ws["deliveries"].append(
        {
            "schema": 1,
            "request_id": receipt.get("request_id"),
            "state": receipt.get("delivery_state"),
            "lifecycle": receipt.get("lifecycle_state"),
            "receipt": receipt,
            "problems": problems,
            "status": status,
            "recorded_at": _now_iso(),
        }
    )
    ws["updated_at"] = _now_iso()


def _op_observe(record: dict, op: dict) -> None:
    """A timestamped liveness reading bound to the assignment it was taken for; never a result."""
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    lifecycle = op.get("lifecycle")
    if lifecycle not in LIFECYCLE_OBSERVATIONS:
        raise CoordinatorError(f"lifecycle must be one of {sorted(LIFECYCLE_OBSERVATIONS)}")
    observed_at = _parse_timestamp(op.get("observed_at"), "observed_at")
    if observed_at > _now() + timedelta(seconds=CLOCK_SKEW_SECONDS):
        raise CoordinatorError("observed_at is in the future")
    entry = {
        "owner": _require_str(op, "owner"),
        "binding": _require_str(op, "binding"),
        "generation": _require_int(op, "generation"),
        "attempt": _require_int(op, "attempt"),
        "observed_at": _iso(observed_at),
        "recorded_at": _now_iso(),
        "lifecycle": lifecycle,
        "source": _require_str(op, "source"),
        "evidence": _require_str(op, "evidence"),
    }
    current = (ws["owner_id"], ws["binding"], ws["assignment_generation"], ws["dispatch_attempts"])
    observed = (entry["owner"], entry["binding"], entry["generation"], entry["attempt"])
    entry["status"] = "accepted" if observed == current else "stale"
    ws["observations"].append(entry)
    ws["updated_at"] = entry["recorded_at"]


def _op_ack_due(record: dict, op: dict) -> None:
    """Schedule when a handled due item is next due, without resolving what it reports."""
    _require_active(record)
    key = _require_str(op, "key")
    when = _parse_when(op.get("next_check_at"), "next_check_at")
    bound = timedelta(minutes=record["profile"]["supervision"]["max_wait_check_minutes"])
    if when <= _now() or when - _now() > bound + timedelta(seconds=CLOCK_SKEW_SECONDS):
        raise CoordinatorError(f"next_check_at must be in the future and within {bound}")
    record["supervision"]["due_acks"][key] = {"next_check_at": _iso(when), "note": op.get("note"), "at": _now_iso()}


def _op_record_sweep(record: dict, op: dict) -> None:
    """Schedule the next fleet review, doubling the interval while nothing changes."""
    _require_active(record)
    changed = op.get("changed")
    if not isinstance(changed, bool):
        raise CoordinatorError("record-sweep needs --changed true or false")
    supervision, limits = record["supervision"], record["profile"]["supervision"]
    previous = supervision["sweep_interval_minutes"]
    if changed or previous is None:
        interval = limits["heartbeat_initial_minutes"]
    else:
        interval = min(previous * 2, limits["heartbeat_max_minutes"])
    supervision["sweep_interval_minutes"] = interval
    supervision["next_sweep_at"] = _iso(_now() + timedelta(minutes=interval))


def _op_park(record: dict, op: dict) -> None:
    _require_active(record)
    record["supervision"]["parked"] = {
        "reason": _require_str(op, "reason"),
        "resume_when": _require_str(op, "resume_when"),
        "at": _now_iso(),
    }


def _op_unpark(record: dict, op: dict) -> None:
    _require_active(record)
    if record["supervision"]["parked"] is None:
        raise _NoChange()
    _require_str(op, "reason")
    record["supervision"]["parked"] = None


def _op_reconcile_result(record: dict, op: dict) -> None:
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    root = _require_str(op, "request_id")
    if _chain_root(ws, root) != root:
        raise CoordinatorError(f"{root} is a recovery request; name its root request")
    chosen = _require_str(op, "choose")
    results = _chain_results(ws, root)
    if not any(r["status"] == "conflict" for r in results):
        raise CoordinatorError(f"request {root} has no conflicting replies to reconcile")
    if not any(r["normalized_digest"] == chosen for r in results):
        raise CoordinatorError(f"no reply to request {root}'s chain has normalized digest {chosen!r}")
    ws["result_reconciliations"].append(
        {
            "root_request_id": root,
            "chosen": chosen,
            "evidence": _require_str(op, "evidence"),
            "results_before": len(ws["results"]),
            "at": _now_iso(),
        }
    )
    ws["updated_at"] = _now_iso()


def _declared_wait(record: dict, ws: dict, op: dict) -> dict:
    """A wait names who owes the next move, what clears it, the evidence, and when to look again.

    The next check is always bounded, even when nobody knows when the condition clears (a
    usage limit with no stated reset, a reviewer that may never arrive).
    """
    wait_on = _require_str(op, "wait_on")
    if wait_on not in WAIT_PARTIES and wait_on not in {w["id"] for w in record["workstreams"] if w["id"] != ws["id"]}:
        raise CoordinatorError(
            f"wait_on must be one of {sorted(WAIT_PARTIES)} or another workstream id, got {wait_on!r}"
        )
    now = _now()
    next_check = _parse_when(op.get("next_check_at"), "next_check_at")
    bound = timedelta(minutes=record["profile"]["supervision"]["max_wait_check_minutes"])
    if next_check <= now:
        raise CoordinatorError("next_check_at must be in the future")
    if next_check - now > bound + timedelta(seconds=CLOCK_SKEW_SECONDS):
        raise CoordinatorError(f"next_check_at is more than the profile's {bound} away; a wait is always rechecked")
    until = op.get("until")
    return {
        "from_event": None,
        "wait_on": wait_on,
        "clears_when": _require_str(op, "clears_when"),
        "source": _require_str(op, "source"),
        "next_check_at": _iso(next_check),
        "until": _iso(_parse_when(until, "until")) if until else None,
        "declared_at": _iso(now),
    }


def _op_update_workstream(record: dict, op: dict) -> None:
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    changes = set(op) - {"op", "id", "next_action", "session_id"}
    wait_fields = {"wait_on", "clears_when", "source", "next_check_at", "until", "from_event"}
    if changes & wait_fields and op.get("status") != "waiting":
        raise CoordinatorError("wait fields are given only with --status waiting")
    if not changes:
        raise CoordinatorError("update-workstream requires at least one change")
    if ws["status"] in TERMINAL_WORKSTREAM_STATUSES and changes & RESTRICTED_ON_TERMINAL_WORKSTREAM:
        raise CoordinatorError(
            f"workstream {ws['id']} is terminal; only verification evidence or a correction may be appended"
        )
    now = _now_iso()
    if "status" in op:
        target = op["status"]
        if target not in WORKSTREAM_STATUSES:
            raise CoordinatorError(f"unknown workstream status {target!r}")
        if target == ws["status"]:
            raise _NoChange()
        allowed = WORKSTREAM_TRANSITIONS.get(ws["status"], set())
        if target not in allowed:
            raise CoordinatorError(f"workstream {ws['id']} cannot move from {ws['status']!r} to {target!r}")
        blocker = op.get("blocker", ws.get("blocker"))
        if target == "blocked" and not blocker:
            raise CoordinatorError("moving a workstream to blocked requires a blocker reason")
        if target == "failed" and ws["status"] in {"queued", "waiting"} and not blocker:
            raise CoordinatorError(
                f"failing a workstream from {ws['status']!r} requires a blocker reason, because "
                "nothing else in the record explains why it ended"
            )
        if target == "working":
            _check_dispatch_ready(record, ws)
            if ws["status"] in {"queued", "review-ready"} or ws["pending_admission"]:
                if record["supervision"]["parked"]:
                    raise CoordinatorError(
                        f"run {record['run_id']} is parked; unpark it on the user's go-ahead before dispatching"
                    )
                _admit_attempt(record, ws, "dispatching again")
        if target == "review-ready":
            result, problems = _governing_problems(ws)
            if result is None or result["outcome"] != "completed" or problems:
                reason = "; ".join(problems) or f"the governing result is {result['outcome']!r}"
                raise CoordinatorError(
                    f"workstream {ws['id']} needs an accepted completed result for generation "
                    f"{ws['assignment_generation']} before review-ready ({reason}); idle or done is a "
                    "lifecycle signal and records no result"
                )
            _check_gate(record, ws, "completion", "become review-ready")
        if target == "completed":
            _check_gate(record, ws, "completion", "complete")
            result = _check_completion_evidence(record, ws, op.get("verifications") or [])
            ws["verified_head"] = result.get("head_sha")
        if target == "waiting":
            ws["wait"] = _wait_from_event(record, ws, op) if op.get("from_event") else _declared_wait(record, ws, op)
        elif ws["status"] == "waiting":
            ws["wait_history"].append({**ws["wait"], "ended_at": now, "ended_by": target})
            ws["wait"] = None
        ws["status"] = target
    if "blocker" in op:
        ws["blocker"] = op["blocker"]
    if op.get("clear_blocker"):
        ws["blocker"] = None
    for deliverable in op.get("deliverables", []):
        _require_record_entry(deliverable, "deliverables")
        ws["deliverables"].append({**deliverable, "recorded_at": now})
    for verification in op.get("verifications", []):
        _require_record_entry(verification, "verifications")
        ws["verification"].append({**verification, "recorded_at": now})
    if "cleanup_disposition" in op:
        disposition = op["cleanup_disposition"]
        if disposition not in CLEANUP_DISPOSITIONS:
            raise CoordinatorError(f"unknown cleanup disposition {disposition!r}")
        evidence = op.get("cleanup_evidence")
        if disposition == "eligible":
            if ws["status"] not in TERMINAL_WORKSTREAM_STATUSES:
                raise CoordinatorError(
                    f"workstream {ws['id']} is {ws['status']!r}; a workspace becomes eligible for cleanup "
                    "only after the workstream is terminal and its owner has stopped writing"
                )
            problems = _cleanup_evidence_problems(record, ws, evidence)
            if problems:
                raise CoordinatorError(
                    "an eligible cleanup disposition needs fresh evidence bound to this "
                    "workstream: " + "; ".join(problems)
                )
        ws["cleanup_disposition"] = disposition
        ws["cleanup_evidence"] = evidence
    if "correct" in op:
        correction = op["correct"]
        ws["corrections"].append(
            {
                "field": _require_str(correction, "field"),
                "note": _require_str(correction, "note"),
                "recorded_at": now,
            }
        )
    ws["updated_at"] = now


def _open_denials(record: dict, action: str, ws_id: str) -> list[str]:
    return [
        d["id"]
        for d in record["denials"]
        if d["status"] == "open" and d["action"] == action and d.get("workstream") in {ws_id, None}
    ]


def _op_record_landing(record: dict, op: dict) -> None:
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    if ws["status"] != "completed":
        raise CoordinatorError(
            f"workstream {ws['id']} is {ws['status']!r}; landing is recorded only after verified completion"
        )
    if ws.get("landing"):
        raise CoordinatorError(f"workstream {ws['id']} already has a landing record")
    _check_gate(record, ws, "landing", "land")
    if ws.get("superseded_by"):
        raise CoordinatorError(f"workstream {ws['id']} is superseded by {ws['superseded_by']}; it cannot land")
    if _restack_required(record, ws):
        raise CoordinatorError(f"workstream {ws['id']} is stacked on a parent head that moved; it cannot land")
    if "merge" not in _granted_actions(record):
        raise CoordinatorError("the run's authority withholds merge; landing is not authorized")
    if "merge" not in ws["permitted_operations"]:
        raise CoordinatorError(f"workstream {ws['id']} is not permitted to merge")
    blocking = _open_denials(record, "merge", ws["id"])
    if blocking:
        raise CoordinatorError(
            f"merge denial(s) {blocking} are unresolved; a denied action is resolved through "
            "the host approval path; another session must not run it"
        )
    if ws.get("deliverable") not in {"pr", "landed"}:
        raise CoordinatorError(
            f"workstream {ws['id']} delivers {ws.get('deliverable')!r}; only a pr or landed deliverable lands"
            + (" (set-deliverable first)" if ws.get("deliverable") is None else "")
        )
    landing = op.get("landing")
    _require_record_entry(landing, "landing")
    forge_problems = _forge_landing_problems(record, ws, landing)
    if forge_problems:
        raise CoordinatorError(f"landing of {ws['id']} is not proven: " + "; ".join(forge_problems))
    executor = _require_str(landing, "executor")
    if record["profile"]["delivery"]["merge"] == "coordinator" and executor != "coordinator":
        raise CoordinatorError("the run profile reserves merges for the coordinator")
    method = _require_str(landing, "method")
    if method not in LANDING_METHODS:
        raise CoordinatorError(f"landing method must be one of {sorted(LANDING_METHODS)}")
    entry = {
        "executor": executor,
        "method": method,
        "target_ref": _require_str(landing, "target_ref"),
        "merged_sha": _require_sha(landing, "merged_sha"),
        "evidence": _require_str(landing, "evidence"),
        "pr": landing.get("pr"),
        "pre_merge": landing.get("pre_merge"),
        "forge": landing.get("forge"),
        "landed_head": ws.get("verified_head"),
        "recorded_at": _now_iso(),
    }
    if method in {"squash", "rebase"}:
        # A squash or rebase breaks ancestry from the feature head, so cleanup later needs a
        # retained ref proving the original head survives somewhere recoverable.
        retained = _require_str(landing, "retained_ref")
        branch = ws.get("branch")
        if retained.startswith("refs/heads/") or retained in {"HEAD", branch, f"refs/heads/{branch}"}:
            raise CoordinatorError(
                f"retained_ref {retained!r} is a local branch the workspace reset can lose; keep the "
                "original head on a remote-tracking ref or a ref outside refs/heads/"
            )
        entry["retained_ref"] = retained
    ws["landing"] = entry
    ws["landing_pending"] = None
    ws["updated_at"] = entry["recorded_at"]


def _forge_landing_problems(record: dict, ws: dict, landing: dict) -> list[str]:
    """For a GitHub delivery, the merge is proven by pre-merge and post-merge reads of one PR."""
    contract = ws.get("delivery_contract") or {}
    if contract.get("forge") != "github":
        return []
    head = ws.get("verified_head")
    pre, post = landing.get("pre_merge"), landing.get("forge")
    if not isinstance(pre, dict) or not isinstance(post, dict):
        return ["a GitHub landing needs pre_merge (pr-evidence) and forge (landing-evidence) objects"]
    problems = [f"pre-merge: {p}" for p in _pr_evidence_problems(record, ws, pre, head)]
    expect = {"schema": 1, "kind": "landing", "source": "gh", "merged": True, "uncertain": False}
    for key, value in expect.items():
        if post.get(key) != value:
            problems.append(f"landing evidence {key} is {post.get(key)!r}, expected {value!r}")
    if post.get("source_head") != head:
        problems.append(f"merged head {post.get('source_head')!r} is not the verified head {head!r}")
    if str(post.get("repo", "")).lower() != str(contract.get("repo", "")).lower() or post.get("pr") != pre.get("pr"):
        problems.append("landing evidence names a different repository or PR than the pre-merge read")
    if post.get("base_ref") != contract.get("target_branch"):
        problems.append(f"merged into {post.get('base_ref')!r}, not {contract.get('target_branch')!r}")
    if post.get("target_contains_merge_commit") is not True:
        problems.append("the target branch does not contain the merge commit")
    if landing.get("merged_sha") != post.get("merge_commit"):
        problems.append("merged_sha is not the merge commit GitHub reports")
    try:
        pre_at = _parse_timestamp(pre.get("read_at"), "pre_merge read_at")
        merged_at = _parse_timestamp(post.get("merged_at"), "merged_at")
        post_at = _parse_timestamp(post.get("read_at"), "landing read_at")
    except CoordinatorError as exc:
        return problems + [str(exc)]
    if not pre_at <= merged_at <= post_at:
        problems.append("chronology must be pre_merge.read_at <= merged_at <= landing.read_at")
    max_age = timedelta(minutes=record["profile"]["delivery"]["evidence_max_age_minutes"])
    if merged_at - pre_at > max_age or post_at - merged_at > max_age:
        problems.append(f"the reads around the merge are more than {max_age} from it")
    return problems


def _op_record_landing_pending(record: dict, op: dict) -> None:
    """A merge whose outcome is queued or unknown: re-read it, never merge it again blindly."""
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    if ws["status"] != "completed" or ws.get("landing"):
        raise CoordinatorError(f"workstream {ws['id']} is not a completed, unlanded workstream")
    ws["landing_pending"] = {
        "command": _require_str(op, "command"),
        "outcome": _require_str(op, "outcome"),
        "at": _now_iso(),
    }


def _op_record_denial(record: dict, op: dict) -> None:
    _require_active(record)
    action = _require_str(op, "action")
    if action not in AUTHORITY_ACTIONS:
        raise CoordinatorError(f"unknown action {action!r}")
    ws_id = op.get("workstream")
    if ws_id is not None:
        _find_workstream(record, ws_id)
    executed = op.get("executed")
    if executed not in {"no", "unknown"}:
        raise CoordinatorError("denial 'executed' must be 'no' or 'unknown'")
    record["denials"].append(
        {
            "id": _new_id("den", {d["id"] for d in record["denials"]}),
            "action": action,
            "workstream": ws_id,
            "executor": _require_str(op, "executor"),
            "command": _require_str(op, "command"),
            "denial": _require_str(op, "denial"),
            "executed": executed,
            "status": "open",
            "resolution": None,
            "resolved_at": None,
            "recorded_at": _now_iso(),
        }
    )


def _op_resolve_denial(record: dict, op: dict) -> None:
    _require_active(record)
    denial = _find(record["denials"], _require_str(op, "id"), "denial", record["run_id"])
    if denial["status"] != "open":
        raise CoordinatorError(f"denial {denial['id']} is already resolved")
    executed = op.get("executed")
    if executed not in {"yes", "no"}:
        raise CoordinatorError(
            "resolving a denial needs 'executed' reconciled to 'yes' or 'no'; an uncertain "
            "outcome is reconciled before anything is retried"
        )
    denial["status"] = "resolved"
    denial["resolution"] = {
        "path": _require_str(op, "resolution"),
        "evidence": _require_str(op, "evidence"),
        "executed": executed,
    }
    denial["resolved_at"] = _now_iso()


def _validate_scope(record: dict, affected: object, gates: object) -> tuple[list[str], list[str]]:
    affected = affected or []
    gates = gates or []
    if not isinstance(affected, list) or not all(isinstance(a, str) for a in affected):
        raise CoordinatorError("affected_workstreams must be a list of workstream ids")
    known = {w["id"] for w in record["workstreams"]}
    unknown = sorted(set(affected) - known)
    if unknown:
        raise CoordinatorError(f"unknown affected workstream(s): {unknown}")
    if not isinstance(gates, list) or not set(gates) <= DECISION_GATES:
        raise CoordinatorError(f"gates must be a subset of {sorted(DECISION_GATES)}")
    return affected, gates


def _new_decision(record: dict, op: dict, *, origin_workstream=None, origin_event=None, defaults=None) -> dict:
    defaults = defaults or {}
    responsibility = op.get("responsibility") or defaults.get("responsibility", "user")
    if responsibility not in DECISION_RESPONSIBILITY:
        raise CoordinatorError(f"responsibility must be one of {sorted(DECISION_RESPONSIBILITY)}")
    affected, gates = _validate_scope(
        record,
        op.get("affected_workstreams", defaults.get("affected_workstreams")),
        op.get("gates", defaults.get("gates")),
    )
    defer_until = op.get("defer_until")
    decision = {
        "id": _new_id("dec", {d["id"] for d in record["decisions"]}),
        "question": _require_str(op, "question"),
        "context": op.get("context"),
        "status": "open",
        "options": op.get("options"),
        "responsibility": responsibility,
        "origin_workstream": origin_workstream,
        "origin_event": origin_event,
        "affected_workstreams": affected,
        "gates": gates,
        "defer_until": _iso(_parse_when(defer_until, "defer_until")) if defer_until else None,
        "scope_history": [],
        "resolution": None,
        "evidence": None,
        "answer_request_id": None,
        "opened_at": _now_iso(),
        "resolved_at": None,
    }
    record["decisions"].append(decision)
    return decision


def _op_open_decision(record: dict, op: dict) -> None:
    _require_active(record)
    origin = op.get("origin_workstream")
    if origin is not None:
        _find_workstream(record, origin)
    _new_decision(record, op, origin_workstream=origin)


def _op_scope_decision(record: dict, op: dict) -> None:
    _require_active(record)
    decision = _find(record["decisions"], _require_str(op, "id"), "decision", record["run_id"])
    if decision["status"] != "open":
        raise CoordinatorError(
            f"decision {decision['id']} is {decision['status']!r}; only an open decision is rescoped"
        )
    affected, gates = _validate_scope(record, op.get("affected_workstreams"), op.get("gates"))
    defer_until = op.get("defer_until")
    decision["scope_history"].append(
        {
            "affected_workstreams": decision["affected_workstreams"],
            "gates": decision["gates"],
            "defer_until": decision["defer_until"],
            "reason": _require_str(op, "reason"),
            "at": _now_iso(),
        }
    )
    decision["affected_workstreams"] = affected
    decision["gates"] = gates
    if "defer_until" in op:
        decision["defer_until"] = _iso(_parse_when(defer_until, "defer_until")) if defer_until else None


def _op_resolve_decision(record: dict, op: dict) -> None:
    """Resolve with recorded provenance: the user's own words for a user's choice."""
    _require_active(record)
    decision = _find(record["decisions"], _require_str(op, "id"), "decision", record["run_id"])
    if decision["status"] != "open":
        raise CoordinatorError(f"decision {decision['id']} is already {decision['status']!r}")
    status = op.get("status")
    if status not in {"resolved", "superseded"}:
        raise CoordinatorError("resolve-decision status must be 'resolved' or 'superseded'")
    provenance = op.get("provenance")
    if provenance not in DECISION_PROVENANCE:
        raise CoordinatorError(f"provenance must be one of {sorted(DECISION_PROVENANCE)}")
    responsibility = decision.get("responsibility") or "user"
    if provenance == "tactical" and responsibility != "coordinator":
        raise CoordinatorError(
            f"decision {decision['id']} belongs to the user; resolve it with the user's words or as moot with evidence"
        )
    if provenance == "user-words":
        _require_str(op, "user_words")
    decision["status"] = status
    decision["resolution"] = {
        "provenance": provenance,
        "text": _require_str(op, "resolution"),
        "user_words": op.get("user_words"),
        "evidence": _require_str(op, "evidence"),
        "at": _now_iso(),
    }
    decision["evidence"] = op["evidence"]
    decision["resolved_at"] = _now_iso()


def _check_base_evidence(evidence: object, required: dict[str, tuple[str, str]]) -> list[dict]:
    """Coordinator-run checks, each exiting 0, asserting every named relation between commits."""
    if not isinstance(evidence, list) or not evidence:
        raise CoordinatorError("evidence must be a non-empty JSON array of coordinator-run checks")
    found = set()
    for entry in evidence:
        _require_record_entry(entry, "evidence")
        _require_str(entry, "command")
        if entry.get("exit_code") != 0 or isinstance(entry.get("exit_code"), bool):
            raise CoordinatorError(f"evidence {entry['command']!r} did not exit 0")
        assertion = entry.get("asserts")
        if assertion in required:
            expected = required[assertion]
            if (entry.get("subject"), entry.get("object")) != expected:
                raise CoordinatorError(
                    f"evidence for {assertion!r} names {entry.get('subject')!r} and {entry.get('object')!r}, "
                    f"expected {expected[0]!r} and {expected[1]!r}"
                )
            found.add(assertion)
    missing = sorted(set(required) - found)
    if missing:
        raise CoordinatorError(f"evidence is missing assertion(s) {missing}")
    return evidence


def _op_advance_base(record: dict, op: dict) -> None:
    """Move a workstream's base forward with recorded proof; acquisition facts stay as they were."""
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    if ws["status"] in TERMINAL_WORKSTREAM_STATUSES:
        raise CoordinatorError(f"workstream {ws['id']} is terminal; a successor carries any rebase")
    old = _require_full_sha(op, "old_base")
    new = _require_full_sha(op, "new_base")
    head = _require_full_sha(op, "workspace_head")
    if old != ws.get("base_sha"):
        raise CoordinatorError(f"old base {old!r} is not the current base {ws.get('base_sha')!r}")
    if old == new:
        raise CoordinatorError("the new base equals the current base")
    required = {"contains": (head, new)}
    replan = op.get("replan_decision")
    if replan:
        decision = _find(record["decisions"], replan, "decision", record["run_id"])
        if decision["status"] != "resolved":
            raise CoordinatorError(f"replan decision {replan} is not resolved")
    else:
        required["ancestry"] = (new, old)
    evidence = _check_base_evidence(op.get("evidence"), required)
    ws["base_history"].append(
        {
            "kind": "advance",
            "old": old,
            "new": new,
            "workspace_head": head,
            "evidence": evidence,
            "replan_decision": replan,
            "reason": _require_str(op, "reason"),
            "at": _now_iso(),
        }
    )
    ws["base_sha"] = new
    ws["updated_at"] = _now_iso()


def _op_restack(record: dict, op: dict) -> None:
    """Rebase a stacked child onto its parent's new authoritative head, within accepted intent."""
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    if not ws.get("stack_parent"):
        raise CoordinatorError(f"workstream {ws['id']} has no stack parent")
    if ws["status"] in TERMINAL_WORKSTREAM_STATUSES:
        raise CoordinatorError(
            f"workstream {ws['id']} is terminal; add a successor with --supersedes to carry the restack"
        )
    old = _require_full_sha(op, "old_parent_head")
    new = _require_full_sha(op, "new_parent_head")
    if old != ws.get("base_sha"):
        raise CoordinatorError(f"old parent head {old!r} is not the child's current base {ws.get('base_sha')!r}")
    parent_head = _stack_parent_head(record, ws)
    if new != parent_head:
        raise CoordinatorError(f"new parent head {new!r} is not the parent's authoritative head {parent_head!r}")
    head = None
    evidence = None
    if ws.get("workspace"):
        head = _require_full_sha(op, "workspace_head")
        evidence = _check_base_evidence(op.get("evidence"), {"contains": (head, new)})
    ws["base_history"].append(
        {
            "kind": "restack",
            "old": old,
            "new": new,
            "workspace_head": head,
            "evidence": evidence,
            "reason": op.get("reason") or f"stack parent {ws['stack_parent']} moved",
            "at": _now_iso(),
        }
    )
    ws["base_sha"] = new
    ws["updated_at"] = _now_iso()


def _op_amend_intent(record: dict, op: dict) -> None:
    """Append the user's later words verbatim; earlier results no longer satisfy the amended intent."""
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    if ws["status"] in TERMINAL_WORKSTREAM_STATUSES:
        raise CoordinatorError(f"workstream {ws['id']} is terminal; a successor carries a new ask")
    request_id = _require_str(op, "request_id")
    if not any(r["request_id"] == request_id for r in ws["requests"]):
        raise CoordinatorError(f"request {request_id} was never issued for {ws['id']}")
    ws["amendments"].append(
        {
            "id": _new_id("amd", {a["id"] for a in ws["amendments"]}),
            "at": _now_iso(),
            "user_words": _require_str(op, "user_words"),
            "coordinator_instruction": op.get("coordinator_instruction"),
            "request_id": request_id,
            "results_before": len(ws["results"]),
        }
    )
    ws["updated_at"] = _now_iso()


def _op_amend_authority(record: dict, op: dict) -> None:
    """Change one run grant on the user's own words, keeping the previous value."""
    _require_active(record)
    action = _require_str(op, "action")
    if action not in AUTHORITY_ACTIONS:
        raise CoordinatorError(f"unknown authority action {action!r}")
    to = op.get("to")
    if to not in {"granted", "withheld"}:
        raise CoordinatorError("amend-authority --to must be 'granted' or 'withheld'")
    user_words = _require_str(op, "user_words")
    grant = next(g for g in record["authority"]["grants"] if g["action"] == action)
    granted = to == "granted"
    if grant["granted"] is granted:
        raise _NoChange()
    record["authority"]["amendments"].append(
        {
            "id": _new_id("auth", {a["id"] for a in record["authority"]["amendments"]}),
            "at": _now_iso(),
            "action": action,
            "from": "granted" if grant["granted"] else "withheld",
            "to": to,
            "user_words": user_words,
            "previous_instruction": grant["instruction"],
        }
    )
    grant["granted"] = granted
    grant["instruction"] = user_words


# Actions that act after completion, so a named terminal workstream may still gain them.
_POST_COMPLETION_ACTIONS = {"merge", "release", "cleanup"}


def _op_amend_permissions(record: dict, op: dict) -> None:
    """Widen or narrow one named workstream's permissions within the current run grants."""
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    action = _require_str(op, "action")
    if action not in AUTHORITY_ACTIONS:
        raise CoordinatorError(f"unknown action {action!r}")
    change = op.get("change")
    if change not in {"add", "remove"}:
        raise CoordinatorError("amend-permissions needs --add or --remove")
    if ws["status"] in TERMINAL_WORKSTREAM_STATUSES and action not in _POST_COMPLETION_ACTIONS:
        raise CoordinatorError(
            f"workstream {ws['id']} is terminal; only {sorted(_POST_COMPLETION_ACTIONS)} can still change"
        )
    user_words = _require_str(op, "user_words")
    if change == "add":
        if action not in _granted_actions(record):
            raise CoordinatorError(f"the run withholds {action!r}; amend-authority first")
        if action in ws["permitted_operations"]:
            raise _NoChange()
        ws["permitted_operations"].append(action)
    else:
        if action not in ws["permitted_operations"]:
            raise _NoChange()
        ws["permitted_operations"].remove(action)
    ws["permission_history"].append(
        {
            "id": _new_id("perm", {h["id"] for h in ws["permission_history"]}),
            "at": _now_iso(),
            "action": action,
            "change": change,
            "user_words": user_words,
        }
    )
    ws["updated_at"] = _now_iso()


def _op_set_deliverable(record: dict, op: dict) -> None:
    """Name the deliverable of a migrated workstream that predates deliverables."""
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    if ws.get("deliverable"):
        raise CoordinatorError(f"workstream {ws['id']} already delivers {ws['deliverable']!r}")
    _require_str(op, "user_words")
    ws["deliverable"], ws["delivery_contract"] = _validate_deliverable(ws["kind"], ws["permitted_operations"], op)
    ws["corrections"].append(
        {"field": "deliverable", "note": f"set to {ws['deliverable']!r}: {op['user_words']}", "recorded_at": _now_iso()}
    )


def _op_record_disposition(record: dict, op: dict) -> None:
    """Record what happens to a completed workstream's artifact, and who follows it up."""
    _require_active(record)
    ws = _find_workstream(record, _require_str(op, "id"))
    if ws["status"] != "completed":
        raise CoordinatorError(f"workstream {ws['id']} is {ws['status']!r}; a disposition follows completion")
    disposition = op.get("disposition")
    if disposition not in DISPOSITIONS:
        raise CoordinatorError(f"disposition must be one of {sorted(DISPOSITIONS)}")
    if disposition == "landed" and not ws.get("landing"):
        raise CoordinatorError(f"workstream {ws['id']} has no landing record")
    if disposition == "superseded" and not ws.get("superseded_by"):
        raise CoordinatorError(f"workstream {ws['id']} has no successor; add one with --supersedes")
    if ws["deliverable"] == "landed" and disposition not in {"landed", "superseded"}:
        raise CoordinatorError(f"workstream {ws['id']} was asked to land; its disposition is landed or superseded")
    ws["disposition"] = {
        "disposition": disposition,
        "artifact": _require_str(op, "artifact"),
        "follow_up_owner": _require_str(op, "follow_up_owner"),
        "source_instruction": _require_str(op, "source_instruction"),
        "successor": ws.get("superseded_by"),
        "at": _now_iso(),
    }
    ws["updated_at"] = _now_iso()


def _mentions_id(text: str, stable_id: str) -> bool:
    """Whether `text` names `stable_id` as a whole token, so "ws-1" never matches "ws-10"."""
    pattern = rf"(?<![\w-]){re.escape(stable_id)}(?![\w-])"
    return re.search(pattern, text) is not None


def _op_close(record: dict, op: dict) -> None:
    _require_active(record)
    outcome = _require_str(op, "outcome")
    unfinished = [w["id"] for w in record["workstreams"] if w["status"] not in TERMINAL_WORKSTREAM_STATUSES]
    if unfinished:
        raise CoordinatorError(f"cannot close: workstream(s) not terminal: {unfinished}")
    open_decisions = [d["id"] for d in record["decisions"] if d["status"] == "open"]
    if open_decisions:
        raise CoordinatorError(f"cannot close: decision(s) still open: {open_decisions}")
    open_denials = [d["id"] for d in record["denials"] if d["status"] == "open"]
    if open_denials:
        raise CoordinatorError(f"cannot close: denial(s) not reconciled: {open_denials}")
    failed = [w["id"] for w in record["workstreams"] if w["status"] == "failed"]
    unacknowledged = [wid for wid in failed if not _mentions_id(outcome, wid)]
    if unacknowledged:
        raise CoordinatorError(f"cannot close: outcome does not acknowledge failed workstream(s): {unacknowledged}")
    undisposed = [
        w["id"]
        for w in record["workstreams"]
        if w["kind"] == "ship" and w["status"] == "completed" and not w.get("disposition")
    ]
    if undisposed:
        raise CoordinatorError(
            f"cannot close: completed ship workstream(s) without an integration disposition: {undisposed}"
        )
    unlanded = [
        w["id"]
        for w in record["workstreams"]
        if w.get("deliverable") == "landed"
        and w["status"] == "completed"
        and not w.get("landing")
        and not w.get("superseded_by")
    ]
    if unlanded:
        raise CoordinatorError(f"cannot close: workstream(s) asked to land have no landing: {unlanded}")
    missing_disposition = [
        w["id"]
        for w in record["workstreams"]
        if w["kind"] == "ship" and w.get("worktree") and not w.get("cleanup_disposition")
    ]
    if missing_disposition:
        raise CoordinatorError(f"cannot close: ship worktree(s) missing a cleanup disposition: {missing_disposition}")
    record["status"] = "completed"
    record["outcome"] = outcome


def _op_abandon(record: dict, op: dict) -> None:
    _require_active(record)
    record["status"] = "abandoned"
    record["outcome"] = _require_str(op, "reason")


def _op_claim(record: dict, op: dict) -> None:
    """Claim a run, idempotently for the holder; a forced takeover is a recorded handover.

    Unknown liveness is not proof of death, so forcing requires the caller to name the
    holder it expects to replace (a compare-and-swap against a concurrent takeover), a
    reason, and the liveness evidence it actually checked.
    """
    _require_active(record)
    session_id = _require_str(op, "session_id")
    current = record.get("claimed_by")
    now = _now_iso()
    if current and current["session_id"] == session_id:
        raise _NoChange()
    generation = (current["generation"] if current else record["claim_history"][-1]["generation"]) + 1
    event = {"event": "claim", "session_id": session_id, "generation": generation, "at": now}
    if current:
        if not op.get("force"):
            raise CoordinatorError(
                f"run {record['run_id']} is already claimed by {current['session_id']!r} "
                f"since {current['claimed_at']}; ask the holder to release it, or force a "
                "handover with --expected-holder, --reason and --liveness-evidence"
            )
        expected = _require_str(op, "expected_holder")
        if expected != current["session_id"]:
            raise CoordinatorError(
                f"expected holder {expected!r} but the run is held by {current['session_id']!r}; "
                "re-read the run before forcing"
            )
        event.update(
            {
                "event": "forced-handover",
                "previous": current["session_id"],
                "reason": _require_str(op, "reason"),
                "liveness_evidence": _require_str(op, "liveness_evidence"),
            }
        )
    record["claimed_by"] = {"session_id": session_id, "claimed_at": now, "generation": generation}
    record["claim_history"].append(event)


def _op_release_claim(record: dict, op: dict) -> None:
    _require_active(record)
    _require_holder(record, op)
    holder = record["claimed_by"]
    record["claim_history"].append(
        {
            "event": "release",
            "session_id": holder["session_id"],
            "generation": holder["generation"],
            "reason": _require_str(op, "reason"),
            "at": _now_iso(),
        }
    )
    record["claimed_by"] = None


_HANDLERS = {
    "add-workstream": _op_add_workstream,
    "assign": _op_assign,
    "record-workspace": _op_record_workspace,
    "issue-request": _op_issue_request,
    "record-result": _op_record_result,
    "update-workstream": _op_update_workstream,
    "record-landing": _op_record_landing,
    "record-denial": _op_record_denial,
    "resolve-denial": _op_resolve_denial,
    "open-decision": _op_open_decision,
    "scope-decision": _op_scope_decision,
    "resolve-decision": _op_resolve_decision,
    "reconcile-result": _op_reconcile_result,
    "record-event": _op_record_event,
    "reconcile-evidence": _op_reconcile_evidence,
    "record-delivery": _op_record_delivery,
    "record-publication": _op_record_publication,
    "record-landing-pending": _op_record_landing_pending,
    "observe": _op_observe,
    "ack-due": _op_ack_due,
    "record-sweep": _op_record_sweep,
    "park": _op_park,
    "unpark": _op_unpark,
    "advance-base": _op_advance_base,
    "restack": _op_restack,
    "amend-intent": _op_amend_intent,
    "amend-authority": _op_amend_authority,
    "amend-permissions": _op_amend_permissions,
    "set-deliverable": _op_set_deliverable,
    "record-disposition": _op_record_disposition,
    "close": _op_close,
    "abandon": _op_abandon,
    "claim": _op_claim,
    "release-claim": _op_release_claim,
}
# Claim and release-claim check the holder themselves; everything else must come from it.
_HOLDER_EXEMPT = {"claim", "release-claim"}


def _apply_op(record: dict, op: dict) -> None:
    kind = op.get("op")
    if kind not in _HOLDER_EXEMPT and (kind in _HANDLERS or kind == "set-next-action"):
        _require_holder(record, op)
    handler = _HANDLERS.get(kind)
    if handler is not None:
        handler(record, op)
    elif kind == "set-next-action":
        _require_active(record)
        record["next_action"] = _require_str(op, "text")
    else:
        raise CoordinatorError(f"unknown operation {kind!r}")


# --- transactions --------------------------------------------------------------------


_REQUIRED_RECORD_KEYS = _V1_REQUIRED_KEYS | {
    "authority",
    "profile",
    "profile_digest",
    "denials",
    "claim_history",
    "coordinator_runtime",
    "supervision",
}


# Every key a v2 operation reads. A record missing one (a hand edit, or a shape from an
# unreleased development build) is refused as corrupt before any operation touches it.
_REQUIRED_WORKSTREAM_KEYS = {
    "id",
    "kind",
    "status",
    "dependencies",
    "stack_parent",
    "permitted_operations",
    "assignment_generation",
    "assignment_history",
    "base_sha",
    "brief_digest",
    "workspace",
    "requests",
    "results",
    "dispatch_attempts",
    "deliverables",
    "verification",
    "corrections",
    "landing",
    "cleanup_disposition",
    "cleanup_evidence",
} | set(_v3_workstream_fields())


def _load_locked(path: Path) -> tuple[dict, str]:
    raw = path.read_text(encoding="utf-8")
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CorruptRecordError(path) from exc
    if isinstance(record, dict) and record.get("schema_version") not in {None, SCHEMA_VERSION}:
        raise CoordinatorError(
            f"run record at {path} has schema_version {record.get('schema_version')!r}; this "
            f"ledger reads version {SCHEMA_VERSION} only and refuses to touch it (migrate a copy with "
            "import-v1 or migrate-v2)"
        )
    if (
        not isinstance(record, dict)
        or not _REQUIRED_RECORD_KEYS.issubset(record)
        or record.get("run_id") != path.stem
        or not isinstance(record.get("workstreams"), list)
        or not isinstance(record.get("decisions"), list)
        or not isinstance(record.get("denials"), list)
        or any(not isinstance(w, dict) or not _REQUIRED_WORKSTREAM_KEYS.issubset(w) for w in record["workstreams"])
        or any(not isinstance(d, dict) or "id" not in d or "status" not in d for d in record["decisions"])
        or any(not isinstance(d, dict) or "id" not in d or "status" not in d for d in record["denials"])
    ):
        raise CorruptRecordError(path)
    return record, raw


def _transact(root: Path, run_id: str, ops: list[dict], base_revision: int | None, *, batch: bool) -> dict:
    path = run_path(root, run_id)
    if not path.is_file():
        raise CoordinatorError(f"no run {run_id} at {path}")
    fd = _open_locked(path)
    try:
        record, raw = _load_locked(path)
        if base_revision is not None and record.get("revision") != base_revision:
            raise RevisionMismatchError(
                f"base revision {base_revision} does not match current revision "
                f"{record.get('revision')} for run {run_id}" + ("; re-read and rebuild the batch" if batch else "")
            )
        working = copy.deepcopy(record)
        token = _TRANSACTION_ROOT.set(root)
        try:
            for op in ops:
                if "body" in op:
                    if op.get("op") != "issue-request":
                        raise CoordinatorError(f"only issue-request carries a body, not {op.get('op')!r}")
                    op["body_digest"] = store_body(root, run_id, op.pop("body"))
            changed = _apply_ops(working, ops, batch)
        finally:
            _TRANSACTION_ROOT.reset(token)
        if not changed:
            return record
        working["revision"] = record["revision"] + 1
        working["updated_at"] = _now_iso()
        _validate_record(working)
        _commit(root, path, working, previous_text=raw)
        return working
    finally:
        os.close(fd)


def _apply_ops(working: dict, ops: list[dict], batch: bool) -> bool:
    changed = False
    for index, op in enumerate(ops):
        if batch and op.get("op") not in BATCH_ALLOWED_OPS:
            raise CoordinatorError(f"op {index} ({op.get('op')!r}) is not permitted inside a batch")
        try:
            _apply_op(working, op)
        except _NoChange:
            continue
        changed = True
        next_action = op.get("next_action")
        if next_action is not None:
            working["next_action"] = next_action
    return changed


def _load_snapshot(root: Path, run_id: str) -> dict:
    path = run_path(root, run_id)
    if not path.is_file():
        raise CoordinatorError(f"no run {run_id} at {path}")
    fd = _open_locked(path, exclusive=False)
    try:
        record, _ = _load_locked(path)
        return record
    finally:
        os.close(fd)


def _abandon(root: Path, run_id: str, reason: str, session_id: str, base_revision: int | None) -> dict:
    path = run_path(root, run_id)
    if not path.is_file():
        raise CoordinatorError(f"no run {run_id} at {path}")
    op = {"op": "abandon", "reason": reason, "session_id": session_id}
    try:
        return _transact(root, run_id, [op], base_revision, batch=False)
    except CorruptRecordError:
        return _abandon_corrupt(root, path, run_id, reason, session_id)


def _abandon_corrupt(root: Path, path: Path, run_id: str, reason: str, session_id: str) -> dict:
    if not isinstance(reason, str) or not reason.strip():
        raise CoordinatorError("'reason' is required and must be a non-empty string")
    fd = _open_locked(path)
    try:
        raw = path.read_text(encoding="utf-8")
        quarantine = _plain_file_path(root, f"{run_id}.json.corrupt-{int(time.time())}", "quarantine copy")
        # Exclusive creation without following links: the raw bytes are kept before anything
        # replaces the corrupt record, and an existing entry of that name is never overwritten.
        try:
            qfd = os.open(quarantine, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError as exc:
            raise CoordinatorError(f"quarantine copy {quarantine} already exists; retry in a second") from exc
        with os.fdopen(qfd, "w", encoding="utf-8") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        record = _new_run_record(
            run_id=run_id,
            title=f"(recovered from a corrupt record; raw preserved at {quarantine.name})",
            intent="(unrecoverable)",
            coordination_reason="(unrecoverable)",
            delivery_authority="(unrecoverable)",
            grants=[],
            profile={},
            profile_digest="",
            session_id=session_id,
            next_action="",
        )
        record.update({"status": "abandoned", "outcome": reason, "claimed_by": None, "recovered_from_corruption": True})
        _validate_record(record)
        _commit(root, path, record, previous_text=None)
        return record
    finally:
        os.close(fd)


# --- listing and rendering ------------------------------------------------------------


def _summarize(root: Path, record: dict) -> dict:
    counts: dict[str, int] = {}
    for w in record["workstreams"]:
        counts[w["status"]] = counts.get(w["status"], 0) + 1
    return {
        "run_id": record["run_id"],
        "title": record["title"],
        "status": record["status"],
        "workstream_counts": counts,
        "open_decisions": [d["id"] for d in record["decisions"] if d["status"] == "open"],
        "open_denials": [d["id"] for d in record.get("denials", []) if d["status"] == "open"],
        "blocked_workstreams": [w["id"] for w in record["workstreams"] if w["status"] == "blocked"],
        "claimed_by": record.get("claimed_by"),
        "next_action": record.get("next_action", ""),
        "updated_at": record.get("updated_at"),
        "path": str(run_path(root, record["run_id"])),
    }


def request_with_body(root: Path, record: dict, ws_id: str | None, request_id: str) -> dict:
    """One request and the full instruction text it carried, for a self-contained rebrief."""
    if not ws_id:
        raise CoordinatorError("--request needs --id naming its workstream")
    ws = _find_workstream(record, ws_id)
    request = next((r for r in ws["requests"] if r["request_id"] == request_id), None)
    if request is None:
        raise CoordinatorError(f"no request {request_id} in {ws_id}")
    digest = request.get("body_digest")
    if digest is None:
        return {"request": request, "body": ws["brief"], "body_source": "workstream brief"}
    problem = body_problem(root, record["run_id"], digest)
    if problem:
        raise CoordinatorError(problem)
    path = body_path(root, record["run_id"], digest)
    return {"request": request, "body": path.read_text(encoding="utf-8"), "body_path": str(path)}


def _item(kind: str, subject: str, ref: str, due_at: str | None, detail: str) -> dict:
    return {"key": f"{kind}:{subject}:{ref}", "kind": kind, "subject": subject, "due_at": due_at, "detail": detail}


def _passed(stamp: str | None, now: datetime) -> bool:
    return stamp is not None and datetime.fromisoformat(stamp) <= now


def due_items(root: Path | None, record: dict, now: datetime | None = None) -> list[dict]:
    """Actionable supervision items for current outstanding expectations, read-only.

    A waiting workstream is governed by its wait, a request with a canonical terminal result
    has no reply deadline, a deferred decision surfaces at its deferral, and an item the
    coordinator scheduled with ack-due stays quiet until then.
    """
    now = now or _now()
    if record["status"] != "active":
        return []
    items: list[dict] = []
    supervision = record["supervision"]
    limits = record["profile"]["supervision"]
    for ws in record["workstreams"]:
        wid = ws["id"]
        governing = _governing_request(ws)
        if ws["status"] in ACTIVE_WORKSTREAM_STATUSES and governing is not None:
            rid = governing["request_id"]
            result, conflicted = _canonical_result(ws, rid)
            recoveries = [r for r in ws["requests"] if r.get("root_request_id") == rid]
            if conflicted:
                items.append(_item("conflicting-result", wid, rid, None, f"request {rid} has conflicting replies"))
            elif result is None and ws["status"] == "working":
                if governing["acked_at"] is None and _passed(governing["expect_ack_by"], now):
                    items.append(_item("ack-overdue", wid, rid, governing["expect_ack_by"], "no acknowledgement"))
                if recoveries and _passed(recoveries[-1]["expect_result_by"], now):
                    items.append(
                        _item("recovery-exhausted", wid, rid, recoveries[-1]["expect_result_by"], "open a decision")
                    )
                elif not recoveries and _passed(governing["expect_result_by"], now):
                    items.append(
                        _item("result-overdue", wid, rid, governing["expect_result_by"], "one report recovery left")
                    )
        if ws["status"] == "waiting" and ws.get("wait") and _passed(ws["wait"]["next_check_at"], now):
            items.append(
                _item(
                    "wait-check", wid, ws["wait"]["declared_at"], ws["wait"]["next_check_at"], ws["wait"]["clears_when"]
                )
            )
        if ws["status"] not in {"failed"} and _restack_required(record, ws) and not ws.get("superseded_by"):
            items.append(_item("restack-required", wid, str(ws.get("base_sha")), None, "stack parent head moved"))
        if ws.get("landing_pending") and not ws.get("landing"):
            items.append(_item("landing-pending", wid, ws["landing_pending"]["at"], None, "re-read the merge outcome"))
        if ws["kind"] == "ship" and ws["status"] == "completed" and not ws.get("disposition"):
            items.append(_item("missing-disposition", wid, "-", None, "record an integration disposition"))
        if root is not None:
            for request in _current_requests(ws):
                if request.get("body_digest") and body_problem(root, record["run_id"], request["body_digest"]):
                    items.append(_item("missing-body", wid, request["request_id"], None, "request body unreadable"))
        if ws["status"] == "queued" and ws.get("owner_id"):
            try:
                _check_dispatch_ready(record, ws)
            except CoordinatorError:
                pass
            else:
                if ws["dispatch_attempts"] < record["profile"]["limits"]["max_dispatch_attempts"]:
                    items.append(_item("dispatchable", wid, "-", None, "ready to start"))
    overdue = timedelta(minutes=limits["decision_overdue_minutes"])
    for decision in record["decisions"]:
        if decision["status"] != "open":
            continue
        if decision.get("defer_until"):
            if _passed(decision["defer_until"], now):
                items.append(
                    _item("decision-deferral", decision["id"], "-", decision["defer_until"], decision["question"])
                )
        elif now - datetime.fromisoformat(decision["opened_at"]) >= overdue:
            due_at = _iso(datetime.fromisoformat(decision["opened_at"]) + overdue)
            items.append(_item("decision-overdue", decision["id"], "-", due_at, decision["question"]))
    if any(w["status"] in ACTIVE_WORKSTREAM_STATUSES for w in record["workstreams"]):
        next_sweep = supervision["next_sweep_at"] or _iso(
            datetime.fromisoformat(record["created_at"]) + timedelta(minutes=limits["heartbeat_initial_minutes"])
        )
        if _passed(next_sweep, now):
            items.append(_item("sweep", "run", "-", next_sweep, "review the fleet"))
    acks = supervision["due_acks"]
    return [i for i in items if not (i["key"] in acks and not _passed(acks[i["key"]]["next_check_at"], now))]


def watch_beacon_path(root: Path, run_id: str) -> Path:
    return _plain_file_path(root, f"{run_path(root, run_id).stem}.watch", "watch beacon")


def turnend_status(root: Path, record: dict, session_id: str) -> dict:
    """Whether the holder may end its turn: parked, idle, or watched by a live helper."""
    active = [w["id"] for w in record["workstreams"] if w["status"] in ACTIVE_WORKSTREAM_STATUSES]
    holder = record.get("claimed_by") or {}
    if record["status"] != "active" or not active:
        return {"ok": True, "reason": "no active workstreams"}
    if record["supervision"]["parked"]:
        return {"ok": True, "reason": "run is parked", "parked": record["supervision"]["parked"]}
    if holder.get("session_id") != session_id:
        return {"ok": False, "reason": f"session {session_id!r} does not hold the run"}
    path = watch_beacon_path(root, record["run_id"])
    try:
        beacon = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "reason": "no readable watch beacon", "active": active}
    problems = []
    if beacon.get("state") != "running":
        problems.append(f"watcher is {beacon.get('state')!r} ({beacon.get('exit_reason')})")
    if (beacon.get("session_id"), beacon.get("claim_generation")) != (session_id, holder.get("generation")):
        problems.append("watcher belongs to another holder or claim generation")
    live = identity_is_live(beacon.get("process_identity"))
    if live is not True:
        problems.append("watcher process is gone or unverifiable" if live is False else "watcher identity unknown")
    try:
        last = datetime.fromisoformat(beacon["last_cycle_at"])
        if _now() - last > timedelta(seconds=2 * int(beacon["interval"])):
            problems.append("watcher has not cycled within twice its interval")
    except (KeyError, TypeError, ValueError):
        problems.append("watch beacon lacks its cycle time or interval")
    return {"ok": not problems, "reason": "; ".join(problems) or "watched", "active": active}


def list_runs(root: Path) -> list[dict]:
    directory = runs_dir(root, create=False)
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("schema_version") != SCHEMA_VERSION:
                raise CoordinatorError("unsupported schema version")
            out.append(_summarize(root, record))
        except (json.JSONDecodeError, OSError, KeyError, TypeError, AttributeError, CoordinatorError):
            out.append({"run_id": path.stem, "status": "corrupt", "path": str(path)})
    return out


def render_snapshot_text(root: Path, record: dict) -> str:
    summary = _summarize(root, record)
    lines = [
        f"Run {summary['run_id']}: {summary['title']} [{summary['status']}]",
        f"Workstreams: {summary['workstream_counts'] or '(none)'}",
    ]
    if summary["blocked_workstreams"]:
        lines.append(f"Blocked: {', '.join(summary['blocked_workstreams'])}")
    if summary["open_decisions"]:
        lines.append(f"Open decisions: {', '.join(summary['open_decisions'])}")
    if summary["open_denials"]:
        lines.append(f"Open denials: {', '.join(summary['open_denials'])}")
    holder = summary["claimed_by"]
    lines.append(
        f"Claimed by: {holder['session_id']} (generation {holder['generation']})"
        if holder
        else "Claimed by: (released)"
    )
    lines.append(f"Next action: {summary['next_action'] or '(none)'}")
    lines.append(f"Record: {summary['path']}")
    return "\n".join(lines)


def _emit(root: Path, record: dict, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(render_snapshot_text(root, record))


# --- CLI ------------------------------------------------------------------------------


def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--state-root", default=None, help="absolute state root (or COORDINATE_STATE_ROOT)")
    sp.add_argument("--format", choices=["text", "json"], default="json")


def _add_run_mutators(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--session-id", required=True)
    sp.add_argument("--base-revision", type=int, default=None)
    sp.add_argument("--next-action", default=None)
    _add_common(sp)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="coordinator_state.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("start", help="create a new coordinator run")
    s.add_argument("--title", required=True)
    s.add_argument("--intent", required=True)
    s.add_argument("--coordination-reason", required=True)
    s.add_argument("--delivery-authority", required=True)
    s.add_argument("--authority", required=True, help="JSON array granting or withholding every action")
    s.add_argument("--profile", required=True, help="run profile TOML")
    s.add_argument("--session-id", required=True)
    s.add_argument("--coordinator-runtime", required=True, choices=sorted(COORDINATOR_RUNTIMES))
    s.add_argument("--next-action", default="")
    _add_common(s)

    aw = sub.add_parser("add-workstream")
    _add_run_mutators(aw)
    aw.add_argument("--title", required=True)
    aw.add_argument("--kind", choices=sorted(WORKSTREAM_KINDS), required=True)
    aw.add_argument("--repository", required=True)
    aw.add_argument("--scope", required=True)
    aw.add_argument("--brief", required=True)
    aw.add_argument("--acceptance-criteria", required=True, help="JSON array or string")
    aw.add_argument("--dependencies", default="[]", help="JSON array of workstream ids")
    aw.add_argument("--stack-parent", default=None)
    aw.add_argument("--delivery-authority", required=True)
    aw.add_argument("--permitted-operations", required=True, help="JSON array of granted actions")
    aw.add_argument("--deliverable", default=None, help="report, local-branch, published-branch, pr or landed")
    aw.add_argument("--forge", default=None, help="github (default for pr/landed) or none")
    aw.add_argument("--forge-repo", default=None, help="owner/name of the repository the PR targets")
    aw.add_argument("--target-branch", default=None)
    aw.add_argument("--no-ci", action="store_true", help="the repository runs no CI for this deliverable")
    aw.add_argument("--user-words", default=None, help="the user's request, verbatim")
    aw.add_argument("--user-references", default=None, help="JSON array of {ref, digest} the user pointed at")
    aw.add_argument("--supersedes", default=None, help="completed workstream this one replaces")
    aw.add_argument("--from-scout", default=None, help="completed scout this ship promotes")

    asg = sub.add_parser("assign")
    _add_run_mutators(asg)
    asg.add_argument("--id", required=True)
    asg.add_argument("--binding", required=True)
    asg.add_argument("--owner-type", required=True)
    asg.add_argument("--owner-id", required=True)
    asg.add_argument("--worktree", default=None)
    asg.add_argument("--branch", default=None)
    asg.add_argument("--base-sha", default=None)
    asg.add_argument("--handover-reason", default=None)
    asg.add_argument("--quiescence-evidence", default=None)
    asg.add_argument("--owner-identity", default=None, help="JSON identity fields the binding exposes")
    asg.add_argument("--runtime-instruction", default=None, help="the user's words choosing another runtime")
    asg.add_argument("--runtime-model", default=None)
    asg.add_argument("--runtime-effort", default=None)

    rw = sub.add_parser("record-workspace")
    _add_run_mutators(rw)
    rw.add_argument("--id", required=True)
    rw.add_argument("--receipt", required=True, help="JSON workspace receipt")

    iq = sub.add_parser("issue-request")
    _add_run_mutators(iq)
    iq.add_argument("--id", required=True)
    iq.add_argument("--request-id", required=True)
    iq.add_argument("--purpose", required=True, help="one of: " + ", ".join(sorted(REQUEST_PURPOSES)))
    iq.add_argument("--brief-digest", default=None)
    iq.add_argument("--target-base-sha", default=None)
    iq.add_argument("--root", dest="root_request_id", default=None, help="root request of a report-recovery")
    iq.add_argument("--answers", default=None, help="decision id an answer request delivers")
    iq.add_argument("--ack-window-minutes", type=int, default=None)
    iq.add_argument("--result-window-minutes", type=int, default=None)
    body = iq.add_mutually_exclusive_group()
    body.add_argument("--body", default=None, help="instruction text, stored content-addressed")
    body.add_argument("--body-file", default=None, help="file holding the instruction text")

    rr = sub.add_parser("record-result")
    _add_run_mutators(rr)
    rr.add_argument("--id", required=True)
    rr.add_argument("--result", required=True, help="JSON result receipt")

    uw = sub.add_parser("update-workstream")
    _add_run_mutators(uw)
    uw.add_argument("--id", required=True)
    uw.add_argument("--status", choices=sorted(WORKSTREAM_STATUSES), default=None)
    uw.add_argument("--blocker", default=None)
    uw.add_argument("--clear-blocker", action="store_true")
    uw.add_argument("--deliverable", action="append", default=[], help="JSON object, repeatable")
    uw.add_argument("--verification", action="append", default=[], help="JSON object, repeatable")
    uw.add_argument("--cleanup-disposition", choices=sorted(CLEANUP_DISPOSITIONS), default=None)
    uw.add_argument("--cleanup-evidence", default=None, help="JSON object")
    uw.add_argument("--correct-field", default=None)
    uw.add_argument("--correct-note", default=None)
    uw.add_argument("--wait-on", default=None, help="owner, user, ci, reviewer, quota or a workstream id")
    uw.add_argument("--clears-when", default=None)
    uw.add_argument("--source", dest="wait_source", default=None, help="the observation behind the wait")
    uw.add_argument("--next-check-at", default=None, help="ISO-8601 with offset, or +Nm / +Nh")
    uw.add_argument("--until", default=None, help="when the condition is known to clear")
    uw.add_argument("--from-event", default=None, help="take the wait from an accepted waiting event")

    rl = sub.add_parser("record-landing")
    _add_run_mutators(rl)
    rl.add_argument("--id", required=True)
    rl.add_argument("--landing", required=True, help="JSON landing record")

    rdn = sub.add_parser("record-denial")
    _add_run_mutators(rdn)
    rdn.add_argument("--action", required=True, choices=AUTHORITY_ACTIONS)
    rdn.add_argument("--workstream", default=None)
    rdn.add_argument("--executor", required=True)
    rdn.add_argument("--command", dest="denied_command", required=True)
    rdn.add_argument("--denial", required=True)
    rdn.add_argument("--executed", required=True, choices=["no", "unknown"])

    rsd = sub.add_parser("resolve-denial")
    _add_run_mutators(rsd)
    rsd.add_argument("--id", required=True)
    rsd.add_argument("--resolution", required=True)
    rsd.add_argument("--evidence", required=True)
    rsd.add_argument("--executed", required=True, choices=["yes", "no"])

    od = sub.add_parser("open-decision")
    _add_run_mutators(od)
    od.add_argument("--question", required=True)
    od.add_argument("--context", default=None)
    od.add_argument("--options", default=None, help="JSON array")
    od.add_argument("--responsibility", choices=sorted(DECISION_RESPONSIBILITY), default=None)
    od.add_argument("--origin-workstream", default=None)
    od.add_argument("--affected", default=None, help="JSON array of workstream ids the decision gates")
    od.add_argument("--gates", default=None, help="JSON array: dispatch, completion, landing")
    od.add_argument("--defer-until", default=None, help="ISO-8601 with offset, or +Nm / +Nh")

    sd = sub.add_parser("scope-decision")
    _add_run_mutators(sd)
    sd.add_argument("--id", required=True)
    sd.add_argument("--affected", required=True, help="JSON array of workstream ids")
    sd.add_argument("--gates", required=True, help="JSON array: dispatch, completion, landing")
    sd.add_argument("--defer-until", default=None)
    sd.add_argument("--reason", required=True)

    rd = sub.add_parser("resolve-decision")
    _add_run_mutators(rd)
    rd.add_argument("--id", required=True)
    rd.add_argument("--status", choices=["resolved", "superseded"], required=True)
    rd.add_argument("--resolution", required=True)
    rd.add_argument("--evidence", required=True)
    rd.add_argument("--provenance", required=True, choices=sorted(DECISION_PROVENANCE))
    rd.add_argument("--user-words", default=None, help="the user's answer, verbatim")

    rcr = sub.add_parser("reconcile-result")
    _add_run_mutators(rcr)
    rcr.add_argument("--id", required=True)
    rcr.add_argument("--request-id", required=True, help="root request of the conflicting chain")
    rcr.add_argument("--choose", required=True, help="normalized digest of the reply that stands")
    rcr.add_argument("--evidence", required=True)

    rev = sub.add_parser("record-event")
    _add_run_mutators(rev)
    rev.add_argument("--id", required=True)
    rev.add_argument("--event", required=True, help="JSON owner event")
    rev.add_argument("--affected", default=None, help="JSON array: scope of a question or blocker decision")
    rev.add_argument("--gates", default=None, help="JSON array: gates of a question or blocker decision")
    rev.add_argument("--responsibility", choices=sorted(DECISION_RESPONSIBILITY), default=None)

    rce = sub.add_parser("reconcile-evidence")
    _add_run_mutators(rce)
    rce.add_argument("--id", required=True)
    target = rce.add_mutually_exclusive_group(required=True)
    target.add_argument("--event-id", default=None)
    target.add_argument("--request-id", default=None)
    rce.add_argument("--identity", required=True, help="JSON runtime identity the fresh read came from")
    rce.add_argument("--evidence", required=True)

    rdv = sub.add_parser("record-delivery")
    _add_run_mutators(rdv)
    rdv.add_argument("--id", required=True)
    rdv.add_argument("--receipt", required=True, help="JSON delivery receipt (normalized)")

    rpb = sub.add_parser("record-publication")
    _add_run_mutators(rpb)
    rpb.add_argument("--id", required=True)
    rpb.add_argument("--evidence", required=True, help="JSON from forge_github.py pr-evidence, or branch evidence")

    rlp = sub.add_parser("record-landing-pending")
    _add_run_mutators(rlp)
    rlp.add_argument("--id", required=True)
    rlp.add_argument("--command", dest="merge_command", required=True)
    rlp.add_argument("--outcome", required=True, help="queued, unknown, or the response that was lost")

    obs = sub.add_parser("observe")
    _add_run_mutators(obs)
    obs.add_argument("--id", required=True)
    obs.add_argument("--owner", required=True)
    obs.add_argument("--binding", required=True)
    obs.add_argument("--generation", type=int, required=True)
    obs.add_argument("--attempt", type=int, required=True)
    obs.add_argument("--observed-at", required=True)
    obs.add_argument("--lifecycle", required=True, choices=sorted(LIFECYCLE_OBSERVATIONS))
    obs.add_argument("--source", dest="obs_source", required=True, help="the binding operation that was read")
    obs.add_argument("--evidence", required=True)

    adu = sub.add_parser("ack-due")
    _add_run_mutators(adu)
    adu.add_argument("--key", required=True)
    adu.add_argument("--next-check-at", required=True)
    adu.add_argument("--note", default=None)

    rsw = sub.add_parser("record-sweep")
    _add_run_mutators(rsw)
    rsw.add_argument("--changed", required=True, choices=["true", "false"])

    pk = sub.add_parser("park")
    _add_run_mutators(pk)
    pk.add_argument("--reason", required=True)
    pk.add_argument("--resume-when", required=True)

    upk = sub.add_parser("unpark")
    _add_run_mutators(upk)
    upk.add_argument("--reason", required=True)

    du = sub.add_parser("due", help="read-only: actionable supervision items")
    du.add_argument("--run-id", required=True)
    _add_common(du)

    tc = sub.add_parser("turnend-check", help="read-only: exit 2 when active work is unwatched")
    tc.add_argument("--run-id", required=True)
    tc.add_argument("--session-id", required=True)
    _add_common(tc)

    adb = sub.add_parser("advance-base")
    _add_run_mutators(adb)
    adb.add_argument("--id", required=True)
    adb.add_argument("--old-base", required=True)
    adb.add_argument("--new-base", required=True)
    adb.add_argument("--workspace-head", required=True)
    adb.add_argument("--evidence", required=True, help="JSON array of {command, exit_code, asserts, subject, object}")
    adb.add_argument("--reason", required=True)
    adb.add_argument("--replan-decision", default=None)

    rst = sub.add_parser("restack")
    _add_run_mutators(rst)
    rst.add_argument("--id", required=True)
    rst.add_argument("--old-parent-head", required=True)
    rst.add_argument("--new-parent-head", required=True)
    rst.add_argument("--workspace-head", default=None)
    rst.add_argument("--evidence", default=None, help="JSON array of coordinator-run checks")
    rst.add_argument("--reason", default=None)

    ap = sub.add_parser("apply")
    ap.add_argument("--ops-file", required=True)
    _add_common(ap)

    sn = sub.add_parser("snapshot")
    sn.add_argument("--run-id", required=True)
    sn.add_argument("--request", default=None, help="print one request and its stored body")
    sn.add_argument("--id", dest="snapshot_ws", default=None, help="workstream of --request")
    _add_common(sn)

    cl = sub.add_parser("close")
    _add_run_mutators(cl)
    cl.add_argument("--outcome", required=True)

    ab = sub.add_parser("abandon")
    _add_run_mutators(ab)
    ab.add_argument("--reason", required=True)

    cm = sub.add_parser("claim")
    _add_run_mutators(cm)
    cm.add_argument("--force", action="store_true")
    cm.add_argument("--expected-holder", default=None)
    cm.add_argument("--reason", default=None)
    cm.add_argument("--liveness-evidence", default=None)

    rc = sub.add_parser("release-claim")
    _add_run_mutators(rc)
    rc.add_argument("--reason", required=True)

    im = sub.add_parser("import-v1", help="import a schema v1 fixture copy into this root")
    im.add_argument("--source", required=True)
    im.add_argument("--profile", required=True)
    im.add_argument("--session-id", required=True)
    _add_common(im)

    ai = sub.add_parser("amend-intent")
    _add_run_mutators(ai)
    ai.add_argument("--id", required=True)
    ai.add_argument("--user-words", required=True)
    ai.add_argument("--coordinator-instruction", default=None)
    ai.add_argument("--request-id", required=True, help="the request that carries the amendment to the owner")

    aa = sub.add_parser("amend-authority")
    _add_run_mutators(aa)
    aa.add_argument("--action", required=True, choices=AUTHORITY_ACTIONS)
    aa.add_argument("--to", required=True, choices=["granted", "withheld"])
    aa.add_argument("--user-words", required=True)

    apm = sub.add_parser("amend-permissions")
    _add_run_mutators(apm)
    apm.add_argument("--id", required=True)
    change = apm.add_mutually_exclusive_group(required=True)
    change.add_argument("--add", default=None, choices=AUTHORITY_ACTIONS)
    change.add_argument("--remove", default=None, choices=AUTHORITY_ACTIONS)
    apm.add_argument("--user-words", required=True)

    sdl = sub.add_parser("set-deliverable")
    _add_run_mutators(sdl)
    sdl.add_argument("--id", required=True)
    sdl.add_argument("--deliverable", required=True)
    sdl.add_argument("--forge", default=None)
    sdl.add_argument("--forge-repo", default=None)
    sdl.add_argument("--target-branch", default=None)
    sdl.add_argument("--no-ci", action="store_true")
    sdl.add_argument("--user-words", required=True)

    rdi = sub.add_parser("record-disposition")
    _add_run_mutators(rdi)
    rdi.add_argument("--id", required=True)
    rdi.add_argument("--disposition", required=True, choices=sorted(DISPOSITIONS))
    rdi.add_argument("--artifact", required=True)
    rdi.add_argument("--follow-up-owner", required=True)
    rdi.add_argument("--source-instruction", required=True, help="the user's words or a decision id")

    mv = sub.add_parser("migrate-v2", help="migrate a schema v2 record copy into this root")
    mv.add_argument("--source", required=True)
    mv.add_argument("--session-id", required=True)
    _add_common(mv)

    ls = sub.add_parser("list")
    _add_common(ls)

    return p


def _load_ops_payload(ops_file: str) -> tuple[str, int, list[dict]]:
    try:
        payload = json.loads(Path(ops_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoordinatorError(f"cannot read ops-file {ops_file}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CoordinatorError("ops-file must hold a JSON object")
    run_id = _require_str(payload, "run_id")
    session_id = _require_str(payload, "session_id")
    base_revision = payload.get("base_revision")
    if not isinstance(base_revision, int) or isinstance(base_revision, bool):
        raise CoordinatorError("ops-file must carry an integer base_revision")
    ops = payload.get("ops")
    if not isinstance(ops, list) or not ops or not all(isinstance(op, dict) for op in ops):
        raise CoordinatorError("ops-file must carry a non-empty list of op objects")
    return run_id, base_revision, [{**op, "session_id": session_id} for op in ops]


def _print_list_text(summaries: list[dict]) -> None:
    if not summaries:
        print("no runs")
        return
    for s in summaries:
        print(f"{s.get('run_id', '?')} [{s.get('status', '?')}] {s.get('title', '')}")


def _body_arg(args: argparse.Namespace) -> dict:
    if args.body_file is not None:
        try:
            return {"body": Path(args.body_file).read_text(encoding="utf-8")}
        except OSError as exc:
            raise CoordinatorError(f"cannot read --body-file {args.body_file}: {exc}") from exc
    return {"body": args.body} if args.body is not None else {}


def _single_op(args: argparse.Namespace) -> dict:
    """Translate a single-operation subcommand into its typed op."""
    base = {"session_id": args.session_id, "next_action": args.next_action}
    command = args.command
    if command == "add-workstream":
        return {
            **base,
            "op": command,
            "title": args.title,
            "kind": args.kind,
            "repository": args.repository,
            "scope": args.scope,
            "brief": args.brief,
            "acceptance_criteria": _json_arg(args.acceptance_criteria, "--acceptance-criteria"),
            "dependencies": _json_arg(args.dependencies, "--dependencies") or [],
            "stack_parent": args.stack_parent,
            "delivery_authority": args.delivery_authority,
            "permitted_operations": _json_arg(args.permitted_operations, "--permitted-operations"),
            "deliverable": args.deliverable,
            "forge": args.forge,
            "forge_repo": args.forge_repo,
            "target_branch": args.target_branch,
            "no_ci": args.no_ci,
            "user_words": args.user_words,
            "user_references": _json_arg(args.user_references, "--user-references"),
            "supersedes": args.supersedes,
            "from_scout": args.from_scout,
        }
    if command == "assign":
        op = {
            **base,
            "op": command,
            "id": args.id,
            "binding": args.binding,
            "owner_type": args.owner_type,
            "owner_id": args.owner_id,
        }
        # Only touch worktree, branch and base when the flag was passed, so a reassignment
        # that does not restate them never erases an existing workspace record.
        for key in (
            "worktree",
            "branch",
            "base_sha",
            "handover_reason",
            "quiescence_evidence",
            "runtime_instruction",
            "runtime_model",
            "runtime_effort",
        ):
            if getattr(args, key) is not None:
                op[key] = getattr(args, key)
        if args.owner_identity is not None:
            op["owner_identity"] = _json_arg(args.owner_identity, "--owner-identity")
        return op
    if command == "record-workspace":
        return {**base, "op": command, "id": args.id, "receipt": _json_arg(args.receipt, "--receipt")}
    if command == "issue-request":
        return {
            **base,
            "op": command,
            "id": args.id,
            "request_id": args.request_id,
            "purpose": args.purpose,
            "brief_digest": args.brief_digest,
            "target_base_sha": args.target_base_sha,
            "root_request_id": args.root_request_id,
            "answers": args.answers,
            "ack_window_minutes": args.ack_window_minutes,
            "result_window_minutes": args.result_window_minutes,
            **_body_arg(args),
        }
    if command == "record-result":
        return {**base, "op": command, "id": args.id, "result": _json_arg(args.result, "--result")}
    if command == "update-workstream":
        op = {**base, "op": command, "id": args.id}
        if args.status is not None:
            op["status"] = args.status
        if args.blocker is not None:
            op["blocker"] = args.blocker
        if args.clear_blocker:
            op["clear_blocker"] = True
        if args.deliverable:
            op["deliverables"] = [_json_arg(d, "--deliverable") for d in args.deliverable]
        if args.verification:
            op["verifications"] = [_json_arg(v, "--verification") for v in args.verification]
        if args.cleanup_disposition is not None:
            op["cleanup_disposition"] = args.cleanup_disposition
            op["cleanup_evidence"] = _json_arg(args.cleanup_evidence, "--cleanup-evidence")
        if args.correct_field is not None or args.correct_note is not None:
            op["correct"] = {"field": args.correct_field, "note": args.correct_note}
        for key, value in (
            ("wait_on", args.wait_on),
            ("clears_when", args.clears_when),
            ("source", args.wait_source),
            ("next_check_at", args.next_check_at),
            ("until", args.until),
            ("from_event", args.from_event),
        ):
            if value is not None:
                op[key] = value
        return op
    if command == "record-landing":
        return {**base, "op": command, "id": args.id, "landing": _json_arg(args.landing, "--landing")}
    if command == "record-denial":
        return {
            **base,
            "op": command,
            "action": args.action,
            "workstream": args.workstream,
            "executor": args.executor,
            "command": args.denied_command,
            "denial": args.denial,
            "executed": args.executed,
        }
    if command == "resolve-denial":
        return {
            **base,
            "op": command,
            "id": args.id,
            "resolution": args.resolution,
            "evidence": args.evidence,
            "executed": args.executed,
        }
    if command == "open-decision":
        return {
            **base,
            "op": command,
            "question": args.question,
            "context": args.context,
            "options": _json_arg(args.options, "--options"),
            "responsibility": args.responsibility,
            "origin_workstream": args.origin_workstream,
            "affected_workstreams": _json_arg(args.affected, "--affected"),
            "gates": _json_arg(args.gates, "--gates"),
            "defer_until": args.defer_until,
        }
    if command == "scope-decision":
        op = {
            **base,
            "op": command,
            "id": args.id,
            "affected_workstreams": _json_arg(args.affected, "--affected"),
            "gates": _json_arg(args.gates, "--gates"),
            "reason": args.reason,
        }
        if args.defer_until is not None:
            op["defer_until"] = args.defer_until
        return op
    if command == "resolve-decision":
        return {
            **base,
            "op": command,
            "id": args.id,
            "status": args.status,
            "resolution": args.resolution,
            "evidence": args.evidence,
            "provenance": args.provenance,
            "user_words": args.user_words,
        }
    if command == "reconcile-result":
        return {
            **base,
            "op": command,
            "id": args.id,
            "request_id": args.request_id,
            "choose": args.choose,
            "evidence": args.evidence,
        }
    if command == "record-event":
        op = {**base, "op": command, "id": args.id, "event": _json_arg(args.event, "--event")}
        if args.affected is not None:
            op["affected_workstreams"] = _json_arg(args.affected, "--affected")
        if args.gates is not None:
            op["gates"] = _json_arg(args.gates, "--gates")
        if args.responsibility is not None:
            op["responsibility"] = args.responsibility
        return op
    if command == "reconcile-evidence":
        return {
            **base,
            "op": command,
            "id": args.id,
            "event_id": args.event_id,
            "request_id": args.request_id,
            "identity": _json_arg(args.identity, "--identity"),
            "evidence": args.evidence,
        }
    if command == "record-delivery":
        return {**base, "op": command, "id": args.id, "receipt": _json_arg(args.receipt, "--receipt")}
    if command == "record-publication":
        return {**base, "op": command, "id": args.id, "evidence": _json_arg(args.evidence, "--evidence")}
    if command == "record-landing-pending":
        return {**base, "op": command, "id": args.id, "command": args.merge_command, "outcome": args.outcome}
    if command == "observe":
        return {
            **base,
            "op": command,
            "id": args.id,
            "owner": args.owner,
            "binding": args.binding,
            "generation": args.generation,
            "attempt": args.attempt,
            "observed_at": args.observed_at,
            "lifecycle": args.lifecycle,
            "source": args.obs_source,
            "evidence": args.evidence,
        }
    if command == "ack-due":
        return {**base, "op": command, "key": args.key, "next_check_at": args.next_check_at, "note": args.note}
    if command == "record-sweep":
        return {**base, "op": command, "changed": args.changed == "true"}
    if command == "park":
        return {**base, "op": command, "reason": args.reason, "resume_when": args.resume_when}
    if command == "unpark":
        return {**base, "op": command, "reason": args.reason}
    if command == "advance-base":
        return {
            **base,
            "op": command,
            "id": args.id,
            "old_base": args.old_base,
            "new_base": args.new_base,
            "workspace_head": args.workspace_head,
            "evidence": _json_arg(args.evidence, "--evidence"),
            "reason": args.reason,
            "replan_decision": args.replan_decision,
        }
    if command == "restack":
        op = {
            **base,
            "op": command,
            "id": args.id,
            "old_parent_head": args.old_parent_head,
            "new_parent_head": args.new_parent_head,
            "reason": args.reason,
        }
        if args.workspace_head is not None:
            op["workspace_head"] = args.workspace_head
        if args.evidence is not None:
            op["evidence"] = _json_arg(args.evidence, "--evidence")
        return op
    if command == "amend-intent":
        return {
            **base,
            "op": command,
            "id": args.id,
            "user_words": args.user_words,
            "coordinator_instruction": args.coordinator_instruction,
            "request_id": args.request_id,
        }
    if command == "amend-authority":
        return {**base, "op": command, "action": args.action, "to": args.to, "user_words": args.user_words}
    if command == "amend-permissions":
        return {
            **base,
            "op": command,
            "id": args.id,
            "action": args.add or args.remove,
            "change": "add" if args.add else "remove",
            "user_words": args.user_words,
        }
    if command == "set-deliverable":
        return {
            **base,
            "op": command,
            "id": args.id,
            "deliverable": args.deliverable,
            "forge": args.forge,
            "forge_repo": args.forge_repo,
            "target_branch": args.target_branch,
            "no_ci": args.no_ci,
            "user_words": args.user_words,
        }
    if command == "record-disposition":
        return {
            **base,
            "op": command,
            "id": args.id,
            "disposition": args.disposition,
            "artifact": args.artifact,
            "follow_up_owner": args.follow_up_owner,
            "source_instruction": args.source_instruction,
        }
    if command == "close":
        return {**base, "op": command, "outcome": args.outcome}
    if command == "claim":
        return {
            **base,
            "op": command,
            "force": args.force,
            "expected_holder": args.expected_holder,
            "reason": args.reason,
            "liveness_evidence": args.liveness_evidence,
        }
    if command == "release-claim":
        return {**base, "op": command, "reason": args.reason}
    raise AssertionError(command)  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        # Resolve before anything else: with no root, or a protected one, nothing is created.
        root = resolve_state_root(args.state_root)
        if args.command == "start":
            record = create_run(
                root,
                title=args.title,
                intent=args.intent,
                coordination_reason=args.coordination_reason,
                delivery_authority=args.delivery_authority,
                authority=_json_arg(args.authority, "--authority"),
                profile_path=args.profile,
                session_id=args.session_id,
                next_action=args.next_action,
                coordinator_runtime=args.coordinator_runtime,
            )
            _emit(root, record, args.format)
        elif args.command == "migrate-v2":
            _emit(root, migrate_v2(root, source=args.source, session_id=args.session_id), args.format)
        elif args.command == "import-v1":
            record = import_v1(root, source=args.source, profile_path=args.profile, session_id=args.session_id)
            _emit(root, record, args.format)
        elif args.command == "apply":
            run_id, base_revision, ops = _load_ops_payload(args.ops_file)
            _emit(root, _transact(root, run_id, ops, base_revision, batch=True), args.format)
        elif args.command == "due":
            record = _load_snapshot(root, args.run_id)
            report = {
                "run_id": record["run_id"],
                "now": _now_iso(),
                "parked": record["supervision"]["parked"],
                "items": due_items(root, record),
            }
            print(json.dumps(report, indent=2, sort_keys=True))
        elif args.command == "turnend-check":
            status = turnend_status(root, _load_snapshot(root, args.run_id), args.session_id)
            print(json.dumps(status, indent=2, sort_keys=True))
            return 0 if status["ok"] else 2
        elif args.command == "snapshot" and args.request:
            print(
                json.dumps(
                    request_with_body(root, _load_snapshot(root, args.run_id), args.snapshot_ws, args.request),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "snapshot":
            _emit(root, _load_snapshot(root, args.run_id), args.format)
        elif args.command == "abandon":
            _emit(root, _abandon(root, args.run_id, args.reason, args.session_id, args.base_revision), args.format)
        elif args.command == "list":
            summaries = list_runs(root)
            if args.format == "json":
                print(json.dumps(summaries, indent=2, sort_keys=True))
            else:
                _print_list_text(summaries)
        else:
            record = _transact(root, args.run_id, [_single_op(args)], args.base_revision, batch=False)
            _emit(root, record, args.format)
    except (StateRootError, ProfileError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except CoordinatorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
