#!/usr/bin/env python3
"""Fake TreeHouse v2.0.0 CLI for adapter tests. Never touches a real pool.

Behavior is driven by environment variables set per test:
  FAKE_TREEHOUSE_DIR   directory holding the fake pool and the call log (required)
  FAKE_TREEHOUSE_MODE  ok | hook-fail | dirty | foreign | subdir | garbage | exit1 | timeout | return-fail
  FAKE_TREEHOUSE_FOREIGN  path to a different repository, for mode=foreign

`get --lease` creates a detached worktree of the current directory's repository at the
root checkout's HEAD (the fallback base TreeHouse v2.0.0 hooks derive), prints only its
path on stdout and writes banners to stderr, matching the documented contract.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

state = Path(os.environ["FAKE_TREEHOUSE_DIR"])
mode = os.environ.get("FAKE_TREEHOUSE_MODE", "ok")
state.mkdir(parents=True, exist_ok=True)
with open(state / "calls.jsonl", "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


command = sys.argv[1] if len(sys.argv) > 1 else ""
if command == "get":
    print("treehouse: acquiring worktree", file=sys.stderr)
    if mode == "exit1":
        print("treehouse: pool exhausted", file=sys.stderr)
        sys.exit(1)
    if mode == "timeout":
        time.sleep(30)
    if mode == "subdir":
        sub = Path(os.getcwd()) / "src"
        sub.mkdir(exist_ok=True)
        print(str(sub.resolve()))
        sys.exit(0)
    if mode == "garbage":
        print("Acquired worktree!\nsee status")
        sys.exit(0)
    repo = os.environ["FAKE_TREEHOUSE_FOREIGN"] if mode == "foreign" else os.getcwd()
    pool = state / "pool"
    pool.mkdir(exist_ok=True)
    index = len(list(pool.iterdir())) + 1
    path = pool / f"wt-{index}"
    git("worktree", "add", "--detach", str(path), "HEAD", cwd=repo)
    if mode == "dirty":
        (path / "leftover.txt").write_text("from a previous lease\n")
    if mode == "hook-fail":
        print("treehouse: post_create hook failed: npm ci exited 1 (continuing)", file=sys.stderr)
    else:
        # The fixture repository ignores this marker, standing in for installed dependencies.
        (path / ".deps-ready").write_text("ok\n")
    print(str(path.resolve()))
    sys.exit(0)
if command == "return":
    if mode == "return-fail":
        print("treehouse: return failed", file=sys.stderr)
        sys.exit(1)
    target = Path(sys.argv[2])
    git("reset", "--hard", cwd=target)
    git("clean", "-fdx", cwd=target)
    sys.exit(0)
print(f"fake treehouse: unsupported command {sys.argv[1:]}", file=sys.stderr)
sys.exit(2)
