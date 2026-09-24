"""Structured file read tool for text files.

Reads a text file, returns a line-numbered slice, and records whether the
caller actually saw the complete file before a later mutation.

Only UTF-8 (and compatible) text files are supported. Binary files and PDFs
use artifact capabilities; inline images can be inspected directly by a
vision-capable model.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from threading import RLock
from typing import Any, Mapping
from uuid import uuid4

from pal.execution.contracts import CapabilityResult
from pal.execution.file_state import (
    FileStateCache,
    UTF8_BOM,
    file_cache_key,
    read_utf8_text_exact,
    resolve_file_path,
)
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
        "Binary files, images, and PDFs are not supported by read_file. "
        "Use artifact_import with the local path to attach an image or process a document. "
        "For a channel-delivered artifact, use artifact_info to inspect its representations; "
        "inspect image pixels only when the image is already inline and the active model supports vision."
    ),
    ERR_INVALID_ARGUMENT: "offset and limit must be positive integers.",
}

DEFAULT_LIMIT = DEFAULT_FILE_READ_LIMIT
FILE_UNCHANGED_STUB = (
    "File unchanged; this line range is already available in the current context."
)
UTF8_BOM_NOTICE = "(UTF-8 BOM present; preserved by read_file/edit_file)"


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
            execution_lifetime_id=self.context.execution_lifetime_id,
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
        # Direct results are committed by FileCapabilityMixin only after the
        # complete CapabilityResult has been returned successfully. Core-turn
        # results remain deferred until append_l1_tool_result succeeds.
        _ = (args, kwargs)

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
        blocks_error = self._validate_requested_blocks(args)
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
        if blocks_error is not None:
            return _result(
                RuntimeStatus.INVALID,
                blocks_error,
                error_code=ERR_INVALID_ARGUMENT,
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
            raw = read_utf8_text_exact(resolved)
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
        version = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        utf8_bom = raw.startswith(UTF8_BOM)
        requested_blocks = _requested_blocks(args, offset, limit)

        # A logical-session read is authorized only after its result reaches
        # L1. A failed lookup can still leave leases for an older digest in the
        # backend, so retire them only when they name bytes different from this
        # observation. Merely lacking a current grant is not invalidation.
        cached_state = self.cache.get_valid_state(resolved)
        if self.defer_delivery and cached_state is None:
            retire_obsolete = getattr(
                self.cache,
                "retire_if_observed_digest_changed",
                None,
            )
            if callable(retire_obsolete):
                retire_obsolete(resolved, observed_digest=version)

        # Normalize every requested block against the real file: clamp ends,
        # keep out-of-range starts as reportable skips.
        normalized: list[tuple[int, int]] = []
        skipped: list[dict[str, Any]] = []
        for block_offset, block_limit in requested_blocks:
            start = max(1, block_offset)
            end = min(start + block_limit - 1, total_lines)
            if start > total_lines:
                skipped.append(
                    {
                        "start_line": block_offset,
                        "end_line": total_lines,
                        "truncated": False,
                        "unchanged": False,
                        "beyond_eof": True,
                    }
                )
                continue
            normalized.append((start, end))

        visible_flags = [
            self.visibility_cache.covers(
                self.visibility_scope,
                resolved,
                version=version,
                start_line=start,
                end_line=end,
            )
            for start, end in normalized
        ]

        # The state cache stores the complete bytes for stale detection, but
        # mutation authority is limited to the exact visible line ranges.
        if not self.defer_delivery:
            self.cache.mark_read(
                str(resolved),
                raw,
                full_view=_blocks_cover_full_file(normalized, total_lines),
                covered_ranges=tuple(normalized),
            )

        if normalized and all(visible_flags) and not skipped:
            return _result(
                RuntimeStatus.OK,
                FILE_UNCHANGED_STUB,
                file_path=str(resolved),
                start_line=normalized[0][0],
                end_line=normalized[-1][1],
                total_lines=total_lines,
                truncated=False,
                full_view=_blocks_cover_full_file(normalized, total_lines),
                unchanged=True,
                encoding="utf-8",
                utf8_bom=utf8_bom,
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
                utf8_bom=False,
                content="",
                _context_delivery=manifest.to_dict(),
            )

        if not normalized:
            # Offset beyond file for every requested block.
            msg = f"(file has {total_lines} lines; every requested block starts beyond end of file)"
            structured = {
                "file_path": str(resolved),
                "start_line": offset,
                "end_line": total_lines,
                "total_lines": total_lines,
                "truncated": False,
                "full_view": False,
                "unchanged": False,
                "encoding": "utf-8",
                "utf8_bom": utf8_bom,
                "blocks": skipped,
            }
            return _result(RuntimeStatus.OK, msg, **structured)

        multi_block = _used_explicit_ranges(args)

        if not self.defer_delivery:
            for (start, end), already_visible in zip(normalized, visible_flags):
                if not already_visible:
                    self.visibility_cache.mark_visible(
                        self.visibility_scope,
                        resolved,
                        version=version,
                        start_line=start,
                        end_line=end,
                    )

        bom_notice = f"{UTF8_BOM_NOTICE}\n" if utf8_bom and normalized[0][0] == 1 else ""
        rendered_blocks: list[str] = []
        spans: list[FileDeliverySpan] = []
        blocks_summary: list[dict[str, Any]] = []
        cursor = len(bom_notice)
        for (start, end), already_visible in zip(normalized, visible_flags):
            if already_visible and multi_block:
                header = f"──── lines {start}-{end} unchanged; already delivered ────"
                rendered_blocks.append(header)
                blocks_summary.append(
                    {
                        "start_line": start,
                        "end_line": end,
                        "truncated": False,
                        "unchanged": True,
                    }
                )
                continue
            selected = lines[start - 1 : end]
            numbered: list[str] = []
            header = (
                f"──── lines {start}-{end} of {total_lines} ────"
                if multi_block
                else ""
            )
            # Spans must reference the rendered content, so the block header
            # (and its newline) is part of the offset math before line spans
            # are generated; otherwise manifest.slice() credits lines that the
            # result preview never actually showed.
            cursor = len(bom_notice) + (
                len("\n\n".join(rendered_blocks)) + 2 if rendered_blocks else 0
            ) + len(header) + (1 if header else 0)
            for i, line in enumerate(selected, start=start):
                display_line = line
                if i == 1 and display_line.startswith(UTF8_BOM):
                    display_line = display_line[len(UTF8_BOM) :]
                rendered_line = (
                    f"{i:>6}\t{display_line.rstrip(chr(10)).rstrip(chr(13))}"
                )
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
            block_text = "\n".join(numbered)
            remaining = total_lines - end
            block_truncated = end < total_lines
            if header:
                block_body = f"{header}\n{block_text}"
            else:
                block_body = block_text
            if block_truncated:
                block_body += f"\n\n... ({remaining} more lines below)"
            rendered_blocks.append(block_body)
            blocks_summary.append(
                {
                    "start_line": start,
                    "end_line": end,
                    "truncated": block_truncated,
                    "unchanged": False,
                }
            )

        content = bom_notice + "\n\n".join(rendered_blocks)
        if skipped:
            content += "\n\n" + "; ".join(
                f"(block starting at line {item['start_line']} is beyond end of file)"
                for item in skipped
            )
        blocks_summary.extend(skipped)

        structured = {
            "file_path": str(resolved),
            "start_line": normalized[0][0],
            "end_line": normalized[-1][1],
            "total_lines": total_lines,
            "truncated": any(item.get("truncated") for item in blocks_summary),
            "full_view": _blocks_cover_full_file(normalized, total_lines),
            "unchanged": False,
            "encoding": "utf-8",
            "utf8_bom": utf8_bom,
            "content": content,
        }
        if multi_block or len(normalized) > 1:
            structured["blocks"] = blocks_summary
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

    @staticmethod
    def _validate_requested_blocks(args: dict[str, Any]) -> str | None:
        """Return an error message when the ranges argument is malformed."""

        ranges = args.get("ranges")
        if ranges in (None, ""):
            return None
        if not isinstance(ranges, (list, tuple)) or not ranges:
            return "ranges must be a non-empty list of {offset, limit} blocks."
        for item in ranges:
            if not isinstance(item, Mapping) and not hasattr(item, "offset"):
                return "each ranges entry must be an object with offset and limit."
            block_offset = item.get("offset") if isinstance(item, Mapping) else getattr(item, "offset", None)
            block_limit = item.get("limit") if isinstance(item, Mapping) else getattr(item, "limit", None)
            if _positive_int(block_offset, default=1) is None or _positive_int(block_limit, default=DEFAULT_LIMIT) is None:
                return "each ranges entry needs positive integer offset and limit."
        return None


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


def _used_explicit_ranges(args: dict[str, Any]) -> bool:
    ranges = args.get("ranges")
    return bool(ranges) and isinstance(ranges, (list, tuple))


def _requested_blocks(
    args: dict[str, Any],
    default_offset: int,
    default_limit: int,
) -> tuple[tuple[int, int], ...]:
    """Resolve requested (offset, limit) blocks; ranges wins over offset/limit."""

    ranges = args.get("ranges")
    if not _used_explicit_ranges(args):
        _ = ranges
        return ((default_offset, default_limit),)
    blocks: list[tuple[int, int]] = []
    for item in ranges:
        if isinstance(item, Mapping):
            block_offset = _positive_int(item.get("offset"), default=1)
            block_limit = _positive_int(item.get("limit"), default=DEFAULT_LIMIT)
        else:
            block_offset = _positive_int(getattr(item, "offset", None), default=1)
            block_limit = _positive_int(getattr(item, "limit", None), default=DEFAULT_LIMIT)
        if block_offset is None or block_limit is None:
            continue
        blocks.append((block_offset, block_limit))
    return tuple(blocks) or ((default_offset, default_limit),)


def _blocks_cover_full_file(
    normalized: list[tuple[int, int]],
    total_lines: int,
) -> bool:
    if total_lines == 0:
        return True
    if not normalized:
        return False
    merged: list[tuple[int, int]] = []
    for start, end in sorted(normalized):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    # Full view requires one contiguous range with no interior holes.
    return len(merged) == 1 and merged[0][0] <= 1 and merged[0][1] >= total_lines


def _result(status: str, text: str, **structured: Any) -> CapabilityResult:
    delivery = structured.pop("_context_delivery", None)
    return CapabilityResult(
        status=status,
        llm_text=text,
        text=text,
        structured=structured,
        context_delivery=dict(delivery) if isinstance(delivery, dict) else None,
    )
