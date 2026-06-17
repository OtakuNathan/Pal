"""Structured file write tool for UTF-8 text files.

``FileWriteTool`` supports whole-file creation, replacement, and append. Create
mode creates missing parent directories. Writes to existing files require a
current :class:`FileStateCache` snapshot so Pal does not modify content it has
not read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.execution.file_state import FileStateCache
from pal.shared import RuntimeStatus


MAX_CONTENT_BYTES = 1_000_000

ERR_FILE_EXISTS = "FILE_EXISTS"
ERR_FILE_NOT_FOUND = "FILE_NOT_FOUND"
ERR_INVALID_MODE = "INVALID_MODE"
ERR_MISSING_CONTENT = "MISSING_CONTENT"
ERR_MISSING_FILE_PATH = "MISSING_FILE_PATH"
ERR_NOT_A_FILE = "NOT_A_FILE"
ERR_NOT_READ = "NOT_READ"
ERR_PARENT_DIR_NOT_FOUND = "PARENT_DIR_NOT_FOUND"
ERR_PARENT_NOT_DIRECTORY = "PARENT_NOT_DIRECTORY"
ERR_STALE_FILE = "STALE_FILE"
ERR_BINARY_CONTENT = "BINARY_CONTENT"
ERR_CONTENT_TOO_LARGE = "CONTENT_TOO_LARGE"
ERR_READ_FAILED = "READ_FAILED"
ERR_WRITE_FAILED = "WRITE_FAILED"

_ERROR_LLMS: dict[str, str] = {
    ERR_FILE_EXISTS: "The file already exists. Use mode='overwrite' to replace it, or mode='append' to add to it after reading it.",
    ERR_FILE_NOT_FOUND: "The specified file does not exist.",
    ERR_INVALID_MODE: "mode must be one of: 'create', 'overwrite', 'append'.",
    ERR_MISSING_CONTENT: "content is required and must be a string.",
    ERR_MISSING_FILE_PATH: "file_path is required.",
    ERR_NOT_A_FILE: "The specified path is not a regular file.",
    ERR_NOT_READ: "File has not been read yet. Read it first with file_read before overwriting or appending.",
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
    """Create, overwrite, or append to a UTF-8 text file."""

    name: str = "op_file_write"
    display_name: str = "File Write"
    family: str = "system"
    description: str = (
        "Use this first for creating, overwriting, or appending UTF-8 text files; do not use op_exec_shell with tee/echo/printf redirection for file writes when this tool is visible. "
        "Create, overwrite, or append to a UTF-8 text file. "
        "Create mode creates missing parent directories. "
        "Overwrite and append require a current prior file_read snapshot."
    )
    tags: tuple[str, ...] = ("file", "write", "create", "append", "overwrite", "system")
    keywords: tuple[str, ...] = ("write", "create", "save", "file", "new", "append", "overwrite")
    cache: FileStateCache = field(default_factory=FileStateCache)
    args_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.args_schema:
            self.args_schema = {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path for the new file to create.",
                    },
                    "content": {
                        "type": "string",
                        "description": "UTF-8 text content for the new file.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["create", "overwrite", "append"],
                        "default": "create",
                        "description": (
                            "Write mode. 'create' creates a new file and fails if it exists. "
                            "Create mode also creates missing parent directories. "
                            "'overwrite' replaces the full existing file after file_read. "
                            "'append' adds content to the end of an existing file after file_read."
                        ),
                    },
                },
                "required": ["file_path", "content"],
            }
        if not self.result_schema:
            self.result_schema = {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "bytes_written": {"type": "integer"},
                    "created": {"type": "boolean"},
                    "encoding": {"type": "string"},
                    "mode": {"type": "string"},
                    "error_code": {"type": "string"},
                },
            }

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        file_path = str(args.get("file_path") or "").strip()
        content = args.get("content")
        mode = str(args.get("mode") or "create").strip().lower()

        if not file_path:
            return _err(RuntimeStatus.INVALID, ERR_MISSING_FILE_PATH)
        if not isinstance(content, str):
            return _err(RuntimeStatus.INVALID, ERR_MISSING_CONTENT, file_path=file_path)
        if mode not in {"create", "overwrite", "append"}:
            return _err(RuntimeStatus.INVALID, ERR_INVALID_MODE, file_path=file_path, mode=mode)

        content_err = _validate_content(content)
        if content_err is not None:
            return content_err

        try:
            resolved = Path(file_path).expanduser().resolve()
        except (OSError, ValueError) as exc:
            return _err(RuntimeStatus.INVALID, ERR_WRITE_FAILED, file_path=file_path, details=str(exc))

        if mode == "create":
            return self._create(resolved, content)
        if mode == "overwrite":
            return self._overwrite(resolved, content)
        return self._append(resolved, content)

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        _ = kwargs
        return self.invoke(args)

    def _create(self, resolved: Path, content: str) -> CapabilityResult:
        if resolved.exists():
            return _err(RuntimeStatus.INVALID, ERR_FILE_EXISTS, file_path=str(resolved))
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
            return _err(RuntimeStatus.INVALID, ERR_FILE_EXISTS, file_path=str(resolved))
        except OSError as exc:
            return _err(RuntimeStatus.ERROR, ERR_WRITE_FAILED, file_path=str(resolved), details=str(exc))

        self.cache.mark_read(resolved, content)
        return _ok(resolved, content, mode="create", created=True)

    def _overwrite(self, resolved: Path, content: str) -> CapabilityResult:
        cached = self._require_current_snapshot(resolved)
        if isinstance(cached, CapabilityResult):
            return cached

        try:
            resolved.write_text(content, encoding="utf-8")
        except OSError as exc:
            return _err(RuntimeStatus.ERROR, ERR_WRITE_FAILED, file_path=str(resolved), details=str(exc))

        self.cache.mark_read(resolved, content)
        return _ok(resolved, content, mode="overwrite", created=False)

    def _append(self, resolved: Path, content: str) -> CapabilityResult:
        cached = self._require_current_snapshot(resolved)
        if isinstance(cached, CapabilityResult):
            return cached

        try:
            with resolved.open("a", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            return _err(RuntimeStatus.ERROR, ERR_WRITE_FAILED, file_path=str(resolved), details=str(exc))

        new_content = cached + content
        self.cache.mark_read(resolved, new_content)
        return _ok(resolved, content, mode="append", created=False)

    def _require_current_snapshot(self, resolved: Path) -> str | CapabilityResult:
        if not resolved.exists():
            return _err(RuntimeStatus.ERROR, ERR_FILE_NOT_FOUND, file_path=str(resolved))
        if not resolved.is_file():
            return _err(RuntimeStatus.ERROR, ERR_NOT_A_FILE, file_path=str(resolved))

        had_record = resolved in self.cache
        cached_content = self.cache.get_valid(resolved)
        if cached_content is None:
            if not had_record:
                return _err(RuntimeStatus.FORBIDDEN, ERR_NOT_READ, file_path=str(resolved))
            return _err(RuntimeStatus.FORBIDDEN, ERR_STALE_FILE, file_path=str(resolved))

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


def _ok(resolved: Path, content: str, *, mode: str, created: bool) -> CapabilityResult:
    bytes_written = len(content.encode("utf-8"))
    action = "created" if created else ("appended to" if mode == "append" else "overwrote")
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
            "mode": mode,
        },
    )
