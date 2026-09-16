"""Apply valid exact replacements against one authorized local UTF-8 snapshot.

Every edit matches the original content. All matches must have been read and
must remain current through the final content comparison and atomic write.
"""

from __future__ import annotations

import difflib
import heapq
import json
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
        edits = args.get("edits")
        if not file_path:
            return _err(RuntimeStatus.INVALID, "file_path is required", reason="missing_file_path")
        if set(args) - {"file_path", "edits"} or not isinstance(edits, list) or not edits:
            return _err(RuntimeStatus.INVALID, "Provide file_path and a nonempty edits array.", reason="bad_edits")
        failures: dict[int, CapabilityResult] = {}
        for index, edit in enumerate(edits):
            invalid = _validate_edit(edit)
            if invalid is not None:
                failures[index] = _err(RuntimeStatus.INVALID, invalid[1], error_code=invalid[0])
        if len(failures) == len(edits):
            return _batch_error(file_path, failures)

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

        # Resolve every replacement against the same pre-image before writing.
        replacements = []
        for index, edit in enumerate(edits):
            if index in failures:
                continue
            matches = _plan_exact_edit(cached_content, cached_state, edit)
            if isinstance(matches, CapabilityResult):
                failures[index] = matches
                continue
            replacements.extend((start, end, edit["new_string"], index) for start, end in matches)
        replacements.sort(key=lambda item: (item[0], item[1]))
        for index, conflicts in _overlapping_edits(replacements).items():
            failures[index] = _err(RuntimeStatus.INVALID, "Overlapping edits were not applied.",
                error_code="OVERLAPPING_EDITS", conflicting_edit_indices=sorted(conflicts),
                conflicting_edit_index=min(conflicts))
        replacements = [item for item in replacements if item[3] not in failures]
        if not replacements:
            return _batch_error(file_path, failures)
        chunks, cursor = [], 0
        for start, end, replacement, _ in replacements:
            chunks.extend((cached_content[cursor:start], replacement))
            cursor = end
        chunks.append(cached_content[cursor:])
        new_content = "".join(chunks)
        if new_content == cached_content:
            for index in {item[3] for item in replacements}:
                failures[index] = _err(RuntimeStatus.INVALID, "These edits together make no change.", error_code=ERR_NO_CHANGE)
            return _batch_error(file_path, failures)

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
            self.cache.invalidate(file_path)
            return _err(
                RuntimeStatus.ERROR,
                f"File write reported an error: {exc}. Commit outcome is uncertain; read the current file before retrying.",
                error_code="WRITE_FAILED",
                file_path=file_path,
            )

        # 5. Record the new content; delivery below determines read authority.
        self.cache.mark_read(resolved, new_content)

        # 6. Generate unified diff patch.
        patch = _unified_diff(str(resolved), cached_content, new_content)
        applied = sorted({item[3] for item in replacements})
        failed = _failure_details(failures)
        for item in failed:
            # Locations from validation refer to the pre-image. Supply current
            # match locations after successful edits have shifted the file.
            for key in ("required_line_ranges", "covered_line_ranges", "match_count"):
                if key in item:
                    item["original_" + key] = item.pop(key)
            edit = edits[item["edit_index"]]
            if isinstance(edit, dict) and isinstance(edit.get("old_string"), str) and edit["old_string"]:
                item["current_match_line_ranges"] = [list(_line_range_for_match(new_content, start, end))
                    for start, end in _match_offsets(new_content, edit["old_string"])]
        report = json.dumps({"applied_edit_indices": applied, "failed_edits": failed}, ensure_ascii=False) + "\n"
        llm_text = report + patch
        standalone_ranges, inherited_ranges = _post_edit_authority(
            old_content=cached_content,
            new_content=new_content,
            replacements=tuple((start, end, text) for start, end, text, _ in replacements),
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
                    start_offset=len(report),
                    end_offset=len(report) + proof_length,
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
            text=llm_text,
            llm_text=llm_text,
            structured={
                "file_path": str(resolved),
                "patch": patch,
                "match_count": len(replacements),
                "edit_count": len(applied),
                "applied_edit_indices": applied,
                "failed_edits": failed,
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


def _overlapping_edits(replacements) -> dict[int, set[int]]:
    active, owners, conflicts = [], {}, {}
    for start, end, _, index in replacements:
        while active and active[0][0] <= start:
            _, owner = heapq.heappop(active)
            owners[owner] -= 1
            if not owners[owner]:
                del owners[owner]
        for owner in owners:
            conflicts.setdefault(index, set()).add(owner)
            conflicts.setdefault(owner, set()).add(index)
        heapq.heappush(active, (end, index))
        owners[index] = owners.get(index, 0) + 1
    return conflicts


def _failure_details(failures: dict[int, CapabilityResult]) -> list[dict[str, Any]]:
    return [{**{key: value for key, value in result.structured.items() if key != "applied_edit_indices"},
             "edit_index": index, "message": result.llm_text}
            for index, result in sorted(failures.items())]


def _batch_error(file_path: str, failures: dict[int, CapabilityResult]) -> CapabilityResult:
    first_index = min(failures)
    first = failures[first_index]
    details = _failure_details(failures)
    return _err(first.status, "No edits applied.\n" + json.dumps(details, ensure_ascii=False),
                **{**dict(first.structured), "file_path": file_path, "edit_index": first_index,
                   "applied_edit_indices": [], "failed_edits": details})


def _validate_edit(edit: Any) -> tuple[str, str] | None:
    if not isinstance(edit, dict) or set(edit) - {"old_string", "new_string", "replace_all"}:
        return "INVALID_EDIT", "Expected old_string, new_string and optional replace_all."
    if not isinstance(edit.get("old_string"), str):
        return "INVALID_EDIT", "old_string must be a string"
    if not isinstance(edit.get("new_string"), str):
        return "INVALID_EDIT", "new_string must be a string"
    if not isinstance(edit.get("replace_all", False), bool):
        return "INVALID_EDIT", "replace_all must be a boolean"
    if not edit["old_string"]:
        return ERR_EMPTY_OLD_STRING, _ERROR_LLMS[ERR_EMPTY_OLD_STRING]
    if edit["old_string"] == edit["new_string"]:
        return ERR_NO_CHANGE, _ERROR_LLMS[ERR_NO_CHANGE]
    return None


def _plan_exact_edit(content, state, edit):
    """Internal single-edit planner. Never mutates a file or its read authority."""
    matches = _match_offsets(content, edit["old_string"])
    if not matches:
        return _err(RuntimeStatus.ERROR, _ERROR_LLMS[ERR_NOT_FOUND_MATCH], error_code=ERR_NOT_FOUND_MATCH)
    if len(matches) > 1 and not edit.get("replace_all", False):
        return _err(RuntimeStatus.ERROR, _ERROR_LLMS[ERR_MULTIPLE_MATCHES],
                    error_code=ERR_MULTIPLE_MATCHES, match_count=len(matches))
    ranges = tuple(_line_range_for_match(content, start, end) for start, end in matches)
    if not all(state.covers_lines(start, end) for start, end in ranges):
        return _err(RuntimeStatus.FORBIDDEN, _ERROR_LLMS[ERR_PARTIAL_READ], error_code=ERR_PARTIAL_READ,
                    required_line_ranges=[list(item) for item in ranges],
                    covered_line_ranges=[list(item) for item in state.covered_ranges])
    return matches


def _match_offsets(content: str, search: str) -> tuple[tuple[int, int], ...]:
    matches: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = content.find(search, cursor)
        if start < 0:
            return tuple(matches)
        end = start + len(search)
        matches.append((start, end))
        cursor = start + 1


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
    replacements: tuple[tuple[int, int, str], ...],
    covered_ranges: tuple[tuple[int, int], ...],
) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    """Return exact post-image ranges and transformed unchanged inheritance."""

    old_affected = tuple(
        line_range_for_offsets(old_content, start, end)
        for start, end, _ in replacements
    )
    new_affected: list[tuple[int, int]] = []
    delta = 0
    deltas: list[tuple[int, int]] = []
    for start, end, replacement in replacements:
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
    # A durability error can occur after os.replace has already committed.
    if payload.get("error_code") != "WRITE_FAILED":
        payload.setdefault("applied_edit_indices", [])
    return CapabilityResult(status=status, text=text, llm_text=text, structured=payload)
