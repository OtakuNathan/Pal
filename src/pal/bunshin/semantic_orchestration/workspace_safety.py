from __future__ import annotations
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.workspace_resources import format_workspace_process_holders, workspace_process_holders


def _safe_component(value: str) -> str:
    return "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in value)


def _git_output(worktree: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr or completed.stdout or "Git command failed"
        )
    return completed.stdout.strip()


def _raise_if_workspace_held(
    workspace: Path,
    message: str,
    *,
    manager_snapshot_lock: Path | None = None,
) -> None:
    holders = workspace_process_holders(workspace)
    if manager_snapshot_lock is not None:
        try:
            lock_path = manager_snapshot_lock.resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            lock_path = ""
        if lock_path:
            holders = tuple(
                holder
                for holder in holders
                if not (
                    holder.pid == os.getpid()
                    and not holder.holds_cwd
                    and bool(holder.read_paths or holder.write_paths or holder.unknown_paths)
                    and all(
                        path == lock_path
                        for path in (
                            *holder.read_paths,
                            *holder.write_paths,
                            *holder.unknown_paths,
                        )
                    )
                )
            )
    if holders:
        raise RuntimeError(f"{message}; {format_workspace_process_holders(holders)}")


def _lease_is_live(lease: Mapping[str, Any]) -> bool:
    raw = str(lease.get("expires_at") or "")
    if not raw:
        return False
    try:
        expires_at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > datetime.now(timezone.utc)
