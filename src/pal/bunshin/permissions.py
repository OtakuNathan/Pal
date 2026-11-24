"""Filesystem policy for one Bunshin role and its sandboxed file tools."""
from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from pal.bunshin.ipc import BUNSHIN_RUNTIME_DB_PATH_ENV, bunshin_role_socket_path
from pal.bunshin.workspace_paths import path_scope_matches, repository_path_targets_control_plane
from pal.foundation.bwrap_permissions import DirectoryPermissions, ReadOnlyBind, append_bind_path, append_dir_scaffold
from pal.foundation.encryption import ensure_runtime_snapshot_key
from pal.shared import BunshinInvocationPack


BUNSHIN_SANDBOX_REFERENCE_ROOT = PurePosixPath("/pal/references")


def _protected_paths(workspace: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(path or "").strip().replace("\\", "/")
        for path in workspace.get("read_only_overlay_paths") or ()
    )


def workspace_permissions(workspace: Mapping[str, Any]) -> DirectoryPermissions:
    """Translate the assigned workspace into OS grants without creating files."""
    mode = str((workspace.get("workspace_policy") or {}).get("mode") or "").strip().lower()
    protected = _protected_paths(workspace)
    if mode == "read_only_repo":
        return DirectoryPermissions(read_only=True, protected_paths=protected)
    if not workspace.get("coder_repository_read") or workspace.get("execution_mode") == "direct":
        return DirectoryPermissions(read_only=False, protected_paths=protected)
    scopes = workspace["write_path_scopes"]
    if any(scope.get("kind") == "repository" for scope in scopes):
        return DirectoryPermissions(read_only=False, protected_paths=protected)
    directories: list[str] = []
    files: list[str] = []
    for scope in scopes:
        path = scope["path"]
        if repository_path_targets_control_plane(path):
            raise ValueError(f"invalid Coder write scope: {path}")
        if scope["kind"] == "directory":
            directories.append(path)
        elif scope["kind"] == "file":
            files.append(path)
        else:
            raise ValueError(f"unsupported Coder write scope: {scope}")
    return DirectoryPermissions(
        read_only=True,
        writable_directories=tuple(directories),
        editable_files=tuple(files),
        protected_paths=protected,
    )


def mount_role_workspace(
    args: list[str], root: Path | None, workspace: Mapping[str, Any],
) -> dict[str, Path]:
    if root is None or not root.exists():
        return {}
    files = workspace_permissions(workspace).mount_into(args, root)
    git_marker = root / ".git"
    if git_marker.exists() or git_marker.is_symlink():
        append_bind_path(args, git_marker, read_only=True)
    return files


def workspace_write_denial(workspace: Mapping[str, Any], relative: str) -> str | None:
    """Return the file tool's rejection reason for a resolved repository path."""
    if any(PurePosixPath(relative).is_relative_to(path) for path in _protected_paths(workspace) if path):
        return "read_only_overlay"
    if "write_path_scopes" in workspace and not any(
        path_scope_matches(relative, scope)
        for scope in workspace.get("write_path_scopes") or () if isinstance(scope, dict)
    ):
        return "outside_write_scope"
    return None


@contextmanager
def coder_file_commit(workspace: Mapping[str, Any], target: Path) -> Iterator[None]:
    from pal.execution.file_state import file_commit_port
    from pal.execution.mounted_file_editor import commit_mounted_file

    if not os.environ.get("PAL_BUNSHIN_SANDBOXED") or not workspace.get("coder_repository_read"):
        yield
        return
    root = Path(workspace["repo_path"]).resolve() if workspace.get("repo_path") else None
    if root is None or not target.is_relative_to(root):
        yield
        return
    key = target.relative_to(root).as_posix()
    if not workspace_permissions(workspace).requires_editor(key):
        yield
        return

    def commit(path: Path, **fields: Any) -> None:
        if path != target:
            raise OSError("file commit path differs from the admitted Coder tool call")
        commit_mounted_file(key, **fields)

    token = file_commit_port.set(commit)
    try:
        yield
    finally:
        file_commit_port.reset(token)


def mount_role_references(
    args: list[str],
    references: list[dict[str, Any]],
) -> None:
    created_dirs: set[str] = set()
    for reference in references:
        source = Path(str(reference.get("source_path") or "")).expanduser()
        target = PurePosixPath(str(reference.get("target_path") or ""))
        required = bool(reference.get("required", True))
        if not source.exists() or not source.is_dir():
            if required:
                raise RuntimeError(
                    f"sandbox reference source is unavailable: {reference.get('name') or source}"
                )
            continue
        if not target.is_absolute() or target.parent != BUNSHIN_SANDBOX_REFERENCE_ROOT:
            raise RuntimeError(f"invalid sandbox reference target: {target}")
        includes = [str(item).strip() for item in list(reference.get("include") or []) if str(item).strip()]
        _append_virtual_dirs(args, target, created_dirs)
        if not includes:
            args.extend(["--ro-bind", str(source.resolve()), str(target)])
            continue
        args.extend(["--tmpfs", str(target)])
        matches = _reference_projection_matches(source, includes)
        if not matches and required:
            raise RuntimeError(
                f"sandbox reference include set is empty: {reference.get('name') or source}"
            )
        bound_directories: list[Path] = []
        for relative, resolved in matches:
            if any(relative == parent or relative.is_relative_to(parent) for parent in bound_directories):
                continue
            destination = target.joinpath(*relative.parts)
            if resolved.is_dir():
                _append_virtual_dirs(args, destination, created_dirs)
                args.extend(["--ro-bind", str(resolved), str(destination)])
                bound_directories.append(relative)
            else:
                _append_virtual_dirs(args, destination.parent, created_dirs)
                args.extend(["--ro-bind", str(resolved), str(destination)])
        args.extend(["--remount-ro", str(target)])


