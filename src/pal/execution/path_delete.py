"""Structured path delete tool with read-before-delete safety for files."""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.execution.file_state import FileStateCache
from pal.shared import RuntimeStatus


ERR_DELETE_FAILED = "DELETE_FAILED"
ERR_DIRECTORY_REQUIRES_RECURSIVE = "DIRECTORY_REQUIRES_RECURSIVE"
ERR_INVALID_SHA256 = "INVALID_SHA256"
ERR_MISSING_PATH = "MISSING_PATH"
ERR_NOT_READ = "NOT_READ"
ERR_PATH_NOT_FOUND = "PATH_NOT_FOUND"
ERR_READ_FAILED = "READ_FAILED"
ERR_SHA256_MISMATCH = "SHA256_MISMATCH"
ERR_STALE_PATH = "STALE_PATH"
ERR_UNSAFE_PATH = "UNSAFE_PATH"
ERR_UNSUPPORTED_PATH = "UNSUPPORTED_PATH"

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_ERROR_LLMS: dict[str, str] = {
    ERR_DELETE_FAILED: "Failed to delete path.",
    ERR_DIRECTORY_REQUIRES_RECURSIVE: "The path is a directory. Set recursive=true to delete a directory.",
    ERR_INVALID_SHA256: "expected_sha256 must be a 64-character hexadecimal SHA-256 digest.",
    ERR_MISSING_PATH: "file_path is required.",
    ERR_NOT_READ: "Path has not been read yet. Read the file first with file_read before deleting, or provide expected_sha256.",
    ERR_PATH_NOT_FOUND: "The specified path does not exist.",
    ERR_READ_FAILED: "Failed to read file before deleting.",
    ERR_SHA256_MISMATCH: "File SHA-256 does not match expected_sha256. Read or inspect the file again before deleting.",
    ERR_STALE_PATH: "File has been modified since read. Read it again before deleting, or provide the current expected_sha256.",
    ERR_UNSAFE_PATH: "Refusing to delete an unsafe path.",
    ERR_UNSUPPORTED_PATH: "The specified path is not a regular file or directory.",
}


@dataclass
class PathDeleteTool:
    """Delete a file or directory through a structured, auditable entrypoint."""

    name: str = "op_path_delete"
    display_name: str = "Path Delete"
    family: str = "system"
    description: str = (
        "Use this first for deleting files or directories; do not use op_exec_shell with rm/unlink/rmdir/git rm/find -delete when this tool is visible. "
        "Delete a file or directory. Regular files must have a current prior file_read snapshot, "
        "or expected_sha256 must match the current file bytes. Directories require recursive=true."
    )
    tags: tuple[str, ...] = ("path", "file", "directory", "delete", "remove", "system", "write")
    keywords: tuple[str, ...] = ("delete", "remove", "unlink", "rm", "path", "file", "directory")
    cache: FileStateCache = field(default_factory=FileStateCache)
    args_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.args_schema:
            self.args_schema = {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to delete."},
                    "expected_sha256": {
                        "type": "string",
                        "description": "Optional current SHA-256 digest for regular files. If supplied, a prior file_read snapshot is not required.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "default": False,
                        "description": "Required for directory deletion. Regular file deletion does not require this.",
                    },
                },
                "required": ["file_path"],
            }
        if not self.result_schema:
            self.result_schema = {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "deleted": {"type": "boolean"},
                    "path_kind": {"type": "string"},
                    "recursive": {"type": "boolean"},
                    "sha256": {"type": "string"},
                    "error_code": {"type": "string"},
                },
            }

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        file_path = str(args.get("file_path") or "").strip()
        expected_sha256 = str(args.get("expected_sha256") or "").strip().lower()
        recursive = bool(args.get("recursive"))
        if not file_path:
            return _err(RuntimeStatus.INVALID, ERR_MISSING_PATH)
        if expected_sha256 and _SHA256_RE.fullmatch(expected_sha256) is None:
            return _err(RuntimeStatus.INVALID, ERR_INVALID_SHA256, file_path=file_path)

        try:
            resolved = Path(file_path).expanduser().resolve()
        except (OSError, ValueError) as exc:
            return _err(RuntimeStatus.INVALID, ERR_DELETE_FAILED, file_path=file_path, details=str(exc))

        if _is_unsafe_delete_target(resolved):
            return _err(RuntimeStatus.FORBIDDEN, ERR_UNSAFE_PATH, file_path=str(resolved))
        if not resolved.exists():
            return _err(RuntimeStatus.ERROR, ERR_PATH_NOT_FOUND, file_path=str(resolved))

        if resolved.is_dir():
            if not recursive:
                return _err(RuntimeStatus.FORBIDDEN, ERR_DIRECTORY_REQUIRES_RECURSIVE, file_path=str(resolved))
            if expected_sha256:
                return _err(RuntimeStatus.INVALID, ERR_INVALID_SHA256, file_path=str(resolved), details="sha256 is only supported for files")
            return self._delete_path(resolved, recursive=True, path_kind="directory")

        if not resolved.is_file():
            return _err(RuntimeStatus.ERROR, ERR_UNSUPPORTED_PATH, file_path=str(resolved))

        if not expected_sha256:
            snapshot = self._require_current_snapshot(resolved)
            if isinstance(snapshot, CapabilityResult):
                return snapshot

        try:
            content = resolved.read_bytes()
        except OSError as exc:
            return _err(RuntimeStatus.ERROR, ERR_READ_FAILED, file_path=str(resolved), details=str(exc))

        digest = hashlib.sha256(content).hexdigest()
        if expected_sha256 and digest != expected_sha256:
            return _err(
                RuntimeStatus.FORBIDDEN,
                ERR_SHA256_MISMATCH,
                file_path=str(resolved),
                sha256=digest,
                expected_sha256=expected_sha256,
            )

        return self._delete_path(resolved, recursive=recursive, path_kind="file", sha256=digest)

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        _ = kwargs
        return self.invoke(args)

    def _delete_path(self, resolved: Path, *, recursive: bool, path_kind: str, sha256: str = "") -> CapabilityResult:
        try:
            if path_kind == "directory":
                shutil.rmtree(resolved)
            else:
                resolved.unlink()
        except OSError as exc:
            return _err(RuntimeStatus.ERROR, ERR_DELETE_FAILED, file_path=str(resolved), details=str(exc))

        self.cache.invalidate(resolved)
        structured: dict[str, Any] = {
            "file_path": str(resolved),
            "deleted": True,
            "path_kind": path_kind,
            "recursive": recursive,
        }
        if sha256:
            structured["sha256"] = sha256
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text=f"Deleted {path_kind}: {resolved}",
            llm_text=f"Deleted {path_kind}: {resolved}",
            structured=structured,
        )

    def _require_current_snapshot(self, resolved: Path) -> str | CapabilityResult:
        had_record = resolved in self.cache
        cached_content = self.cache.get_valid(resolved)
        if cached_content is None:
            if not had_record:
                return _err(RuntimeStatus.FORBIDDEN, ERR_NOT_READ, file_path=str(resolved))
            return _err(RuntimeStatus.FORBIDDEN, ERR_STALE_PATH, file_path=str(resolved))
        return cached_content


def _is_unsafe_delete_target(path: Path) -> bool:
    text = str(path)
    return path.parent == path or text in {"", ".", "/"}


def _err(status: str, error_code: str, **structured: Any) -> CapabilityResult:
    text = _ERROR_LLMS.get(error_code, error_code)
    payload = {"error_code": error_code, **structured}
    return CapabilityResult(status=status, text=text, llm_text=text, structured=payload)
