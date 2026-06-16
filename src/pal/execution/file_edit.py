"""Structured file edit tool modeled after Claude Code's FileEditTool.

Accepts ``file_path``, ``old_string``, and ``new_string`` arguments and
performs a precise in-place replacement.  Returns a unified diff patch on
success.

Safety guarantees (matching Claude Code):
- The file must have been previously registered via :class:`FileStateCache`
  (i.e. the caller has "read" it first).
- The on-disk mtime must still match the cached snapshot; otherwise a
  ``STALE_FILE`` error is returned.
- ``old_string`` must appear exactly once in the current content; otherwise
  ``MULTIPLE_MATCHES`` or ``NOT_FOUND_MATCH`` is returned.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.execution.file_state import FileStateCache
from pal.shared import RuntimeStatus


# Error codes
ERR_NOT_READ = "NOT_READ"
ERR_STALE_FILE = "STALE_FILE"
ERR_MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
ERR_NOT_FOUND_MATCH = "NOT_FOUND_MATCH"
ERR_EMPTY_OLD_STRING = "EMPTY_OLD_STRING"
ERR_NO_CHANGE = "NO_CHANGE"

# LLM-friendly short descriptions for each error code
_ERROR_LLMS: dict[str, str] = {
    ERR_NOT_READ: "File has not been read yet. Read it first before editing.",
    ERR_STALE_FILE: "File has been modified since read. Read it again before editing.",
    ERR_MULTIPLE_MATCHES: "old_string appears multiple times in the file. Provide more context to uniquely identify the match.",
    ERR_NOT_FOUND_MATCH: "old_string was not found in the file.",
    ERR_EMPTY_OLD_STRING: "old_string must not be empty.",
    ERR_NO_CHANGE: "new_string must be different from old_string.",
}


@dataclass
class FileEditTool:
    """File edit tool implementing the :class:`~pal.execution.contracts.Tool` protocol.

    Parameters
    ----------
    cache:
        Shared :class:`FileStateCache` instance.  The cache must be populated
        externally (e.g. by a file-read tool) *before* ``invoke`` is called.
    """

    name: str = "op_file_edit"
    display_name: str = "File Edit"
    family: str = "system"
    description: str = (
        "Edit a file by replacing an exact old_string with new_string. "
        "The file must have been read first (its content cached). "
        "Returns a unified diff patch on success."
    )
    tags: tuple[str, ...] = ("file", "edit", "system", "write")
    keywords: tuple[str, ...] = ("edit", "modify", "replace", "patch", "diff", "file")
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
                        "description": "Path to the file to edit.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to find and replace in the file.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            }
        if not self.result_schema:
            self.result_schema = {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "error_code": {"type": "string"},
                    "patch": {"type": "string"},
                    "match_count": {"type": "integer"},
                },
            }

    # ------------------------------------------------------------------
    # Tool protocol
    # ------------------------------------------------------------------

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        file_path = str(args.get("file_path") or "").strip()
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")

        if not file_path:
            return _err(RuntimeStatus.INVALID, "file_path is required", reason="missing_file_path")

        if not isinstance(old_string, str):
            return _err(RuntimeStatus.INVALID, "old_string must be a string", reason="bad_old_string")

        if not isinstance(new_string, str):
            return _err(RuntimeStatus.INVALID, "new_string must be a string", reason="bad_new_string")

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
        had_record = _resolve(file_path) in self.cache
        cached_content = self.cache.get_valid(file_path)
        if cached_content is None:
            if not had_record:
                return _err(
                    RuntimeStatus.FORBIDDEN,
                    _ERROR_LLMS[ERR_NOT_READ],
                    error_code=ERR_NOT_READ,
                    file_path=file_path,
                )
            return _err(
                RuntimeStatus.FORBIDDEN,
                _ERROR_LLMS[ERR_STALE_FILE],
                error_code=ERR_STALE_FILE,
                file_path=file_path,
            )

        try:
            current_content = Path(file_path).expanduser().resolve().read_text(encoding="utf-8")
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
        count = cached_content.count(old_string)
        if count == 0:
            return _err(
                RuntimeStatus.ERROR,
                _ERROR_LLMS[ERR_NOT_FOUND_MATCH],
                error_code=ERR_NOT_FOUND_MATCH,
                file_path=file_path,
            )

        if count > 1:
            return _err(
                RuntimeStatus.ERROR,
                _ERROR_LLMS[ERR_MULTIPLE_MATCHES],
                error_code=ERR_MULTIPLE_MATCHES,
                file_path=file_path,
                match_count=count,
            )

        # 3. Apply replacement.
        new_content = cached_content.replace(old_string, new_string, 1)

        # 4. Write to disk.
        try:
            path = Path(file_path).expanduser().resolve()
            path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return _err(
                RuntimeStatus.ERROR,
                f"failed to write file: {exc}",
                error_code="WRITE_FAILED",
                file_path=file_path,
            )

        # 5. Update cache with new content/mtime so subsequent edits work.
        self.cache.mark_read(file_path, new_content)

        # 6. Generate unified diff patch.
        patch = _unified_diff(file_path, cached_content, new_content)

        return CapabilityResult(
            status=RuntimeStatus.OK,
            text=patch,
            llm_text=patch,
            structured={
                "file_path": file_path,
                "patch": patch,
                "match_count": 1,
            },
        )

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        # File I/O is fast enough to run synchronously; delegate to invoke.
        _ = kwargs
        return self.invoke(args)


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _resolve(file_path: str) -> str:
    try:
        return str(Path(file_path).resolve())
    except (OSError, ValueError):
        return file_path


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
