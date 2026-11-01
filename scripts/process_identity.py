#!/usr/bin/env python3
"""Owned-process identity with a pinned time zone and locale.

`ps -o lstart=` renders a process's start time in the caller's time zone and locale, so the
same process reads differently from a shell with another TZ or LANG, and a live process could
be mistaken for a recycled pid. Capture and comparison both pin them, and a recorded identity
names the format it was captured in. A probe that cannot read the process table raises, so an
unreadable table is never mistaken for a process that has exited.
"""

import os
import subprocess

PROCESS_IDENTITY_FORMAT = "lstart-utc-c"
_PS_ENV = {"TZ": "UTC0", "LC_ALL": "C", "LANG": "C"}


class ProcessProbeError(Exception):
    """The process table could not be read; absence is unproven."""


def process_started(pid: int) -> str | None:
    """The pinned start identity of a live pid, or None when ps proves it absent."""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **_PS_ENV},
        )
    except OSError as exc:
        raise ProcessProbeError(f"ps could not run: {exc}") from exc
    started = result.stdout.strip()
    if started:
        return started
    if result.returncode == 1 and not result.stderr.strip():
        return None
    raise ProcessProbeError(f"ps exited {result.returncode}: {result.stderr.strip()}")


def process_identity(pid: int) -> dict:
    """Capture a live process's identity in the pinned format."""
    started = process_started(pid)
    if started is None:
        raise ProcessProbeError(f"no process {pid}")
    return {"pid": pid, "started": started, "identity_format": PROCESS_IDENTITY_FORMAT}


def identity_is_live(identity: dict) -> bool | None:
    """True when the recorded process is still running, False when proven gone, None when unknown."""
    if not isinstance(identity, dict) or identity.get("identity_format") != PROCESS_IDENTITY_FORMAT:
        return None
    try:
        started = process_started(int(identity["pid"]))
    except (ProcessProbeError, KeyError, TypeError, ValueError):
        return None
    return started is not None and started == identity.get("started")
