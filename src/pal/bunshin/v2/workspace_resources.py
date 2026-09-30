from __future__ import annotations
import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, IO, Iterator
from pal.bunshin.v2.workspace_git import _git


@dataclass(frozen=True)
class WorkspaceProcessHolder:
    pid: int
    process_group: int
    command: str
    holds_cwd: bool = False
    read_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    unknown_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "process_group": self.process_group,
            "command": self.command,
            "holds_cwd": self.holds_cwd,
            "read_paths": list(self.read_paths),
            "write_paths": list(self.write_paths),
            "unknown_paths": list(self.unknown_paths),
        }


@dataclass
class WorkspaceLockRegistry:
    _streams_by_workspace: dict[str, IO[str]] = field(default_factory=dict, init=False)
    _workspace_by_owner: dict[str, str] = field(default_factory=dict, init=False)

    def acquire(self, owner_id: str, workspace: Path) -> Path:
        import fcntl

        canonical_workspace = os.path.realpath(workspace)
        if owner_id in self._workspace_by_owner:
            raise RuntimeError(f"workspace lock owner already holds a lock: {owner_id}")
        if canonical_workspace in self._streams_by_workspace:
            raise BlockingIOError(
                f"workspace lock is already held: {canonical_workspace}"
            )
        git_probe = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if git_probe.returncode == 0:
            git_dir = (workspace / git_probe.stdout.strip()).resolve()
            lock_path = git_dir / "pal-bunshin-v2.snapshot.lock"
        else:
            workspace_key = hashlib.sha256(
                canonical_workspace.encode("utf-8")
            ).hexdigest()[:24]
            lock_path = workspace.parent / ".pal-candidate-locks" / f"{workspace_key}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stream = lock_path.open("a+")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            stream.close()
            raise
        self._streams_by_workspace[canonical_workspace] = stream
        self._workspace_by_owner[owner_id] = canonical_workspace
        return lock_path

    def release(self, owner_id: str) -> None:
        import fcntl

        canonical_workspace = self._workspace_by_owner.pop(owner_id, None)
        stream = (
            self._streams_by_workspace.pop(canonical_workspace, None)
            if canonical_workspace is not None
            else None
        )
        if stream is None:
            return
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def is_held(self, owner_id: str) -> bool:
        return owner_id in self._workspace_by_owner


def workspace_process_holders(worktree: Path) -> tuple[WorkspaceProcessHolder, ...]:
    """Describe processes whose cwd or open files still touch a worktree."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return ()
    resolved_path = worktree.resolve()
    resolved = str(resolved_path)
    prefix = resolved + os.sep
    holders: list[WorkspaceProcessHolder] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        holds_cwd = _proc_link_touches_workspace(entry / "cwd", resolved, prefix)
        read_paths: list[str] = []
        write_paths: list[str] = []
        unknown_paths: list[str] = []
        fd_dir = entry / "fd"
        try:
            fd_links = tuple(fd_dir.iterdir())
        except OSError:
            fd_links = ()
        for link in fd_links:
            target = _proc_link_workspace_target(link, resolved, prefix)
            if target is None:
                continue
            relative = _workspace_relative_process_path(target, resolved_path)
            access = _proc_fd_access(entry, link.name)
            if access == "write":
                write_paths.append(relative)
            elif access == "read":
                read_paths.append(relative)
            else:
                unknown_paths.append(relative)
        if not holds_cwd and not read_paths and not write_paths and not unknown_paths:
            continue
        pid = int(entry.name)
        try:
            process_group = os.getpgid(pid)
        except (OSError, ProcessLookupError):
            process_group = 0
        try:
            command = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            command = ""
        holders.append(
            WorkspaceProcessHolder(
                pid=pid,
                process_group=process_group,
                command=command,
                holds_cwd=holds_cwd,
                read_paths=tuple(sorted(set(read_paths))),
                write_paths=tuple(sorted(set(write_paths))),
                unknown_paths=tuple(sorted(set(unknown_paths))),
            )
        )
    return tuple(sorted(holders, key=lambda item: item.pid))


def format_workspace_process_holders(
    holders: tuple[WorkspaceProcessHolder, ...],
    *,
    max_holders: int = 12,
    max_paths_per_access: int = 8,
) -> str:
    records: list[dict[str, Any]] = []
    for holder in holders[:max_holders]:
        record = holder.to_dict()
        for field_name in ("read_paths", "write_paths", "unknown_paths"):
            paths = list(record[field_name])
            record[field_name] = paths[:max_paths_per_access]
            if len(paths) > max_paths_per_access:
                record[f"{field_name}_omitted"] = len(paths) - max_paths_per_access
        records.append(record)
    payload: dict[str, Any] = {"holders": records}
    if len(holders) > max_holders:
        payload["holders_omitted"] = len(holders) - max_holders
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def workspace_has_live_processes(worktree: Path) -> bool:
    """Detect processes whose cwd or open files still touch a worktree."""
    return bool(workspace_process_holders(worktree))


def _proc_link_touches_workspace(link: Path, resolved: str, prefix: str) -> bool:
    return _proc_link_workspace_target(link, resolved, prefix) is not None


def _proc_link_workspace_target(link: Path, resolved: str, prefix: str) -> str | None:
    try:
        target = os.readlink(link)
    except OSError:
        return None
    normalized = target.removesuffix(" (deleted)")
    if normalized == resolved or normalized.startswith(prefix):
        return normalized
    return None


def _workspace_relative_process_path(target: str, worktree: Path) -> str:
    try:
        relative = Path(target).relative_to(worktree)
    except ValueError:
        return target
    return str(relative) if str(relative) else "."


def _proc_fd_access(process_entry: Path, fd_name: str) -> str:
    try:
        lines = (process_entry / "fdinfo" / fd_name).read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
    except OSError:
        return "unknown"
    flags_text = next(
        (line.partition(":")[2].strip() for line in lines if line.startswith("flags:")),
        "",
    )
    try:
        flags = int(flags_text, 8)
    except ValueError:
        return "unknown"
    mode = flags & os.O_ACCMODE
    if mode in {os.O_WRONLY, os.O_RDWR}:
        return "write"
    if mode == os.O_RDONLY:
        return "read"
    return "unknown"


@contextmanager
def exclusive_workspace_lock(worktree: Path) -> Iterator[None]:
    import fcntl

    git_dir_text = _git(worktree, "rev-parse", "--git-dir").strip()
    git_dir = (worktree / git_dir_text).resolve()
    lock_path = git_dir / "pal-bunshin-v2.snapshot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
