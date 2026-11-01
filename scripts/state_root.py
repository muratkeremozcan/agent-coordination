#!/usr/bin/env python3
"""Explicit, fail-closed state root resolution for the coordinator ledger and adapters.

The ledger this package grew out of defaulted to a personal assistant's private state
home. A standalone copy that kept that default would write coordinator runs into
someone's live memory the first time it ran without configuration. So there is no
default here: the caller names a state root with `--state-root` or
`COORDINATE_STATE_ROOT`, and with neither set every state-mutating command exits
nonzero before it creates a single file or directory.

A configured root is also refused when it canonicalizes into a protected location:
either personal-agent checkout, the personal-agent state home (including one named by
`PERSONAL_AGENT_HOME`), or a host skill directory. The check resolves symlinks and `..`
on the longest existing ancestor and appends the nonexistent remainder, so an alias or
a not-yet-created descendant cannot slip past it. Nothing here imports or calls the
personal-agent resolver; its default layout is restated from reading its source.
"""

import os
import pwd
from pathlib import Path

STATE_ROOT_ENV = "COORDINATE_STATE_ROOT"

# Restated from personal-agent's `_sanctum.py` (read as source only): the home
# is $PERSONAL_AGENT_HOME or ~/personal-agent, the private state lives under
# `_bmad/memory/personal-agent`, and the ledger it used wrote under
# `.runtime/coordinator/runs` below that.
_PERSONAL_AGENT_HOME_ENV = "PERSONAL_AGENT_HOME"
_PROTECTED_HOME_RELATIVE = (
    "personal-agent",
    "opensource/personal-agent",
    ".agents",
    ".claude",
    ".codex",
)


class StateRootError(Exception):
    """The state root is missing, relative, or resolves into a protected location."""


def canonicalize(path: Path) -> Path:
    """Resolve symlinks and `..` in filesystem order, keeping a nonexistent suffix literally.

    `..` must apply to the directory a symlink actually points at, so `alias/../live` with
    `alias -> other/nested` means `other/live`. Collapsing `..` lexically first (as
    `os.path.abspath` does) would protect the wrong directory. `os.path.realpath` walks the
    components left to right, resolving each symlink before the next `..`, and keeps the
    components that do not exist yet.
    """
    return Path(os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(os.fspath(path)))))


def _homes() -> list[Path]:
    """Both the HOME-derived home and the account's password-database home.

    Tests point HOME at a temporary directory; consulting the account home as well means a
    real protected path is still refused when HOME has been redirected.
    """
    homes = [Path.home()]
    try:
        homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except KeyError:
        pass
    return homes


def protected_roots() -> list[Path]:
    roots: list[Path] = []
    for home in _homes():
        for relative in _PROTECTED_HOME_RELATIVE:
            roots.append(canonicalize(home / relative))
    override = os.environ.get(_PERSONAL_AGENT_HOME_ENV)
    if override and Path(override).expanduser().is_absolute():
        roots.append(canonicalize(Path(override)))
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _folded(path: Path) -> Path:
    # Default macOS volumes are case-insensitive, so `PERSONAL-AGENT` names the same directory
    # as `personal-agent`. Folding case can only over-refuse on a case-sensitive volume.
    return Path(os.fspath(path).casefold())


def _identities(path: Path) -> set[tuple[int, int]]:
    """Device and inode of the path and each existing ancestor; spelling-independent."""
    found = set()
    for candidate in (path, *path.parents):
        try:
            st = os.stat(candidate)
        except OSError:
            continue
        found.add((st.st_dev, st.st_ino))
    return found


def _overlaps(candidate: Path, root: Path) -> bool:
    if _is_within(candidate, root) or _is_within(root, candidate):
        return True
    if _is_within(_folded(candidate), _folded(root)) or _is_within(_folded(root), _folded(candidate)):
        return True
    try:
        root_id = os.stat(root)
    except OSError:
        root_id = None
    if root_id is not None and (root_id.st_dev, root_id.st_ino) in _identities(candidate):
        return True
    try:
        candidate_id = os.stat(candidate)
    except OSError:
        return False
    return (candidate_id.st_dev, candidate_id.st_ino) in _identities(root)


def check_not_protected(path: Path, *, purpose: str) -> Path:
    """Return the canonical path, or raise when it sits inside a protected root."""
    canonical = canonicalize(path)
    for root in protected_roots():
        if _overlaps(canonical, root):
            raise StateRootError(
                f"{purpose} {path} resolves to {canonical}, which overlaps the protected "
                f"location {root}; choose a root outside personal-agent checkouts, its state "
                "home and host skill directories"
            )
    return canonical


def within_root(root: Path, path: Path, *, purpose: str) -> Path:
    """Canonicalize a path under the state root and refuse any escape through a symlink.

    Checking only the configured root is not enough: a `runs` or `workspaces` entry inside it
    could be a symlink into a protected location, and every file opened below it would follow.
    """
    canonical = canonicalize(path)
    if not _is_within(canonical, root):
        raise StateRootError(f"{purpose} {path} resolves to {canonical}, outside the state root {root}")
    return check_not_protected(canonical, purpose=purpose)


def resolve_state_root(explicit: str | None) -> Path:
    """Resolve the configured state root without creating anything."""
    raw = explicit if explicit else os.environ.get(STATE_ROOT_ENV)
    if not raw:
        raise StateRootError(
            f"no state root configured; pass --state-root or set {STATE_ROOT_ENV}. There is deliberately no default."
        )
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise StateRootError(f"state root {raw!r} must be an absolute path")
    return check_not_protected(candidate, purpose="state root")
