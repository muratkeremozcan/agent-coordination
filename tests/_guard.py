"""Test guard: no test may touch real user state, installed skills or live runtimes.

Imported first by every test module. It redirects HOME to a throwaway directory, strips
variables that could point code at real state, and installs an audit hook that raises on
any file open, directory creation, rename, removal or subprocess launch that targets a
protected real path or a real `herdr`, `treehouse` or `gh` binary. The canary tests in
test_guard.py prove the hook fires, so a regression that reaches for real state fails
loudly instead of quietly writing there.
"""

import atexit
import os
import pwd
import shutil
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parent.parent
for sub in ("scripts", "adapters"):
    path = str(REPO_ROOT / sub)
    if path not in sys.path:
        sys.path.insert(0, path)


class GuardViolation(RuntimeError):
    pass


_REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
PROTECTED_REAL_PATHS = tuple(
    str((_REAL_HOME / relative).resolve())
    for relative in (
        "personal-agent",
        "opensource/personal-agent",
        ".agents",
        ".claude",
        ".codex",
        ".config/treehouse",
        ".treehouse",
    )
)
FORBIDDEN_BINARIES = ("herdr", "treehouse", "gh")


def _under_protected(raw: object) -> bool:
    if isinstance(raw, int) or raw is None:
        return False
    try:
        text = os.fsdecode(raw)
    except TypeError:
        return False
    candidates = {os.path.abspath(text)}
    try:
        candidates.add(os.path.realpath(text))
    except (OSError, ValueError):
        pass
    for candidate in candidates:
        for root in PROTECTED_REAL_PATHS:
            folded, folded_root = candidate.casefold(), root.casefold()
            if folded == folded_root or folded.startswith(folded_root + os.sep):
                return True
    return False


def _audit(event: str, args: tuple) -> None:
    if event in {"open", "os.mkdir", "os.remove", "os.rmdir", "os.symlink", "os.link", "shutil.rmtree"}:
        if args and _under_protected(args[0]):
            raise GuardViolation(f"{event} on protected real path {args[0]!r}")
    elif event in {"os.rename", "os.replace"}:
        if any(_under_protected(a) for a in args[:2]):
            raise GuardViolation(f"{event} touching protected real path {args[:2]!r}")
    elif event == "subprocess.Popen":
        executable, argv, cwd = args[0], args[1], args[2]
        names = [executable] + (list(argv) if isinstance(argv, (list, tuple)) else [argv])
        first = names[1] if len(names) > 1 and names[1] is not None else names[0]
        base = os.path.basename(os.fsdecode(first)) if first is not None else ""
        if base in FORBIDDEN_BINARIES:
            raise GuardViolation(f"test tried to launch a real {base} binary")
        if _under_protected(cwd) or any(_under_protected(n) for n in names if isinstance(n, (str, bytes, os.PathLike))):
            raise GuardViolation(f"subprocess touching protected real path: {names!r}")


_FAKE_HOME = Path(tempfile.mkdtemp(prefix="coordinate-test-home-"))
# Removed when the test process exits, so repeated runs leave nothing behind.
atexit.register(shutil.rmtree, _FAKE_HOME, ignore_errors=True)
os.environ["HOME"] = str(_FAKE_HOME)
for variable in (
    "PERSONAL_AGENT_HOME",
    "COORDINATE_STATE_ROOT",
    "COORDINATE_TREEHOUSE_BIN",
    "COORDINATE_GH_BIN",
    "TREEHOUSE_LEASE_HOLDER",
    "HERDR_ENV",
    "GIT_DIR",
    "GIT_WORK_TREE",
):
    os.environ.pop(variable, None)
# Disposable Git repositories must not inherit the user's global hooks, templates or signing.
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ["GIT_AUTHOR_NAME"] = os.environ["GIT_COMMITTER_NAME"] = "Fixture"
os.environ["GIT_AUTHOR_EMAIL"] = os.environ["GIT_COMMITTER_EMAIL"] = "fixture@example.invalid"

if not getattr(sys, "_coordinate_guard_installed", False):
    sys.addaudithook(_audit)
    sys._coordinate_guard_installed = True

FAKE_HOME = _FAKE_HOME
REAL_HOME = str(_REAL_HOME)
