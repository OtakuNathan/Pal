"""Bounded filesystem snapshot without authoring-store dependencies."""
from __future__ import annotations
import hashlib
import os
import subprocess
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


def verification_corpus_snapshot(workspace: Mapping[str, Any]) -> dict[str, Any]:
    """Snapshot explicit corpus scopes with bounded, no-follow descriptor reads.

    Relative paths agree across Manager/sandbox mounts. Races, unreadable files,
    and exceeded limits fail closed; no partial digest can qualify a receipt.
    """
    binding = dict(workspace.get("bunshin_v2") or {})
    root_value = str(workspace.get("repo_path") or "")
    root = Path(root_value).resolve() if root_value else None
    candidate = ""
    if root is not None and (root / ".git").exists():
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        candidate = head.stdout.strip()
    entries: dict[str, str] = {}
    count = total_bytes = 0
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def collect(parent: int, name: str, relative: str, depth: int = 0, kind: str = "") -> None:
        nonlocal count, total_bytes
        count += 1
        if count > 10000 or depth > 64:
            raise ValueError("verification corpus snapshot exceeds its file/depth limit")
        try:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            entries[relative] = "missing"
            return
        if stat.S_ISLNK(before.st_mode):
            entries[relative] = "symlink:" + os.readlink(name, dir_fd=parent)
        elif kind == "file" and not stat.S_ISREG(before.st_mode):
            entries[relative] = "not-a-file"
        elif kind == "directory" and not stat.S_ISDIR(before.st_mode):
            entries[relative] = "not-a-directory"
        elif stat.S_ISREG(before.st_mode):
            total_bytes += before.st_size
            if total_bytes > 256 * 1024 * 1024:
                raise ValueError("verification corpus snapshot exceeds its byte limit")
            with os.fdopen(os.open(name, flags, dir_fd=parent), "rb") as stream:
                if identity(os.fstat(stream.fileno())) != identity(before):
                    raise ValueError("verification corpus changed during snapshot")
                digest = hashlib.sha256()
                remaining = before.st_size
                while True:
                    chunk = stream.read(min(1024 * 1024, remaining + 1))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise ValueError("verification corpus changed during snapshot")
                    digest.update(chunk)
                entries[relative] = digest.hexdigest()
                if identity(os.fstat(stream.fileno())) != identity(before):
                    raise ValueError("verification corpus changed during snapshot")
        elif stat.S_ISDIR(before.st_mode):
            fd = os.open(name, flags | os.O_DIRECTORY, dir_fd=parent)
            try:
                if identity(os.fstat(fd)) != identity(before):
                    raise ValueError("verification corpus changed during snapshot")
                with os.scandir(fd) as children:
                    for child in children:
                        collect(fd, child.name, f"{relative}/{child.name}".strip("/"), depth + 1)
            finally:
                os.close(fd)
        else:
            raise ValueError("verification corpus contains an unsupported file type")
        if identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity(before):
            raise ValueError("verification corpus changed during snapshot")

    if workspace.get("verification_scratch_only"):
        scratch = str(workspace.get("review_scratch_dir") or "")
        if scratch:
            path = Path(scratch)
            parent = os.open(path.parent, flags | os.O_DIRECTORY)
            try:
                collect(parent, path.name, "review_scratch", kind="directory")
            finally:
                os.close(parent)
    elif root is not None:
        root_fd = os.open(root, flags | os.O_DIRECTORY)
        try:
            for raw in list(workspace.get("write_path_scopes") or []):
                scope = dict(raw or {})
                relative = PurePosixPath(str(scope.get("path") or ""))
                if (str(relative) in {"", "."} or relative.is_absolute()
                        or ".." in relative.parts or scope.get("kind") not in {"file", "directory"}):
                    raise ValueError("verification corpus requires a safe bound path scope")
                parent = os.dup(root_fd)
                ancestors = []
                try:
                    for part in relative.parts[:-1]:
                        try:
                            info = os.stat(part, dir_fd=parent, follow_symlinks=False)
                        except FileNotFoundError:
                            entries[str(relative)] = "missing"
                            break
                        if stat.S_ISLNK(info.st_mode):
                            entries[str(relative)] = "symlink:" + os.readlink(part, dir_fd=parent)
                            break
                        next_fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
                        if identity(os.fstat(next_fd)) != identity(info):
                            os.close(next_fd)
                            raise ValueError("verification corpus changed during snapshot")
                        ancestors.append((parent, part, info))
                        parent = next_fd
                    else:
                        collect(parent, relative.name, str(relative), kind=scope["kind"])
                    for ancestor_fd, part, before in ancestors:
                        if identity(os.stat(part, dir_fd=ancestor_fd, follow_symlinks=False)) != identity(before):
                            raise ValueError("verification corpus changed during snapshot")
                finally:
                    os.close(parent)
                    for ancestor_fd, _, _ in ancestors:
                        os.close(ancestor_fd)
        finally:
            os.close(root_fd)
    return {"input_fingerprint": str(binding.get("authoring_input_fingerprint") or ""),
            "case_revision": int(workspace.get("verification_case_revision") or 0),
            "candidate_digest": candidate,
            "corpus": [[name, digest] for name, digest in sorted(entries.items())]}
