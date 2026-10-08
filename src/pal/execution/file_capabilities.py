from __future__ import annotations

from dataclasses import replace

from uuid import uuid4

from pal.execution.generated_tool_models import (
    ExecutionFileCapabilitiesFileCapabilityMixinDeleteInput,
    ExecutionFileCapabilitiesFileCapabilityMixinDeleteOutput,
    ExecutionFileCapabilitiesFileCapabilityMixinEditInput,
    ExecutionFileCapabilitiesFileCapabilityMixinEditOutput,
    ExecutionFileCapabilitiesFileCapabilityMixinReadInput,
    ExecutionFileCapabilitiesFileCapabilityMixinReadOutput,
    ExecutionFileCapabilitiesFileCapabilityMixinStateInput,
    ExecutionFileCapabilitiesFileCapabilityMixinStateOutput,
    ExecutionFileCapabilitiesFileCapabilityMixinWriteInput,
    ExecutionFileCapabilitiesFileCapabilityMixinWriteOutput,
)
from pal.execution.tool_facade import NextToolHint, ToolGuidance

from pal.execution.file_edit import FileEditTool
from pal.execution.file_read import (
    FileReadTool,
    SessionFileVisibilityCache,
)
from pal.execution.file_state import (
    FileStateCache,
    FileStateTool,
    SessionFileStateCache,
)
from pal.execution.file_write import FileWriteTool
from pal.execution.path_delete import PathDeleteTool
from pal.execution.tool_facade import EffectOutcome, EffectReceipt, ToolRejectedError
from pal.execution.tool_semantics import (
    DIRECT_LOCAL_READ,
    DIRECT_LOCAL_WRITE,
    DIRECT_UNSAFE_LOCAL_WRITE,
    INDIRECT_LOCAL_READ,
    INDIRECT_LOCAL_WRITE,
)
from pal.shared import OPERATION_NAMESPACE, IntrospectionCall, IntrospectionResult, RuntimeStatus, capability_action


FILE_READ_GUIDANCE = ToolGuidance(
    purpose=(
        "Read selected lines from one UTF-8 text file. "
        "ranges=[{offset, limit}, ...] delivers multiple blocks in one call (sed -n style). "
        "When an unchanged marker is returned, reuse the earlier result; "
        "re-read only if the file changed."
    ),
    use_when=(
        "Reading local source, configuration, immutable output snapshots, or an artifact's supplied text_file.file_path. Use ranges for multiple "
        "blocks of the same file (for example scattered definitions found by rg) instead of "
        "several single-range calls; each block renders with its own header and truncation note. "
        "Search locates relevant code; before using edit_file, use read_file to deliver the affected ranges. "
        "Reuse valid delivered reads. Shell output and reads of output snapshots do not authorize edits to the original file."
    ),
    do_not_use_when=(
        "Binary files, images, or raw PDFs/audio. Use their extracted text_file path when supplied. Reading several different "
        "files at once (read_file reads one file per call; batch ranges apply within one file). "
        "offset and limit here count lines, not characters. "
        "Do not re-read an unchanged covered block after read_file returns an unchanged marker; "
        "use the earlier result unless the file changed or another range is needed."
    ),
    failure_next_steps=(
        "For READ_FAILED, check the path's permissions or existence with run_shell before retrying."
    ),
    next_tool_hints=(
        NextToolHint(
            name="edit_file",
            use_when="The affected source-file lines were delivered by read_file and a focused exact replacement is required; an output snapshot is not the source file.",
        ),
        NextToolHint(
            name="write_file",
            use_when="A new text file is needed, or a fully-read existing file must be replaced completely.",
        ),
    ),
)

FILE_EDIT_GUIDANCE = ToolGuidance(
    purpose="Apply valid exact replacements to one local UTF-8 file and report any failed items.",
    use_when=(
        "Making a focused change to an existing text file whose affected lines were delivered by read_file and remain in the current logical "
        "context. Supply edits=[{old_string, new_string, replace_all?}]. All items match the original read snapshot; "
        "Overlapping items fail together; other valid items are applied. Each match must be unique unless that item requests replace_all=true. "
        "Every affected range must have been delivered and remain valid; reading the entire file is not required. "
        "Results identify applied indices and failed items; only failed items need further attention. "
        "Use separate calls for separate files."
    ),
    do_not_use_when=(
        "Creating a file or replacing its complete contents (use write_file)."
    ),
    failure_next_steps=(
        "For WRITE_FAILED the commit outcome is uncertain: read the current file before deciding whether to retry. "
        "Do not resubmit applied items."
    ),
)

