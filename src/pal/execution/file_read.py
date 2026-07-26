"""Structured file read tool for text files.

Reads a text file, returns a line-numbered slice, and records whether the
caller actually saw the complete file before a later mutation.

Only UTF-8 (and compatible) text files are supported.  Binary files,
images, and PDFs should be handled through artifact / vision tools instead.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from pal.execution.contracts import CapabilityResult
from pal.execution.file_state import FileStateCache, file_cache_key, resolve_file_path
from pal.execution.file_tool_contracts import DEFAULT_FILE_READ_LIMIT
from pal.execution.session_state import (
    FileDeliveryManifest,
    FileDeliverySpan,
    LogicalExecutionContext,
    LogicalExecutionStateBackend,
)
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
    "File unchanged; this line range is already available in the current context."
)


@dataclass(frozen=True)
class _VisibleFile:
    version: str
    covered_ranges: tuple[tuple[int, int], ...]


class FileVisibilityCache:
    """Track file line ranges already returned to one LLM context.

    This cache is deliberately separate from :class:`FileStateCache`.
    ``FileStateCache`` authorizes later mutations; this cache only suppresses
    duplicate tool output. Entries are isolated by caller-provided scope and
    invalidated by the exact UTF-8 content digest rather than filesystem mtime.
    """

    MAX_ENTRIES = 4000

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._max = max(max_entries, 1)
        self._cache: OrderedDict[tuple[str, str], _VisibleFile] = OrderedDict()
        self._lock = RLock()

    def covers(
        self,
        scope: str,
        file_path: str | Path,
        *,
        version: str,
        start_line: int,
        end_line: int,
    ) -> bool:
        """Return whether the exact file version and line interval were seen."""
        if not scope or start_line > end_line:
            return False
        key = (scope, file_cache_key(file_path))
        with self._lock:
            entry = self._cache.get(key)
            if entry is None or entry.version != version:
                if entry is not None:
                    self._cache.pop(key, None)
                return False
            self._cache.move_to_end(key)
            return any(
                covered_start <= start_line and covered_end >= end_line
                for covered_start, covered_end in entry.covered_ranges
            )

    def mark_visible(
        self,
        scope: str,
        file_path: str | Path,
        *,
        version: str,
        start_line: int,
        end_line: int,
    ) -> None:
        """Record a returned line interval and merge overlapping coverage."""
        if not scope or start_line > end_line:
            return
        key = (scope, file_cache_key(file_path))
        with self._lock:
            entry = self._cache.get(key)
            ranges = (
                list(entry.covered_ranges)
                if entry is not None and entry.version == version
                else []
            )
            ranges.append((start_line, end_line))
            ranges.sort()
            merged: list[tuple[int, int]] = []
            for range_start, range_end in ranges:
                if not merged or range_start > merged[-1][1] + 1:
                    merged.append((range_start, range_end))
                    continue
                previous_start, previous_end = merged[-1]
                merged[-1] = (previous_start, max(previous_end, range_end))
            self._cache[key] = _VisibleFile(
                version=version,
                covered_ranges=tuple(merged),
            )
            self._cache.move_to_end(key)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)

    def clear_scope(self, scope: str) -> None:
        """Forget visibility for a completed or reset LLM context."""
        if not scope:
            return
        with self._lock:
            for key in [candidate for candidate in self._cache if candidate[0] == scope]:
                self._cache.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


@dataclass
class SessionFileVisibilityCache:
    backend: LogicalExecutionStateBackend
    context: LogicalExecutionContext

    def covers(
        self,
        scope: str,
        file_path: str | Path,
        *,
        version: str,
        start_line: int,
        end_line: int,
    ) -> bool:
        _ = scope
        grant = self.backend.file_grant(
            logical_session_id=self.context.logical_session_id,
            file_key=file_cache_key(file_path),
            digest=version,
        )
        if grant is None:
            return False
        if grant.total_lines == 0:
            return grant.empty_file
        return any(
            covered_start <= start_line and covered_end >= end_line
            for covered_start, covered_end in grant.covered_ranges
        )

    def mark_visible(self, *args: Any, **kwargs: Any) -> None:
        # Runtime delivery is committed only after the result enters the
        # provider-visible tool protocol.
        _ = args, kwargs

    def clear_scope(self, scope: str) -> None:
        _ = scope


@dataclass
class FileReadTool:
    """Business handler for validated file reads and shared state caching."""

    cache: FileStateCache = field(default_factory=FileStateCache)
    visibility_cache: FileVisibilityCache = field(default_factory=FileVisibilityCache)
    visibility_scope: str = field(default_factory=lambda: f"file-reader:{uuid4().hex}")
    defer_delivery: bool = False

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

        # Always compare exact content before suppressing output. mtime alone is
        # sufficient for the mutation guard's fast path, but not for deciding
        # that an LLM can safely reuse text already present in its context.
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
        version = hashlib.sha256(raw.encode("utf-8")).hexdigest()

        already_visible = self.visibility_cache.covers(
            self.visibility_scope,
            resolved,
            version=version,
            start_line=start,
            end_line=end,
        )

        # The state cache stores the complete bytes for stale detection, but
        # only a complete visible read grants permission to mutate the file.
        if not self.defer_delivery:
            self.cache.mark_read(str(resolved), raw, full_view=full_view)

        if already_visible:
            return _result(
                RuntimeStatus.OK,
                FILE_UNCHANGED_STUB,
                file_path=str(resolved),
                start_line=start,
                end_line=end,
                total_lines=total_lines,
                truncated=end < total_lines,
                full_view=full_view,
                unchanged=True,
                encoding="utf-8",
            )

        if total_lines == 0:
            manifest = FileDeliveryManifest(
                file_key=file_cache_key(resolved),
                digest=version,
                total_lines=0,
                empty_file=True,
            )
            return _result(
                RuntimeStatus.OK,
                "(empty file)",
                file_path=str(resolved),
                start_line=1,
                end_line=0,
                total_lines=0,
                truncated=False,
                full_view=True,
                unchanged=False,
                encoding="utf-8",
                content="",
                _context_delivery=manifest.to_dict(),
            )

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

        if not self.defer_delivery:
            self.visibility_cache.mark_visible(
                self.visibility_scope,
                resolved,
                version=version,
                start_line=start,
                end_line=end,
            )

        truncated = end < total_lines

        selected = lines[start - 1 : end]
        numbered: list[str] = []
        spans: list[FileDeliverySpan] = []
        cursor = 0
        for i, line in enumerate(selected, start=start):
            rendered_line = f"{i:>6}\t{line.rstrip(chr(10)).rstrip(chr(13))}"
            numbered.append(rendered_line)
            spans.append(
                FileDeliverySpan(
                    start_offset=cursor,
                    end_offset=cursor + len(rendered_line),
                    start_line=i,
                    end_line=i,
                    visible_start_in_line=0,
                    visible_end_in_line=len(rendered_line),
                    line_length=len(rendered_line),
                )
            )
            cursor += len(rendered_line) + 1

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
            "content": content,
        }
        manifest = FileDeliveryManifest(
            file_key=file_cache_key(resolved),
            digest=version,
            total_lines=total_lines,
            spans=tuple(spans),
        )

        return _result(
            RuntimeStatus.OK,
            content,
            _context_delivery=manifest.to_dict(),
            **structured,
        )

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
    delivery = structured.pop("_context_delivery", None)
    return CapabilityResult(
        status=status,
        llm_text=text,
        text=text,
        structured=structured,
        context_delivery=dict(delivery) if isinstance(delivery, dict) else None,
    )
