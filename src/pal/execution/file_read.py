"""Structured file read tool for text files.

Reads a text file, caches its full content in :class:`FileStateCache` for
subsequent :class:`FileEditTool` edits, and returns a line-numbered slice.

Only UTF-8 (and compatible) text files are supported.  Binary files,
images, and PDFs should be handled through artifact / vision tools instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.execution.file_state import FileStateCache
from pal.shared import RuntimeStatus


# Error codes
ERR_FILE_NOT_FOUND = "FILE_NOT_FOUND"
ERR_UNSUPPORTED_TEXT_ENCODING = "UNSUPPORTED_TEXT_ENCODING"
ERR_INVALID_ARGUMENT = "INVALID_ARGUMENT"

_ERROR_LLMS: dict[str, str] = {
    ERR_FILE_NOT_FOUND: "The specified file does not exist.",
    ERR_UNSUPPORTED_TEXT_ENCODING: (
        "The file could not be decoded as UTF-8 text. "
        "Binary files, images, and PDFs are not supported by file_read. "
        "Use artifact or vision tools instead."
    ),
    ERR_INVALID_ARGUMENT: "offset and limit must be positive integers.",
}

DEFAULT_LIMIT = 2000


@dataclass
class FileReadTool:
    """File read tool implementing the :class:`~pal.execution.contracts.Tool` protocol.

    Every successful read populates the shared :class:`FileStateCache` so that
    :class:`FileEditTool` can verify "read-before-edit" safety.
    """

    name: str = "op_file_read"
    display_name: str = "File Read"
    family: str = "system"
    description: str = (
        "Use this first when you need to inspect a UTF-8 text file; do not use op_exec_shell with cat/head/tail for file reads when this tool is visible. "
        "Read a text file and return a line-numbered slice. "
        "The full content is cached for subsequent file_edit calls. "
        "Supports offset and limit for large files. "
        "Only text (UTF-8) files are supported."
    )
    tags: tuple[str, ...] = ("file", "read", "system")
    keywords: tuple[str, ...] = ("read", "cat", "view", "file", "open", "load")
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
                        "description": "Path to the file to read.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Alias for file_path.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "1-based line number to start reading from. "
                            "Defaults to 1 (beginning of file)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Maximum number of lines to return. "
                            f"Defaults to {DEFAULT_LIMIT}."
                        ),
                    },
                },
                "required": ["file_path"],
            }
        if not self.result_schema:
            self.result_schema = {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "total_lines": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                    "encoding": {"type": "string"},
                    "error_code": {"type": "string"},
                },
            }

    # ------------------------------------------------------------------
    # Tool protocol
    # ------------------------------------------------------------------

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        file_path = str(args.get("file_path") or args.get("path") or "").strip()
        offset = _positive_int(args.get("offset"), default=1)
        limit = _positive_int(args.get("limit"), default=DEFAULT_LIMIT)
        if offset is None or limit is None:
            msg = _ERROR_LLMS[ERR_INVALID_ARGUMENT]
            return _result(
                RuntimeStatus.INVALID,
                msg,
                error_code=ERR_INVALID_ARGUMENT,
                offset=args.get("offset"),
                limit=args.get("limit"),
            )

        if not file_path:
            return _result(RuntimeStatus.INVALID, "file_path is required.", error_code="MISSING_FILE_PATH")

        resolved = Path(file_path).expanduser().resolve()

        if not resolved.exists():
            msg = _ERROR_LLMS[ERR_FILE_NOT_FOUND]
            return _result(
                RuntimeStatus.ERROR,
                msg,
                error_code=ERR_FILE_NOT_FOUND,
                file_path=str(resolved),
            )

        if not resolved.is_file():
            return _result(
                RuntimeStatus.ERROR,
                f"Not a regular file: {resolved}",
                error_code="NOT_A_FILE",
                file_path=str(resolved),
            )

        try:
            raw = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            msg = _ERROR_LLMS[ERR_UNSUPPORTED_TEXT_ENCODING]
            return _result(
                RuntimeStatus.ERROR,
                msg,
                error_code=ERR_UNSUPPORTED_TEXT_ENCODING,
                file_path=str(resolved),
            )
        except OSError as exc:
            return _result(
                RuntimeStatus.ERROR,
                f"failed to read file: {exc}",
                error_code="READ_FAILED",
                file_path=str(resolved),
            )

        # Cache the FULL content (not just the slice) for file_edit safety.
        self.cache.mark_read(str(resolved), raw)

        # Build line-numbered output.
        lines = raw.splitlines(keepends=True)
        total_lines = len(lines)
        start = max(1, offset)
        end = min(start + limit - 1, total_lines)

        # Offset beyond file: return informative message instead of empty content.
        if start > total_lines:
            msg = f"(file has {total_lines} lines; offset {start} is beyond end of file)"
            structured = {
                "file_path": str(resolved),
                "start_line": start,
                "end_line": total_lines,
                "total_lines": total_lines,
                "truncated": False,
                "encoding": "utf-8",
            }
            return _result(RuntimeStatus.OK, msg, **structured)

        truncated = end < total_lines

        selected = lines[start - 1 : end]
        numbered: list[str] = []
        for i, line in enumerate(selected, start=start):
            numbered.append(f"{i:>6}\t{line.rstrip(chr(10)).rstrip(chr(13))}")

        content = "\n".join(numbered)
        if truncated:
            content += f"\n\n... ({total_lines - end} more lines below)"

        structured = {
            "file_path": str(resolved),
            "start_line": start,
            "end_line": end,
            "total_lines": total_lines,
            "truncated": truncated,
            "encoding": "utf-8",
        }

        return _result(RuntimeStatus.OK, content, **structured)

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        _ = kwargs
        return self.invoke(args)


def _positive_int(value: Any, *, default: int) -> int | None:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 1:
        return None
    return parsed


def _result(status: str, text: str, **structured: Any) -> CapabilityResult:
    return CapabilityResult(status=status, llm_text=text, text=text, structured=structured)
