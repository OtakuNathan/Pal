"""Low-friction whole-file create-or-overwrite tool for UTF-8 text files."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EffectOutcome, EffectReceipt
from pal.execution.file_state import (
    FileContentChangedError,
    FileSnapshotReadError,
    FileState,
    FileStateCache,
    atomic_compare_and_swap_utf8,
    resolve_file_path,
)
from pal.execution.session_state import (
    FileDeliveryManifest,
    FileDeliverySpan,
    content_digest,
    count_text_lines,
)
from pal.shared import RuntimeStatus
from pal.shared.diagnostics import diagnostic_text, exception_report


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
ERR_WRITE_FAILED = "WRITE_FAILED"
ERR_READ_FAILED = "READ_FAILED"

_ERROR_LLMS: dict[str, str] = {
    ERR_FILE_NOT_FOUND: "The specified file does not exist.",
    ERR_MISSING_CONTENT: "content is required and must be a string.",
    ERR_MISSING_FILE_PATH: "file_path is required.",
    ERR_NOT_A_FILE: "The specified path is not a regular file.",
    ERR_NOT_READ: "File has not been read yet. Read the complete file first before overwriting it.",
    ERR_PARTIAL_READ: "Only part of the file was read. Read the complete file before overwriting it.",
    ERR_PARENT_DIR_NOT_FOUND: "The parent directory does not exist.",
    ERR_PARENT_NOT_DIRECTORY: "The parent path exists but is not a directory.",
    ERR_STALE_FILE: "The current file version could not be confirmed against the read snapshot. Read it again before writing.",
    ERR_BINARY_CONTENT: "Content contains binary data (NUL bytes). Only UTF-8 text is supported.",
    ERR_CONTENT_TOO_LARGE: f"Content exceeds the maximum allowed size of {MAX_CONTENT_BYTES} bytes.",
    ERR_WRITE_FAILED: "Failed to write file.",
    ERR_READ_FAILED: "Could not read the file before writing.",
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
            return _err(RuntimeStatus.INVALID, ERR_WRITE_FAILED, file_path=file_path, details=exception_report(exc))

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
        parent_creation_attempted = not parent.exists()
        if parent_creation_attempted:
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return _err(RuntimeStatus.ERROR, ERR_WRITE_FAILED, file_path=str(resolved), details=exception_report(exc))
        if not parent.is_dir():
            return _err(RuntimeStatus.INVALID, ERR_PARENT_NOT_DIRECTORY, file_path=str(resolved),
                        effect_receipt=_parent_creation_receipt(parent, parent_creation_attempted))

        try:
            atomic_compare_and_swap_utf8(
                resolved,
                expected_content=None,
                new_content=content,
                create_parents=True,
            )
        except FileContentChangedError as exc:
            return _err(RuntimeStatus.FORBIDDEN, ERR_STALE_FILE, file_path=str(resolved),
                        details=exception_report(exc),
                        effect_receipt=_parent_creation_receipt(parent, parent_creation_attempted))
        except OSError as exc:
            return _err(RuntimeStatus.ERROR, ERR_WRITE_FAILED, file_path=str(resolved), details=exception_report(exc), commit_uncertain=True)

        self.cache.mark_read(resolved, content)
        return _ok(
            resolved,
            content,
            old_content="",
            created=True,
            parent_result_ids=(),
        )

    def _overwrite(self, resolved: Path, content: str) -> CapabilityResult:
        cached = self._require_current_snapshot(resolved)
        if isinstance(cached, CapabilityResult):
            return cached

        try:
            atomic_compare_and_swap_utf8(
                resolved,
                expected_content=cached.content,
                new_content=content,
            )
        except FileContentChangedError as exc:
            self.cache.invalidate(resolved)
            return _err(RuntimeStatus.FORBIDDEN, ERR_STALE_FILE, file_path=str(resolved), details=exception_report(exc))
        except OSError as exc:
            return _err(RuntimeStatus.ERROR, ERR_WRITE_FAILED, file_path=str(resolved), details=exception_report(exc), commit_uncertain=True)

        self.cache.mark_read(resolved, content)
        return _ok(
            resolved,
            content,
            old_content=cached.content,
            created=False,
            parent_result_ids=cached.authority_result_ids,
        )

    def _require_current_snapshot(self, resolved: Path) -> FileState | CapabilityResult:
        if not resolved.exists():
            return _err(RuntimeStatus.ERROR, ERR_FILE_NOT_FOUND, file_path=str(resolved))
        if not resolved.is_file():
            return _err(RuntimeStatus.ERROR, ERR_NOT_A_FILE, file_path=str(resolved))

        had_record = resolved in self.cache
        try:
            cached_state = self.cache.get_valid_state(resolved)
        except FileSnapshotReadError as exc:
            return _err(RuntimeStatus.ERROR, ERR_READ_FAILED, file_path=str(resolved), details=exception_report(exc))
        if cached_state is None:
            if not had_record:
                return _err(RuntimeStatus.FORBIDDEN, ERR_NOT_READ, file_path=str(resolved))
            return _err(RuntimeStatus.FORBIDDEN, ERR_STALE_FILE, file_path=str(resolved))
        if not cached_state.full_view:
            return _err(RuntimeStatus.FORBIDDEN, ERR_PARTIAL_READ, file_path=str(resolved))
        return cached_state


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


def _parent_creation_receipt(parent: Path, attempted: bool) -> EffectReceipt | None:
    return (EffectReceipt(outcome=EffectOutcome.UNKNOWN, receipt={"parent_creation_attempted": str(parent)})
            if attempted else None)


def _err(status: str, error_code: str, *, effect_receipt: EffectReceipt | None = None, **structured: Any) -> CapabilityResult:
    payload: dict[str, Any] = {"error_code": error_code}
    payload.update(structured)
    text = _ERROR_LLMS[error_code]
    if structured.get("details"):
        text += "\nCause: " + diagnostic_text(structured["details"], limit=None)
    if structured.get("commit_uncertain"):
        text += "\nThe replacement may have committed before persistence failed. Read the current file before retrying."
    return CapabilityResult(status=status, text=text, llm_text=text, structured=payload, effect_receipt=effect_receipt)


def _ok(
    resolved: Path,
    content: str,
    *,
    old_content: str,
    created: bool,
    parent_result_ids: tuple[str, ...],
) -> CapabilityResult:
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
    total_lines = count_text_lines(content)
    proof_length = max(1, len(text))
    manifest = FileDeliveryManifest(
        file_key=str(resolved),
        digest=content_digest(content),
        total_lines=total_lines,
        spans=(
            (
                FileDeliverySpan(
                    start_offset=0,
                    end_offset=proof_length,
                    start_line=1,
                    end_line=total_lines,
                    visible_start_in_line=0,
                    visible_end_in_line=proof_length,
                    line_length=proof_length,
                ),
            )
            if total_lines > 0
            else ()
        ),
        empty_file=(content == ""),
        operation="write",
        before_digest=("" if created else content_digest(old_content)),
        parent_result_ids=parent_result_ids,
        complete_file=True,
    )
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
        context_delivery=manifest.to_dict(),
    )
