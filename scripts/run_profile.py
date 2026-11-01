#!/usr/bin/env python3
"""Load and validate a run profile: the resolved policy a coordinator dispatches under.

A profile turns the policy choices that used to live as scattered prose (who merges,
whether lanes may be reused, who spawns successors, who writes state, what review is
required, how sessions and workspaces are cleaned up) into one explicit document that is
resolved before the first dispatch and frozen into the run record with its digest.

Some values are invariants of this package, and a profile that contradicts one is
refused: the coordinator is the only state
writer, concurrent writers never share a workspace, a lane is reused only at a verified
boundary, and nothing is cleaned up on idleness alone.
"""

import hashlib
import json
import tomllib
from pathlib import Path

PROFILE_VERSION = 1

# key -> allowed values. A tuple of allowed strings means an enum; `int` means a positive
# integer; `list` means a list of non-empty strings; `bool` means a JSON boolean.
_SCHEMA: dict[str, dict[str, object]] = {
    "profile": {"name": str, "version": int},
    "runtime": {
        "default": ("match-coordinator",),
        "explicit_choice_wins": (True,),
        "cross_runtime_requires_instruction": (True,),
    },
    "topology": {
        "mode": ("workstreams", "lanes"),
        "state_writer": ("coordinator",),
        "one_writable_assignment_per_workspace": (True,),
        "lane_reuse": ("never", "verified-boundary"),
        "successor_spawning": ("coordinator", "owner-with-grant"),
    },
    "delivery": {
        "push": ("owner", "coordinator"),
        "pr_create": ("owner", "coordinator"),
        "merge": ("coordinator", "owner-with-grant"),
        "release": ("coordinator", "owner-with-grant"),
        "merge_order": ("explicit-queue",),
    },
    "review": {
        "required": list,
        "allow_unavailable": list,
        "max_reviewer_sessions_per_owner": int,
        "findings_policy": ("fix-in-workstream",),
        "regressions_only_from_round": int,
    },
    "context": {
        "retain_owner_through_fixes": (True,),
        "rebrief_after_compaction": (True,),
        "compact_at_boundary": bool,
    },
    "cleanup": {
        "reviewer_sessions": ("retain", "close-after-accepted-evidence"),
        "owner_sessions": ("retain-until-integrated",),
        "workspaces": ("retain-until-verified-and-authorized",),
        "partial_launch": ("retain-for-inspection",),
    },
    "limits": {
        "max_active_workstreams": int,
        "max_dispatch_attempts": int,
        "cleanup_evidence_max_age_hours": int,
    },
    "repository": {
        "install_command": ("inspect-repository",),
        "local_gates": ("repository-defined",),
    },
}

# Sections and keys added after the first profiles shipped. A profile that omits them gets
# these values, so an older profile stays loadable and resolves to an explicit policy; the
# resolved profile, defaults included, is what a run freezes and digests.
_OPTIONAL: dict[str, dict[str, tuple[object, object]]] = {
    "supervision": {
        "ack_window_minutes": (int, 10),
        "result_window_minutes": (int, 90),
        "max_wait_check_minutes": (int, 240),
        "decision_overdue_minutes": (int, 30),
        "heartbeat_initial_minutes": (int, 10),
        "heartbeat_max_minutes": (int, 120),
    },
    "delivery": {
        "evidence_max_age_minutes": (int, 30),
        "forbid_agent_coauthors": (bool, True),
    },
}

_REQUIRED_SECTIONS = set(_SCHEMA)


class ProfileError(Exception):
    pass


def _check_value(section: str, key: str, value: object, rule: object) -> None:
    where = f"[{section}].{key}"
    if rule is str:
        if not isinstance(value, str) or not value.strip():
            raise ProfileError(f"{where} must be a non-empty string")
    elif rule is int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ProfileError(f"{where} must be a positive integer")
    elif rule is bool:
        if not isinstance(value, bool):
            raise ProfileError(f"{where} must be true or false")
    elif rule is list:
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise ProfileError(f"{where} must be a list of non-empty strings")
    elif isinstance(rule, tuple):
        if value not in rule or type(value) is not type(rule[0]):
            raise ProfileError(f"{where} must be one of {list(rule)!r}, got {value!r}")
    else:  # pragma: no cover - schema construction error
        raise AssertionError(rule)


def _apply_defaults(data: dict) -> dict:
    resolved = {section: dict(table) if isinstance(table, dict) else table for section, table in data.items()}
    for section, keys in _OPTIONAL.items():
        table = resolved.setdefault(section, {})
        if not isinstance(table, dict):
            raise ProfileError(f"[{section}] must be a table")
        for key, (rule, default) in keys.items():
            if key in table:
                _check_value(section, key, table[key], rule)
            else:
                table[key] = default
    return resolved


def _known_keys(section: str) -> set[str]:
    return set(_SCHEMA.get(section, {})) | set(_OPTIONAL.get(section, {}))


def validate_profile(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ProfileError("profile must be a table")
    missing = _REQUIRED_SECTIONS - set(data)
    if missing:
        raise ProfileError(f"profile is missing section(s): {sorted(missing)}")
    data = _apply_defaults(data)
    unknown_sections = set(data) - _REQUIRED_SECTIONS - set(_OPTIONAL)
    if unknown_sections:
        raise ProfileError(f"profile has unknown section(s): {sorted(unknown_sections)}")
    for section in set(_OPTIONAL) - set(_SCHEMA):
        unknown_keys = set(data[section]) - _known_keys(section)
        if unknown_keys:
            raise ProfileError(f"[{section}] has unknown key(s): {sorted(unknown_keys)}")
    for section, keys in _SCHEMA.items():
        table = data[section]
        if not isinstance(table, dict):
            raise ProfileError(f"[{section}] must be a table")
        missing_keys = set(keys) - set(table)
        if missing_keys:
            raise ProfileError(f"[{section}] is missing key(s): {sorted(missing_keys)}")
        unknown_keys = set(table) - _known_keys(section)
        if unknown_keys:
            raise ProfileError(f"[{section}] has unknown key(s): {sorted(unknown_keys)}")
        for key, rule in keys.items():
            _check_value(section, key, table[key], rule)
    if data["profile"]["version"] != PROFILE_VERSION:
        raise ProfileError(
            f"unsupported profile version {data['profile']['version']!r}; this package reads version {PROFILE_VERSION}"
        )
    if data["topology"]["mode"] == "workstreams" and data["topology"]["lane_reuse"] != "never":
        raise ProfileError(
            "[topology].lane_reuse must be 'never' when mode is 'workstreams'; lane reuse belongs to the lanes topology"
        )
    overlap = set(data["review"]["required"]) & set(data["review"]["allow_unavailable"])
    if overlap:
        raise ProfileError(f"reviewer(s) {sorted(overlap)} cannot be both required and allowed to be unavailable")
    return data


def canonical_json(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def load_profile(path: str | Path) -> tuple[dict, str]:
    """Return the validated profile and the SHA-256 of its canonical JSON form."""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProfileError(f"no profile at {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"profile {path} is not valid TOML: {exc}") from exc
    data = validate_profile(data)
    digest = hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()
    return data, digest
