"""Structured file delete tool with read-before-delete safety."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.execution.file_state import FileStateCache
from pal.shared import RuntimeStatus


ERR_FILE_NOT_FOUND = "FILE_NOT_FOUND"
ERR_INVALID_SHA256 = "INVALID_SHA256"
ERR_MISSING_FILE_PATH = "MISSING_FILE_PATH"
ERR_NOT_A_FILE = "NOT_A_FILE"
ERR_NOT_READ = "NOT_READ"
ERR_READ_FAILED = "READ_FAILED"
ERR_SHA256_MISMATCH = "SHA256_MISMATCH"
ERR_STALE_FILE = "STALE_FILE"
ERR_DELETE_FAILED = "DELETE_FAILED"

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_ERROR_LLMS: dict[str, str] = {
    ERR_FILE_NOT_FOUND: "The specified file does not exist.",
    ERR_INVALID_SHA256: "expected_sha256 must be a 64-character hexadecimal SHA-256 digest.",
    ERR_MISSING_FILE_PATH: "file_path is required.",
    ERR_NOT_A_FILE: "The specified path is not a regular file.",
    ERR_NOT_READ: "File has not been read yet. Read it first with file_read before deleting, or provide expected_sha256.",
    ERR_READ_FAILED: "Failed to read file before deleting.",
    ERR_SHA256_MISMATCH: "File SHA-256 does not match expected_sha256. Read or inspect the file again before deleting.",
    ERR_STALE_FILE: "File has been modified since read. Read it again before deleting, or provide the current expected_sha256.",
    ERR_DELETE_FAILED: "Failed to delete file.",
}


@dataclass
class FileDeleteTool:
    """Delete a regular file after a current read snapshot or SHA-256 check."""

    name: str = "op_file_delete"
    display_name: str = "File Delete"
    family: str = "system"
    description: str = (
        "Delete a regular file. The file must have a current prior file_read snapshot, "
        "or expected_sha256 must match the current file bytes."
    )
    tags: tuple[str, ...] = ("file", "delete", "remove", "system", "write")
    keywords: tuple[str, ...] = ("delete", "remove", "unlink", "file")
    cache: FileStateCache = field(default_factory=FileStateCache)
    args_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.args_schema:
            self.args_schema = {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the file to delete."},
                    "expected_sha256": {
                        "type": "string",
                        "description": "Optional current SHA-256 digest. If supplied, a prior file_read snapshot is not required.",
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
                    "sha256": {"type": "string"},
                    "error_code": {"type": "string"},
                },
            }

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        file_path = str(args.get("file_path") or "").strip()
        expected_sha256 = str(args.get("expected_sha256") or "").strip().lower()
        if not file_path:
            return _err(RuntimeStatus.INVALID, ERR_MISSING_FILE_PATH)
        if expected_sha256 and _SHA256_RE.fullmatch(expected_sha256) is None:
            return _err(RuntimeStatus.INVALID, ERR_INVALID_SHA256, file_path=file_path)

        try:
            resolved = Path(file_path).expanduser().resolve()
        except (OSError, ValueError) as exc:
            return _err(RuntimeStatus.INVALID, ERR_DELETE_FAILED, file_path=file_path, details=str(exc))

        if not resolved.exists():
            return _err(RuntimeStatus.ERROR, ERR_FILE_NOT_FOUND, file_path=str(resolved))
        if not resolved.is_file():
            return _err(RuntimeStatus.ERROR, ERR_NOT_A_FILE, file_path=str(resolved))

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

        try:
            resolved.unlink()
        except OSError as exc:
            return _err(RuntimeStatus.ERROR, ERR_DELETE_FAILED, file_path=str(resolved), details=str(exc))

        self.cache.invalidate(resolved)
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text=f"Deleted file: {resolved}",
            llm_text=f"Deleted file: {resolved}",
            structured={"file_path": str(resolved), "deleted": True, "sha256": digest},
        )

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        _ = kwargs
        return self.invoke(args)

    def _require_current_snapshot(self, resolved: Path) -> str | CapabilityResult:
        had_record = resolved in self.cache
        cached_content = self.cache.get_valid(resolved)
        if cached_content is None:
            if not had_record:
                return _err(RuntimeStatus.FORBIDDEN, ERR_NOT_READ, file_path=str(resolved))
            return _err(RuntimeStatus.FORBIDDEN, ERR_STALE_FILE, file_path=str(resolved))
        return cached_content


def _err(status: str, error_code: str, **structured: Any) -> CapabilityResult:
    text = _ERROR_LLMS.get(error_code, error_code)
    payload = {"error_code": error_code, **structured}
    return CapabilityResult(status=status, text=text, llm_text=text, structured=payload)
