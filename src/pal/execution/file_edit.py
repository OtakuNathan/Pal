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
    FileStateCache,
    resolve_file_path,
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
    ERR_PARTIAL_READ: "Only part of the file was read. Read the complete file before editing.",
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
            if not cached_state.full_view:
                return _err(
                    RuntimeStatus.FORBIDDEN,
                    _ERROR_LLMS[ERR_PARTIAL_READ],
                    error_code=ERR_PARTIAL_READ,
                    file_path=file_path,
                )
            cached_content = cached_state.content

        try:
            resolved = resolve_file_path(file_path)
            current_content = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            return _err(
                RuntimeStatus.ERROR,
                f"failed to read file before editing: {exc}",
                error_code="READ_FAILED",
                file_path=file_path,
            )

        if current_content != cached_content:
            self.cache.invalidate(file_path)
            return _err(
                RuntimeStatus.FORBIDDEN,
                _ERROR_LLMS[ERR_STALE_FILE],
                error_code=ERR_STALE_FILE,
                file_path=file_path,
            )

        # 2. Find occurrences of old_string.
        actual_old_string = _find_actual_string(cached_content, old_string)
        if actual_old_string is None:
            return _err(
                RuntimeStatus.ERROR,
                _ERROR_LLMS[ERR_NOT_FOUND_MATCH],
                error_code=ERR_NOT_FOUND_MATCH,
                file_path=file_path,
            )
        count = cached_content.count(actual_old_string)

        if count > 1 and not replace_all:
            return _err(
                RuntimeStatus.ERROR,
                _ERROR_LLMS[ERR_MULTIPLE_MATCHES],
                error_code=ERR_MULTIPLE_MATCHES,
                file_path=file_path,
                match_count=count,
            )

        # 3. Apply replacement.
        replacement = _preserve_quote_style(old_string, actual_old_string, new_string)
        new_content = cached_content.replace(
            actual_old_string,
            replacement,
            count if replace_all else 1,
        )

        # 4. Write to disk.
        try:
            resolved.write_text(new_content, encoding="utf-8")
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

        return CapabilityResult(
            status=RuntimeStatus.OK,
            text=patch,
            llm_text=patch,
            structured={
                "file_path": str(resolved),
                "patch": patch,
                "match_count": count if replace_all else 1,
            },
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


_QUOTE_TRANSLATION = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def _find_actual_string(content: str, search: str) -> str | None:
    if search in content:
        return search
    normalized_content = content.translate(_QUOTE_TRANSLATION)
    normalized_search = search.translate(_QUOTE_TRANSLATION)
    index = normalized_content.find(normalized_search)
    if index < 0:
        return None
    return content[index : index + len(search)]


def _preserve_quote_style(old: str, actual_old: str, new: str) -> str:
    if old == actual_old:
        return new
    if "“" in actual_old or "”" in actual_old:
        new = _curl_quotes(new, straight='"', left="“", right="”")
    if "‘" in actual_old or "’" in actual_old:
        new = _curl_quotes(new, straight="'", left="‘", right="’")
    return new


def _curl_quotes(text: str, *, straight: str, left: str, right: str) -> str:
    output: list[str] = []
    opening_predecessors = "([{<"
    for index, character in enumerate(text):
        if character != straight:
            output.append(character)
            continue
        previous = text[index - 1] if index else ""
        is_opening = index == 0 or previous.isspace() or previous in opening_predecessors
        output.append(left if is_opening else right)
    return "".join(output)


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