FILE_WRITE_GUIDANCE = ToolGuidance(
    purpose="Write complete UTF-8 text content to a local file on the Pal host, creating it or replacing all of its contents.",
    use_when=(
        "Creating a text file, or intentionally replacing an existing file's complete contents after its complete "
        "current version has been read. Missing parent directories are created. Paths are local to the Pal host."
    ),
    do_not_use_when=(
        "Focused changes to an existing file (use edit_file). Do not overwrite an existing file from a partial, "
        "retired, or stale read snapshot."
    ),
    failure_next_steps=(
        "For WRITE_FAILED the commit outcome is uncertain: read the current file before deciding whether to retry."
    ),
)

# OS failures may follow a partial commit. Explicit receipts take precedence
# for paths that already attempted another effect, such as parent creation.
_EFFECT_UNCERTAIN_CODES = frozenset({"WRITE_FAILED", "DELETE_FAILED"})

_READ_RECOVERY_HINTS = {
    "FILE_NOT_FOUND": "Correct the path; use run_shell with rg --files or a bounded listing to locate the file.",
    "NOT_A_FILE": "The path is not a regular file; correct the path or list the directory with run_shell.",
    "INVALID_ARGUMENT": "offset and limit (including every ranges entry) must be positive integers.",
    "UNSUPPORTED_TEXT_ENCODING": "The file is not UTF-8 text; do not retry read_file. Use a supplied text_file path or a binary-aware workflow.",
}
_EDIT_RECOVERY_HINTS = {
    "READ_FAILED": "Inspect the path and permissions described in the error before retrying; no edits were applied.",
    "NOT_READ": "Read the affected lines with read_file, then retry the edits.",
    "PARTIAL_READ": "Read the line ranges listed in required_line_ranges, then retry the failed edits.",
    "STALE_FILE": "The read snapshot could not be confirmed. Resolve any reported read error, then read the current affected ranges and re-plan the edits; do not resend them unchanged.",
    "NOT_FOUND_MATCH": "Copy old_string exactly from the current read_file output without line numbers or notices. Preserve whitespace and line endings; encode CRLF as \\r\\n in JSON strings.",
    "MULTIPLE_MATCHES": "Add surrounding lines to old_string until it is unique; use replace_all=true only if every occurrence should change.",
    "OVERLAPPING_EDITS": "Merge the overlapping items into one edit.",
    "NO_CHANGE": "Make new_string differ from old_string, or drop the item.",
    "EMPTY_OLD_STRING": "old_string must contain the exact text to replace; use write_file to create a file.",
}
_WRITE_RECOVERY_HINTS = {
    "FILE_NOT_FOUND": "The file disappeared before validation. Check the intended path before deciding whether to create it.",
    "NOT_A_FILE": "The path is not a regular file; choose the intended file path.",
    "READ_FAILED": "Inspect the path and permissions described in the error before retrying; no file replacement was attempted.",
    "WRITE_FAILED": "Path validation failed before writing; correct the path using the reported cause.",
    "NOT_READ": "Read the complete current file with read_file before replacing it, or use edit_file for a focused change.",
    "PARTIAL_READ": "Only part of the file was read. Read the complete file before replacing it, or use edit_file for a focused change.",
    "STALE_FILE": "The read snapshot could not be confirmed. Resolve any reported read error, then read the complete current file and reassess before replacing it.",
    "PARENT_NOT_DIRECTORY": "A parent path component is not a directory; choose a different path.",
    "BINARY_CONTENT": "write_file accepts UTF-8 text only; do not resend the same content.",
    "CONTENT_TOO_LARGE": "The content exceeds the write limit; do not resend it unchanged.",
}
_DELETE_RECOVERY_HINTS = {
    "DELETE_FAILED": "Path validation failed before deletion; correct the path using the reported cause.",
    "READ_FAILED": "The file could not be read before deletion; inspect its permissions before deciding whether to retry.",
    "PATH_NOT_FOUND": "Nothing exists at this path. Verify the path only if you expected it to exist.",
    "DIRECTORY_REQUIRES_RECURSIVE": "Set recursive=true only if deleting the directory and all of its contents is intended.",
    "SHA256_NOT_SUPPORTED_FOR_DIRECTORY": "expected_sha256 checks files only; omit it only if deleting the whole directory is intended.",
    "SHA256_NOT_SUPPORTED_FOR_SYMLINK": "expected_sha256 checks regular files only; omit it only if deleting the symbolic link itself is intended. The target will be preserved.",
    "INVALID_SHA256": "Supply a 64-character hexadecimal SHA-256 digest from the intended file.",
    "UNSUPPORTED_PATH": "The path is not a regular file or directory; inspect its type before choosing an appropriate operation.",
    "SHA256_MISMATCH": "The file's content differs from expected_sha256. Inspect the file before deciding whether to delete it.",
    "UNSAFE_PATH": "This path is protected from deletion; do not delete it by other means.",
}


