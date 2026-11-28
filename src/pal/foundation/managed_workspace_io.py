"""Short cross-process gates for canonical background-task data.

Workers use their own worktrees and never acquire this gate. The foreground
holds a shared gate only while performing local I/O; Git publication holds
the exclusive gate.
"""
from __future__ import annotations

import asyncio
import fcntl
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import AsyncIterator, Iterator


def _gate_path(root: Path) -> Path:
    return root / "data" / ".managed-workspace.lock"


@contextmanager
def managed_workspace_write_gate(root: Path) -> Iterator[None]:
    path = _gate_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextmanager
def managed_workspace_sync_read_gate(root: Path) -> Iterator[None]:
    path = _gate_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        yield


@asynccontextmanager
async def managed_workspace_read_gate(root: Path) -> AsyncIterator[None]:
    path = _gate_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.025)
        yield
