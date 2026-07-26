"""Low-friction whole-file create-or-overwrite tool for UTF-8 text files."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.execution.file_state import (
    FileStateCache,
    resolve_file_path,
)
from pal.shared import RuntimeStatus


MAX_CONTENT_BYTES = 1_000_000

ERR_FILE_NOT_FOUND = "FILE_NOT_FOUND"
ERR_MISSING_CONTENT = "MISSING_CONTENT"
ERR_MISSING_FILE_PATH = "MISSING_FILE_PATH"
ERR_NOT_A_FILE = "NOT_A_FILE"
ERR_NOT_READ = "NOT_READ"
ERR_PARTIAL_READ = "PARTIAL_READ"
ERR_PARENT_DIR_NOT_FOUND = "PARENT_DIR_NOT_FOUND"
ERR_PARENT_NOT_DIRECTORY = "PARENT_NOT_DIRECTORY"
ERR_STALE_FILE = "STALE_FILE"
ERR_BINARY_CONTENT = "BINARY_CONTENT"
ERR_CONTENT_TOO_LARGE = "CONTENT_TOO_LARGE"
ERR_READ_FAILED = "READ_FAILED"
ERR_WRITE_FAILED = "WRITE_FAILED"

_ERROR_LLMS: dict[str, str] = {
    ERR_FILE_NOT_FOUND: "The specified file does not exist.",
    ERR_MISSING_CONTENT: "content is required and must be a string.",
    ERR_MISSING_FILE_PATH: "file_path is required.",
    ERR_NOT_A_FILE: "The specified path is not a regular file.",
    ERR_NOT_READ: "File has not been read yet. Read the complete file first before overwriting it.",
    ERR_PARTIAL_READ: "Only part of the file was read. Read the complete file before overwriting it.",
    ERR_PARENT_DIR_NOT_FOUND: "The parent directory does not exist.",
    ERR_PARENT_NOT_DIRECTORY: "The parent path exists but is not a directory.",
    ERR_STALE_FILE: "File has been modified since read. Read it again before writing.",
    ERR_BINARY_CONTENT: "Content contains binary data (NUL bytes). Only UTF-8 text is supported.",
    ERR_CONTENT_TOO_LARGE: f"Content exceeds the maximum allowed size of {MAX_CONTENT_BYTES} bytes.",
    ERR_READ_FAILED: "Failed to read file before writing.",
    ERR_WRITE_FAILED: "Failed to write file.",
}


@dataclass
class FileWriteTool:
    """Create a missing file or replace a fully-read existing file."""

    cache: FileStateCache = field(default_factory=FileStateCache)
    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        file_path = str(args.get("file_path") or "").strip()
        content = args.get("content")

        if not file_path:
            return _err(RuntimeStatus.INVALID, ERR_MISSING_FILE_PATH)
        if not isinstance(content, str):
            return _err(RuntimeStatus.INVALID, ERR_MISSING_CONTENT, file_path=file_path)

        content_err = _validate_content(content)
        if content_err is not None:
            return content_err

        try:
            resolved = resolve_file_path(file_path)
        except (OSError, ValueError) as exc:
            return _err(RuntimeStatus.INVALID, ERR_WRITE_FAILED, file_path=file_path, details=str(exc))

        if resolved.exists():
            return self._overwrite(resolved, content)
        return self._create(resolved, content)

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        _ = kwargs
        return self.invoke(args)

    def _create(self, resolved: Path, content: str) -> CapabilityResult:
        if resolved.exists():
            return self._overwrite(resolved, content)
        parent = resolved.parent
        if not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return _err(RuntimeStatus.ERROR, ERR_WRITE_FAILED, file_path=str(resolved), details=str(exc))
        if not parent.is_dir():
            return _err(RuntimeStatus.INVALID, ERR_PARENT_NOT_DIRECTORY, file_path=str(resolved))

        try:
            with resolved.open("x", encoding="utf-8") as handle:
                handle.write(content)
        except FileExistsError:
            return self._overwrite(resolved, content)
        except OSError as exc:
            return _err(RuntimeStatus.ERROR, ERR_WRITE_FAILED, file_path=str(resolved), details=str(exc))

        self.cache.mark_read(resolved, content)
        return _ok(resolved, content, old_content="", created=True)

    def _overwrite(self, resolved: Path, content: str) -> CapabilityResult:
        cached = self._require_current_snapshot(resolved)
        if isinstance(cached, CapabilityResult):
            return cached

        try:
            resolved.write_text(content, encoding="utf-8")
        except OSError as exc:
            return _err(RuntimeStatus.ERROR, ERR_WRITE_FAILED, file_path=str(resolved), details=str(exc))

        self.cache.mark_read(resolved, content)
        return _ok(resolved, content, old_content=cached, created=False)

    def _require_current_snapshot(self, resolved: Path) -> str | CapabilityResult:
        if not resolved.exists():
            return _err(RuntimeStatus.ERROR, ERR_FILE_NOT_FOUND, file_path=str(resolved))
        if not resolved.is_file():
            return _err(RuntimeStatus.ERROR, ERR_NOT_A_FILE, file_path=str(resolved))

        had_record = resolved in self.cache
        cached_state = self.cache.get_valid_state(resolved)
        if cached_state is None:
            if not had_record:
                return _err(RuntimeStatus.FORBIDDEN, ERR_NOT_READ, file_path=str(resolved))
            return _err(RuntimeStatus.FORBIDDEN, ERR_STALE_FILE, file_path=str(resolved))
        if not cached_state.full_view:
            return _err(RuntimeStatus.FORBIDDEN, ERR_PARTIAL_READ, file_path=str(resolved))
        cached_content = cached_state.content

        try:
            current_content = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            return _err(RuntimeStatus.ERROR, ERR_READ_FAILED, file_path=str(resolved), details=str(exc))

        if current_content != cached_content:
            self.cache.invalidate(resolved)
            return _err(RuntimeStatus.FORBIDDEN, ERR_STALE_FILE, file_path=str(resolved))

        return cached_content


def _validate_content(content: str) -> CapabilityResult | None:
    if "\x00" in content:
        return _err(RuntimeStatus.INVALID, ERR_BINARY_CONTENT)
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > MAX_CONTENT_BYTES:
        return _err(
            RuntimeStatus.INVALID,
            ERR_CONTENT_TOO_LARGE,
            content_bytes=content_bytes,
            max_bytes=MAX_CONTENT_BYTES,
        )
    return None


def _err(status: str, error_code: str, **structured: Any) -> CapabilityResult:
    payload: dict[str, Any] = {"error_code": error_code}
    payload.update(structured)
    text = _ERROR_LLMS[error_code]
    return CapabilityResult(status=status, text=text, llm_text=text, structured=payload)


def _ok(resolved: Path, content: str, *, old_content: str, created: bool) -> CapabilityResult:
    bytes_written = len(content.encode("utf-8"))
    operation = "create" if created else "update"
    action = "Created" if created else "Updated"
    patch = "".join(
        difflib.unified_diff(
            old_content.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=str(resolved),
            tofile=str(resolved),
            n=3,
        )
    )
    text = f"{action} {resolved} ({bytes_written} bytes)"
    return CapabilityResult(
        status=RuntimeStatus.OK,
        text=text,
        llm_text=text,
        structured={
            "file_path": str(resolved),
            "bytes_written": bytes_written,
            "created": created,
            "encoding": "utf-8",
            "operation": operation,
            "patch": patch,
        },
    )