def _reject_pre_effect_failure(result: IntrospectionResult, hints: dict[str, str]) -> IntrospectionResult:
    """Report a refusal that happened before any effect as a rejected call."""

    if result.status == RuntimeStatus.OK:
        failed = (result.structured or {}).get("failed_edits") or ()
        recovery = " ".join(dict.fromkeys(hints[item["error_code"]] for item in failed
            if isinstance(item, dict) and item.get("error_code") in hints))
        if recovery:
            return replace(result, recovery_hint=" ".join(filter(None, (result.recovery_hint, recovery))))
        return result
    structured = dict(result.structured or {})
    error_code = str(structured.get("error_code") or structured.get("reason") or "invalid_request")
    if hints is _READ_RECOVERY_HINTS:
        return replace(result, effect_receipt=EffectReceipt(outcome=EffectOutcome.NONE),
                       recovery_hint=hints.get(error_code, FILE_READ_GUIDANCE.failure_next_steps))
    receipt = result.effect_receipt
    if receipt is not None and receipt.outcome in {EffectOutcome.APPLIED, EffectOutcome.UNKNOWN}:
        return replace(result, recovery_hint=result.recovery_hint or hints.get(error_code, ""))
    if error_code in _EFFECT_UNCERTAIN_CODES and result.status == RuntimeStatus.ERROR:
        return result
    failed_codes = [
        str(item.get("error_code") or "")
        for item in structured.get("failed_edits") or ()
        if isinstance(item, dict)
    ] or [error_code]
    recovery = " ".join(dict.fromkeys(hints[code] for code in failed_codes if code in hints))
    raise ToolRejectedError(
        str(result.llm_text or result.text),
        error_code=error_code,
        details=structured,
        recovery_hint=recovery,
    )


def get_file_state_cache() -> FileStateCache:
    """Return an isolated legacy cache for direct business-handler callers."""

    return FileStateCache()


def _tool_capability_result(tool: object, args: dict[str, object]) -> IntrospectionResult:
    return tool.invoke(dict(args))


def _file_tool_result(
    tool: object,
    call: IntrospectionCall,
    *,
    defer_delivery: bool,
    context: object,
    hints: dict[str, str],
) -> IntrospectionResult:
    runtime = call.meta.get("execution_runtime")
    snapshots = getattr(runtime, "result_snapshots", None)
    path = str(call.args.get("file_path") or "")
    ref = snapshots.lookup_path(path) if snapshots is not None and path else None
    if (snapshots is not None and path and snapshots.manages_path(path)
            and isinstance(tool, (FileEditTool, FileWriteTool))):
        raise ToolRejectedError("Output snapshots are immutable copies, not editable source files.", error_code="immutable_result_snapshot",
            recovery_hint="Read the original source file before editing it; output snapshots are retained evidence copies.")
    delivery_id = getattr(call.meta.get("tool_call"), "call_id", "") or uuid4().hex
    if ref is not None:
        snapshots.retain_delivery((ref,), lifetime=context.execution_lifetime_id, call_id=delivery_id)
    try:
        result = _reject_pre_effect_failure(_tool_capability_result(tool, call.args), hints)
    except BaseException:
        if ref is not None:
            snapshots.finish_delivery(lifetime=context.execution_lifetime_id, call_id=delivery_id)
        raise
    if ref is not None:
        return replace(result, context_delivery=None, snapshot_refs=(ref,))
    delivery = getattr(result, "context_delivery", None)
    if defer_delivery or not isinstance(delivery, dict):
        return result
    runtime = call.meta.get("execution_runtime")
    commit = getattr(runtime, "commit_tool_delivery", None)
    if callable(commit):
        direct_context_id = str(call.meta.get("direct_context_id") or "").strip()
        commit(
            turn_id=direct_context_id,
            context_delivery=dict(delivery),
            result_id=f"direct:{uuid4().hex}",
        )
    else:
        backend = getattr(runtime, "logical_state", None)
        record = getattr(backend, "record_delivery", None)
        execution_lifetime_id = str(
            getattr(context, "execution_lifetime_id", "") or ""
        )
        if callable(record) and execution_lifetime_id:
            committed = dict(delivery)
            committed["result_id"] = f"direct:{uuid4().hex}"
            record(
                execution_lifetime_id=execution_lifetime_id,
                delivery=committed,
            )
    return result


