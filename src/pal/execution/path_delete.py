"""Structured path delete tool with read-before-delete safety for files."""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.execution.file_state import resolve_file_path
from pal.shared import RuntimeStatus


ERR_DELETE_FAILED = "DELETE_FAILED"
ERR_DIRECTORY_REQUIRES_RECURSIVE = "DIRECTORY_REQUIRES_RECURSIVE"
ERR_INVALID_SHA256 = "INVALID_SHA256"
ERR_MISSING_PATH = "MISSING_PATH"
ERR_PATH_NOT_FOUND = "PATH_NOT_FOUND"
ERR_READ_FAILED = "READ_FAILED"
ERR_SHA256_MISMATCH = "SHA256_MISMATCH"
ERR_UNSAFE_PATH = "UNSAFE_PATH"
ERR_UNSUPPORTED_PATH = "UNSUPPORTED_PATH"

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_ERROR_LLMS: dict[str, str] = {
    ERR_DELETE_FAILED: "Failed to delete path.",
    ERR_DIRECTORY_REQUIRES_RECURSIVE: "The path is a directory. Set recursive=true to delete a directory.",
    ERR_INVALID_SHA256: "expected_sha256 must be a 64-character hexadecimal SHA-256 digest.",
    ERR_MISSING_PATH: "file_path is required.",
    ERR_PATH_NOT_FOUND: "The specified path does not exist.",
    ERR_READ_FAILED: "Failed to read file before deleting.",
    ERR_SHA256_MISMATCH: "File SHA-256 does not match expected_sha256. Read or inspect the file again before deleting.",
    ERR_UNSAFE_PATH: "Refusing to delete an unsafe path.",
    ERR_UNSUPPORTED_PATH: "The specified path is not a regular file or directory.",
}


@dataclass
class PathDeleteTool:
    """Delete a file or directory through a structured, auditable entrypoint."""

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        file_path = str(args.get("file_path") or "").strip()
        expected_sha256 = str(args.get("expected_sha256") or "").strip().lower()
        recursive = bool(args.get("recursive"))
        if not file_path:
            return _err(RuntimeStatus.INVALID, ERR_MISSING_PATH)
        if expected_sha256 and _SHA256_RE.fullmatch(expected_sha256) is None:
            return _err(RuntimeStatus.INVALID, ERR_INVALID_SHA256, file_path=file_path)

        try:
            resolved = resolve_file_path(file_path)
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

def _is_unsafe_delete_target(path: Path) -> bool:
    text = str(path)
    return path.parent == path or text in {"", ".", "/"}


def _err(status: str, error_code: str, **structured: Any) -> CapabilityResult:
    text = _ERROR_LLMS.get(error_code, error_code)
    payload = {"error_code": error_code, **structured}
    return CapabilityResult(status=status, text=text, llm_text=text, structured=payload)
