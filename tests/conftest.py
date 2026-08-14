"""Test-session overrides shared by the Pal test suite.

These fixtures exist to harden the *test environment only*; they must never
alter Pal's normal runtime paths. If a fix belongs in production code, put it
there instead of relying on a test-side shim.

Current override:
  - bunshin manager orphan guard: when the test process is killed hard (OOM
    killer, timeout, pkill), the bunshin manager subprocess it spawned would
    otherwise survive forever, leaking ~80MB each and eventually exhausting
    this small machine. We attach PDEATHSIG (Linux prctl) to manager spawns
    made through subprocess.Popen so the kernel terminates the manager the
    moment its parent dies, whatever killed the parent. CGroup scoping covers
    the pal.service tree; this covers manager spawns from arbitrary test
    processes that live outside pal.service.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys

import pytest

_PR_SET_PDEATHSIG = 1
# Load libc once before any fork so preexec_fn never triggers a loader lock.
_LIBC = ctypes.CDLL("libc.so.6", use_errno=True)


def _enable_parent_death_signal() -> None:
    """Run inside the forked child before exec: die when the parent dies."""
    _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    # prctl is racy: if the parent died between fork() and prctl(), no signal
    # will ever arrive. Detect that window and exit immediately.
    if os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGTERM)


@pytest.fixture(autouse=True)
def _bunshin_manager_orphan_guard(monkeypatch):
    if not sys.platform.startswith("linux"):
        return
    original_popen = subprocess.Popen

    def guarded_popen(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args", [])
        if isinstance(argv, (list, tuple)) and any(
            "pal.bunshin.manager_main" in str(item) for item in argv
        ):
            kwargs["preexec_fn"] = _enable_parent_death_signal
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