def _session_file_tools(owner: object, call: IntrospectionCall):
    _ = owner
    turn_id = str(
        call.meta.get("turn_id")
        or call.meta.get("direct_context_id")
        or ""
    ).strip()
    runtime = call.meta.get("execution_runtime")
    if not turn_id:
        raise ToolRejectedError(
            "file tools require an explicit logical turn",
            error_code="missing_execution_lifetime",
        )
    if runtime is None or not callable(
        getattr(runtime, "logical_context_for_turn", None)
    ):
        raise ToolRejectedError(
            "file tools require the ExecutionRuntime logical-state owner",
            error_code="missing_execution_runtime",
        )
    context = runtime.logical_context_for_turn(turn_id)
    backend = runtime.logical_state
    return (
        SessionFileStateCache(backend=backend, context=context),
        SessionFileVisibilityCache(backend=backend, context=context,
            visible_result_ids=(runtime.execution_sessions.visible_results(turn_id)
                if call.meta.get("turn_id") else None)),
        context,
        bool(str(call.meta.get("turn_id") or "").strip()),
    )


class FileCapabilityMixin:
    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="read",
        guidance=FILE_READ_GUIDANCE,
        aliases=("read_file",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinReadInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinReadOutput,
        execution=DIRECT_LOCAL_READ,
        metadata={"canonical_path": "op_file_read"},
    )
    def file_read(self, call: IntrospectionCall) -> IntrospectionResult:
        state, visibility, context, defer_delivery = _session_file_tools(self, call)
        return _file_tool_result(
            FileReadTool(
                cache=state,
                visibility_cache=visibility,
                visibility_scope=context.execution_lifetime_id,
                defer_delivery=defer_delivery,
            ),
            call,
            defer_delivery=defer_delivery,
            context=context,
            hints=_READ_RECOVERY_HINTS,
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="edit",
        guidance=FILE_EDIT_GUIDANCE,
        aliases=("edit_file",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinEditInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinEditOutput,
        execution=DIRECT_UNSAFE_LOCAL_WRITE,
        metadata={"canonical_path": "op_file_edit"},
    )
    def file_edit(self, call: IntrospectionCall) -> IntrospectionResult:
        state, _, context, defer_delivery = _session_file_tools(self, call)
        return _file_tool_result(
            FileEditTool(cache=state),
            call,
            defer_delivery=defer_delivery,
            context=context,
            hints=_EDIT_RECOVERY_HINTS,
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="write",
        guidance=FILE_WRITE_GUIDANCE,
        aliases=("write_file",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinWriteInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinWriteOutput,
        execution=DIRECT_LOCAL_WRITE,
        metadata={"canonical_path": "op_file_write"},
    )
    def file_write(self, call: IntrospectionCall) -> IntrospectionResult:
        state, _, context, defer_delivery = _session_file_tools(self, call)
        return _file_tool_result(
            FileWriteTool(cache=state),
            call,
            defer_delivery=defer_delivery,
            context=context,
            hints=_WRITE_RECOVERY_HINTS,
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="path",
        action_name="delete",
        guidance=ToolGuidance(
            purpose="Delete the file, directory, or symbolic link at the given path; deleting a link preserves its target.",
            use_when="Removing unwanted filesystem entries. Symbolic links, including links to directories and dangling links, are unlinked without following their targets; recursive=true is required only for real directories.",
            do_not_use_when="Moving or renaming files (use run_shell mv).",
            failure_next_steps="For DELETE_FAILED, deletion may be partial: inspect the remaining path before deciding whether to retry.",
        ),
        aliases=("delete_path",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinDeleteInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinDeleteOutput,
        execution=INDIRECT_LOCAL_WRITE,
        metadata={"canonical_path": "op_path_delete"},
    )
    def path_delete(self, call: IntrospectionCall) -> IntrospectionResult:
        snapshots = getattr(call.meta.get("execution_runtime"), "result_snapshots", None)
        path = str(call.args.get("file_path") or "")
        if snapshots is not None and path and snapshots.manages_path(path, include_parents=True, follow_final_symlink=False):
            raise ToolRejectedError("Output snapshots are retired with their context references.", error_code="immutable_result_snapshot",
                recovery_hint="Let snapshot retention retire these files; verify the intended source path if the task requires deleting a source.")
        return _reject_pre_effect_failure(_tool_capability_result(PathDeleteTool(), call.args), _DELETE_RECOVERY_HINTS)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="state",
        guidance=ToolGuidance(
            purpose="Inspect the read-before-edit file cache.",
            use_when="Checking whether a file has a current cached read snapshot before edit_file, or debugging stale-edit detection.",
            do_not_use_when="Reading file content (use read_file). Editing a file (use edit_file).",
            failure_next_steps="Read-only diagnostic. If cache is stale, re-read the file with read_file before editing.",
        ),
        aliases=("inspect_file_cache",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinStateInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinStateOutput,
        execution=INDIRECT_LOCAL_READ,
        metadata={"canonical_path": "op_file_state"},
    )
    def file_state(self, call: IntrospectionCall) -> IntrospectionResult:
        state, _, _, _ = _session_file_tools(self, call)
        return _tool_capability_result(FileStateTool(cache=state), call.args)
