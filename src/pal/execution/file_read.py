"""Structured file read tool for text files.

Reads a text file, returns a line-numbered slice, and records whether the
caller actually saw the complete file before a later mutation.

Only UTF-8 (and compatible) text files are supported.  Binary files,
images, and PDFs should be handled through artifact / vision tools instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.execution.file_state import FileStateCache, resolve_file_path
from pal.execution.file_tool_contracts import DEFAULT_FILE_READ_LIMIT
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

DEFAULT_LIMIT = DEFAULT_FILE_READ_LIMIT
FILE_UNCHANGED_STUB = (
    "File unchanged since the previous read of this line range. "
    "Use the prior content already in context."
)


@dataclass
class FileReadTool:
    """Business handler for validated file reads and shared state caching."""

    cache: FileStateCache = field(default_factory=FileStateCache)

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

        resolved = resolve_file_path(file_path)

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

        cached_state = self.cache.get_valid_state(resolved)
        if cached_state is not None:
            raw = cached_state.content
        else:
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

        # Build line-numbered output.
        lines = raw.splitlines(keepends=True)
        total_lines = len(lines)
        start = max(1, offset)
        end = min(start + limit - 1, total_lines)
        full_view = start == 1 and (total_lines == 0 or end == total_lines)
        view = (start, end)

        if (
            cached_state is not None
            and cached_state.full_view
            and cached_state.last_view == view
        ):
            return _result(
                RuntimeStatus.OK,
                FILE_UNCHANGED_STUB,
                file_path=str(resolved),
                start_line=start,
                end_line=end,
                total_lines=total_lines,
                truncated=end < total_lines,
                full_view=cached_state.full_view,
                unchanged=True,
                encoding="utf-8",
            )

        # The cache stores full bytes for stale detection, but only a complete
        # visible read grants permission to mutate the whole file.
        self.cache.mark_read(str(resolved), raw, full_view=full_view, view=view)

        # Offset beyond file: return informative message instead of empty content.
        if start > total_lines:
            msg = f"(file has {total_lines} lines; offset {start} is beyond end of file)"
            structured = {
                "file_path": str(resolved),
                "start_line": start,
                "end_line": total_lines,
                "total_lines": total_lines,
                "truncated": False,
                "full_view": False,
                "unchanged": False,
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
            "full_view": full_view,
            "unchanged": False,
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