def _reference_projection_matches(
    source: Path,
    includes: list[str],
) -> list[tuple[Path, Path]]:
    root = source.resolve()
    matches: dict[Path, Path] = {}
    for pattern in includes:
        normalized = pattern.replace("\\", "/").strip()
        parts = PurePosixPath(normalized).parts
        if not normalized or normalized.startswith("/") or ".." in parts:
            raise RuntimeError(f"invalid sandbox reference include pattern: {pattern}")
        for candidate in source.glob(normalized):
            try:
                resolved = candidate.resolve(strict=True)
                relative = candidate.relative_to(source)
            except (OSError, ValueError):
                continue
            if not resolved.is_relative_to(root):
                raise RuntimeError(f"sandbox reference include escapes its source root: {candidate}")
            if relative.parts:
                matches[relative] = resolved
    return sorted(matches.items(), key=lambda item: (len(item[0].parts), str(item[0])))


def _append_virtual_dirs(
    args: list[str],
    path: PurePosixPath,
    created: set[str],
) -> None:
    current = PurePosixPath("/")
    for part in path.parts[1:]:
        current /= part
        value = str(current)
        if value in created:
            continue
        args.extend(["--dir", value])
        created.add(value)


def mount_role_runtime(
    args: list[str],
    runtime_root: Path,
    pack: BunshinInvocationPack,
    *,
    env: Mapping[str, str],
) -> None:
    runtime_root = Path(runtime_root).expanduser()
    snapshot_key = ensure_runtime_snapshot_key(runtime_root)
    append_bind_path(args, snapshot_key, read_only=True)
    configured_db_path = str(env.get(BUNSHIN_RUNTIME_DB_PATH_ENV) or "").strip()
    database_path = (
        Path(configured_db_path).expanduser()
        if configured_db_path
        else runtime_root / "pal.sqlite3"
    )
    for path in (
        database_path,
        Path(f"{database_path}-shm"),
        Path(f"{database_path}-wal"),
        runtime_root / "config.toml",
    ):
        if path.exists():
            append_bind_path(args, path, read_only=True)
    for dir_name in ("plugins", "SKILL", "llm/models", "llm/providers", "llm/endpoints"):
        path = runtime_root / dir_name
        if path.exists():
            append_bind_path(args, path, read_only=True)
    memory_root = runtime_root / "memory"
    if memory_root.is_dir():
        append_bind_path(args, memory_root, read_only=True)
    run_dir_value = str(pack.workspace.get("run_dir") or "").strip()
    run_dir = Path(run_dir_value).expanduser() if run_dir_value else None
    if run_dir is not None and run_dir.is_dir():
        append_bind_path(args, run_dir, read_only=False)
    endpoint_path = bunshin_role_socket_path(runtime_root)
    if not endpoint_path.exists():
        raise RuntimeError(
            "sandboxed Bunshin requires the assignment-scoped Unix role gateway"
        )
    append_bind_path(args, endpoint_path, read_only=True)


def mount_role_runtime_dependencies(
    args: list[str], runtime_root: Path, scratch_tmp: Path, mounts: tuple[ReadOnlyBind, ...],
) -> None:
    """Add host declarations after the sandbox has claimed its own mount paths."""
    data_root = runtime_root.expanduser().absolute() / "data"
    occupied = set()
    for index, option in enumerate(args):
        if option in {"--bind", "--ro-bind", "--symlink"}:
            occupied.add(Path(args[index + 2]))
        elif option in {"--tmpfs", "--proc", "--dev", "--remount-ro"}:
            occupied.add(Path(args[index + 1]))
    # /tmp is the worker's private scratch projection. Runtime paths can sit
    # below it, but no additional mount may replace it.
    occupied.discard(Path("/tmp"))
    pending = []
    for mount in mounts:
        target = mount.target.expanduser()
        if target == data_root or ".." in target.parts or not target.is_relative_to(data_root):
            raise ValueError(f"runtime dependency mount must be below {data_root}: {target}")
        if any(target.is_relative_to(path) or path.is_relative_to(target) for path in occupied):
            raise ValueError(f"runtime dependency mount overlaps an existing mount: {target}")
        if target.is_relative_to("/tmp"):
            projected = scratch_tmp / target.relative_to("/tmp")
            if projected.resolve() != scratch_tmp.resolve() / target.relative_to("/tmp"):
                raise ValueError(f"runtime dependency mount traverses a scratch symlink: {target}")
        try:
            source = mount.source.expanduser().resolve(strict=True)
        except FileNotFoundError:
            if mount.required:
                raise
            continue
        pending.append((source, target))
        occupied.add(target)
    for source, target in pending:
        append_dir_scaffold(args, target.parent)
        args.extend(["--ro-bind", str(source), str(target)])
