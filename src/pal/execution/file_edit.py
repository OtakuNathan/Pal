"""Structured file edit tool modeled after Claude Code's FileEditTool.

Accepts ``file_path``, ``old_string``, and ``new_string`` arguments and
performs a precise in-place replacement.  Returns a unified diff patch on
success.

Safety guarantees (matching Claude Code):
- The file must have been previously registered via :class:`FileStateCache`
  (i.e. the caller has "read" it first).
- The on-disk mtime must still match the cached snapshot; otherwise a
  ``STALE_FILE`` error is returned.
- ``old_string`` must appear exactly once unless ``replace_all=true``;
  otherwise ``MULTIPLE_MATCHES`` or ``NOT_FOUND_MATCH`` is returned.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.execution.file_state import (
    FileContentChangedError,
    FileStateCache,
    atomic_compare_and_swap_utf8,
    line_number_at_offset,
    line_range_for_offsets,
    resolve_file_path,
    text_line_starts,
)
from pal.execution.session_state import (
    FileDeliveryManifest,
    FileDeliverySpan,
    content_digest,
    count_text_lines,
)
from pal.shared import RuntimeStatus


# Error codes
ERR_NOT_READ = "NOT_READ"
ERR_PARTIAL_READ = "PARTIAL_READ"
ERR_STALE_FILE = "STALE_FILE"
ERR_MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
ERR_NOT_FOUND_MATCH = "NOT_FOUND_MATCH"
ERR_EMPTY_OLD_STRING = "EMPTY_OLD_STRING"
ERR_NO_CHANGE = "NO_CHANGE"

# LLM-friendly short descriptions for each error code
_ERROR_LLMS: dict[str, str] = {
    ERR_NOT_READ: "File has not been read yet. Read it first before editing.",
    ERR_PARTIAL_READ: "The requested edit is outside the line ranges already read. Read the exact affected range before editing.",
    ERR_STALE_FILE: "File has been modified since read. Read it again before editing.",
    ERR_MULTIPLE_MATCHES: "old_string appears multiple times in the file. Provide more context to uniquely identify the match.",
    ERR_NOT_FOUND_MATCH: "old_string was not found in the file.",
    ERR_EMPTY_OLD_STRING: "old_string must not be empty.",
    ERR_NO_CHANGE: "new_string must be different from old_string.",
}


@dataclass
class FileEditTool:
    """Business handler for read-before-edit file mutations.

    Parameters
    ----------
    cache:
        Shared :class:`FileStateCache` instance.  The cache must be populated
        externally (e.g. by a file-read tool) *before* ``invoke`` is called.
    """

    cache: FileStateCache = field(default_factory=FileStateCache)

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        file_path = str(args.get("file_path") or "").strip()
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        replace_all = _semantic_bool(args.get("replace_all", False))

        if not file_path:
            return _err(RuntimeStatus.INVALID, "file_path is required", reason="missing_file_path")

        if not isinstance(old_string, str):
            return _err(RuntimeStatus.INVALID, "old_string must be a string", reason="bad_old_string")

        if not isinstance(new_string, str):
            return _err(RuntimeStatus.INVALID, "new_string must be a string", reason="bad_new_string")

        if replace_all is None:
            return _err(RuntimeStatus.INVALID, "replace_all must be a boolean", reason="bad_replace_all")

        if old_string == "":
            return _err(
                RuntimeStatus.INVALID,
                _ERROR_LLMS[ERR_EMPTY_OLD_STRING],
                error_code=ERR_EMPTY_OLD_STRING,
                file_path=file_path,
            )

        if old_string == new_string:
            return _err(
                RuntimeStatus.INVALID,
                _ERROR_LLMS[ERR_NO_CHANGE],
                error_code=ERR_NO_CHANGE,
                file_path=file_path,
            )

        # 1. Check that file has been read (cached).
        # Capture presence before get_valid(), because stale entries are
        # evicted as a side effect of validation.
        had_record = file_path in self.cache
        cached_state = self.cache.get_valid_state(file_path)
        if cached_state is None:
            if not had_record:
                return _err(
                    RuntimeStatus.FORBIDDEN,
                    _ERROR_LLMS[ERR_NOT_READ],
                    error_code=ERR_NOT_READ,
                    file_path=file_path,
                )
            else:
                return _err(
                    RuntimeStatus.FORBIDDEN,
                    _ERROR_LLMS[ERR_STALE_FILE],
                    error_code=ERR_STALE_FILE,
                    file_path=file_path,
                )
        else:
            cached_content = cached_state.content

        try:
            resolved = resolve_file_path(file_path)
        except OSError as exc:
            return _err(
                RuntimeStatus.ERROR,
                f"failed to resolve file before editing: {exc}",
                error_code="READ_FAILED",
                file_path=file_path,
            )

        # 2. Find occurrences of old_string.
        if old_string not in cached_content:
            return _err(
                RuntimeStatus.ERROR,
                _ERROR_LLMS[ERR_NOT_FOUND_MATCH],
                error_code=ERR_NOT_FOUND_MATCH,
                file_path=file_path,
            )
        count = cached_content.count(old_string)

        if count > 1 and not replace_all:
            return _err(
                RuntimeStatus.ERROR,
                _ERROR_LLMS[ERR_MULTIPLE_MATCHES],
                error_code=ERR_MULTIPLE_MATCHES,
                file_path=file_path,
                match_count=count,
            )

        match_offsets = _match_offsets(cached_content, old_string)
        required_line_ranges = tuple(
            _line_range_for_match(cached_content, start, end)
            for start, end in match_offsets
        )
        if not all(
            cached_state.covers_lines(start_line, end_line)
            for start_line, end_line in required_line_ranges
        ):
            return _err(
                RuntimeStatus.FORBIDDEN,
                _ERROR_LLMS[ERR_PARTIAL_READ],
                error_code=ERR_PARTIAL_READ,
                file_path=file_path,
                required_line_ranges=[list(item) for item in required_line_ranges],
                covered_line_ranges=[list(item) for item in cached_state.covered_ranges],
            )

        # 3. Apply replacement.
        replacement = new_string
        new_content = cached_content.replace(
            old_string,
            replacement,
            count if replace_all else 1,
        )

        # 4. Commit only if the exact authorized pre-image is still current.
        try:
            atomic_compare_and_swap_utf8(
                resolved,
                expected_content=cached_content,
                new_content=new_content,
            )
        except FileContentChangedError:
            self.cache.invalidate(file_path)
            return _err(
                RuntimeStatus.FORBIDDEN,
                _ERROR_LLMS[ERR_STALE_FILE],
                error_code=ERR_STALE_FILE,
                file_path=file_path,
            )
        except OSError as exc:
            return _err(
                RuntimeStatus.ERROR,
                f"failed to write file: {exc}",
                error_code="WRITE_FAILED",
                file_path=file_path,
            )

        # 5. Update cache with new content/mtime so subsequent edits work.
        self.cache.mark_read(resolved, new_content)

        # 6. Generate unified diff patch.
        patch = _unified_diff(str(resolved), cached_content, new_content)
        standalone_ranges, inherited_ranges = _post_edit_authority(
            old_content=cached_content,
            new_content=new_content,
            match_offsets=match_offsets,
            replacement=replacement,
            covered_ranges=(
                ((1, count_text_lines(cached_content)),)
                if cached_state.full_view and count_text_lines(cached_content) > 0
                else cached_state.covered_ranges
            ),
        )
        proof_length = max(1, len(patch))
        manifest = FileDeliveryManifest(
            file_key=str(resolved),
            digest=content_digest(new_content),
            total_lines=count_text_lines(new_content),
            spans=tuple(
                FileDeliverySpan(
                    start_offset=0,
                    end_offset=proof_length,
                    start_line=start_line,
                    end_line=end_line,
                    visible_start_in_line=0,
                    visible_end_in_line=proof_length,
                    line_length=proof_length,
                )
                for start_line, end_line in standalone_ranges
            ),
            empty_file=(new_content == ""),
            operation="edit",
            before_digest=content_digest(cached_content),
            inherited_ranges=inherited_ranges,
            parent_result_ids=cached_state.authority_result_ids,
        )

        return CapabilityResult(
            status=RuntimeStatus.OK,
            text=patch,
            llm_text=patch,
            structured={
                "file_path": str(resolved),
                "patch": patch,
                "match_count": count if replace_all else 1,
            },
            context_delivery=manifest.to_dict(),
        )

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        # File I/O is fast enough to run synchronously; delegate to invoke.
        _ = kwargs
        return self.invoke(args)


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _semantic_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def _match_offsets(content: str, search: str) -> tuple[tuple[int, int], ...]:
    matches: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = content.find(search, cursor)
        if start < 0:
            return tuple(matches)
        end = start + len(search)
        matches.append((start, end))
        cursor = end


def _line_range_for_match(
    content: str,
    start_offset: int,
    end_offset: int,
) -> tuple[int, int]:
    return line_range_for_offsets(content, start_offset, end_offset)


def _post_edit_authority(
    *,
    old_content: str,
    new_content: str,
    match_offsets: tuple[tuple[int, int], ...],
    replacement: str,
    covered_ranges: tuple[tuple[int, int], ...],
) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    """Return exact post-image ranges and transformed unchanged inheritance."""

    old_affected = tuple(
        line_range_for_offsets(old_content, start, end)
        for start, end in match_offsets
    )
    new_affected: list[tuple[int, int]] = []
    delta = 0
    deltas: list[tuple[int, int]] = []
    for start, end in match_offsets:
        new_start = start + delta
        new_end = new_start + len(replacement)
        new_affected.append(line_range_for_offsets(new_content, new_start, new_end))
        delta_change = len(replacement) - (end - start)
        delta += delta_change
        deltas.append((end, delta_change))

    inherited: list[tuple[int, int]] = []
    old_starts = text_line_starts(old_content)
    for fragment_start, fragment_end in _subtract_line_ranges(
        covered_ranges,
        old_affected,
    ):
        if not old_starts or fragment_start > len(old_starts):
            continue
        old_offset = old_starts[fragment_start - 1]
        shifted_offset = old_offset + sum(
            change for match_end, change in deltas if match_end <= old_offset
        )
        new_start_line = line_number_at_offset(new_content, shifted_offset)
        inherited.append(
            (new_start_line, new_start_line + (fragment_end - fragment_start))
        )
    return _merge_ranges(new_affected), _merge_ranges(inherited)


def _subtract_line_ranges(
    sources: tuple[tuple[int, int], ...],
    removed: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    remaining: list[tuple[int, int]] = []
    for source_start, source_end in _merge_ranges(sources):
        cursor = source_start
        for remove_start, remove_end in _merge_ranges(removed):
            if remove_end < cursor or remove_start > source_end:
                continue
            if remove_start > cursor:
                remaining.append((cursor, min(source_end, remove_start - 1)))
            cursor = max(cursor, remove_end + 1)
            if cursor > source_end:
                break
        if cursor <= source_end:
            remaining.append((cursor, source_end))
    return tuple(remaining)


def _merge_ranges(
    ranges: tuple[tuple[int, int], ...] | list[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(
        (max(1, int(start)), max(1, int(end)))
        for start, end in ranges
        if int(end) >= int(start)
    ):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)


def _unified_diff(file_path: str, old: str, new: str, context: int = 3) -> str:
    """Return a unified diff string between *old* and *new* content."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile=file_path, tofile=file_path, n=context)
    return "".join(diff)


def _err(status: str, text: str, *, reason: str = "", **structured: Any) -> CapabilityResult:
    payload: dict[str, Any] = {"reason": reason} if reason else {}
    payload.update(structured)
    return CapabilityResult(status=status, text=text, llm_text=text, structured=payload)
