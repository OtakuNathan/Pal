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
from pal.execution.tool_facade import ToolRejectedError
from pal.execution.tool_semantics import (
    DIRECT_LOCAL_READ,
    DIRECT_LOCAL_WRITE,
    INDIRECT_LOCAL_READ,
    INDIRECT_LOCAL_WRITE,
)
from pal.shared import OPERATION_NAMESPACE, IntrospectionCall, IntrospectionResult, capability_action


FILE_READ_GUIDANCE = ToolGuidance(
    purpose=(
        "Read selected lines from one UTF-8 text file. "
        "ranges=[{offset, limit}, ...] delivers multiple blocks in one call (sed -n style). "
        "When an unchanged marker is returned, reuse the earlier result; "
        "re-read only if the file changed."
    ),
    use_when=(
        "Reading local source, configuration, immutable output snapshots, or other UTF-8 text. Use ranges for multiple "
        "blocks of the same file (for example scattered definitions found by rg) instead of "
        "several single-range calls; each block renders with its own header and truncation note. "
        "Search locates relevant code; before using edit_file, use read_file to deliver the affected ranges. "
        "Reuse valid delivered reads. Shell output and reads of output snapshots do not authorize edits to the original file."
    ),
    do_not_use_when=(
        "Binary files, images, PDFs, or channel-delivered artifacts. Reading several different "
        "files at once (read_file reads one file per call; batch ranges apply within one file). "
        "offset and limit here count lines, not characters. "
        "Do not re-read an unchanged covered block after read_file returns an unchanged marker; "
        "use the earlier result unless the file changed or another range is needed."
    ),
    failure_next_steps=(
        "For FILE_NOT_FOUND or NOT_A_FILE, correct the path and use run_shell with rg --files or a bounded listing if "
        "discovery is needed. For INVALID_ARGUMENT, fix offset/limit (and every ranges entry) to be positive integers. "
        "For UNSUPPORTED_TEXT_ENCODING, do not retry as text; use the appropriate artifact or binary workflow."
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
        "Results identify applied indices and failed items; only failed items need further attention. "
        "Use separate calls for separate files."
    ),
    do_not_use_when=(
        "Creating a file or replacing its complete contents (use write_file). Every affected range must have been "
        "delivered and remain valid; reading the entire file is not required."
    ),
    failure_next_steps=(
        "For NOT_READ or PARTIAL_READ, read the missing affected ranges. For STALE_FILE, read the current affected "
        "ranges and reassess the edits against the changed content. Do not resubmit applied items. "
        "For NOT_FOUND_MATCH, copy old_string from the current read. For MULTIPLE_MATCHES, add "
        "enough surrounding context to make the match unique; use replace_all only when every match should change."
    ),
)

FILE_WRITE_GUIDANCE = ToolGuidance(
    purpose="Write complete UTF-8 text content to a local file on the Pal host, creating it or replacing all of its contents.",
    use_when=(
        "Creating a text file, or intentionally replacing an existing file's complete contents after its complete "
        "current version has been read. Missing parent directories are created. Paths are local to the Pal host; "
        "run_shell(target=...) does not change this tool's target."
    ),
    do_not_use_when=(
        "Focused changes to an existing file (use edit_file). Do not overwrite an existing file from a partial, "
        "retired, or stale read snapshot. Remote files: use run_shell on the required target."
    ),
    failure_next_steps=(
        "For NOT_READ, PARTIAL_READ, or STALE_FILE, read the complete current file with read_file before retrying. "
        "For PARENT_NOT_DIRECTORY, correct the path. For BINARY_CONTENT or CONTENT_TOO_LARGE, do not retry with the "
        "same content; use an appropriate binary or large-file workflow."
    ),
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
) -> IntrospectionResult:
    runtime = call.meta.get("execution_runtime")
    snapshots = getattr(runtime, "result_snapshots", None)
    path = str(call.args.get("file_path") or "")
    ref = snapshots.lookup_path(path) if snapshots is not None and path else None
    if (snapshots is not None and path and snapshots.manages_path(path)
            and isinstance(tool, (FileEditTool, FileWriteTool))):
        raise ToolRejectedError("Output snapshots are immutable copies, not editable source files.", error_code="immutable_result_snapshot")
    delivery_id = getattr(call.meta.get("tool_call"), "call_id", "") or uuid4().hex
    if ref is not None:
        snapshots.retain_delivery((ref,), lifetime=context.execution_lifetime_id, call_id=delivery_id)
    try:
        result = _tool_capability_result(tool, call.args)
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
        SessionFileVisibilityCache(backend=backend, context=context),
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
        execution=DIRECT_LOCAL_WRITE,
        metadata={"canonical_path": "op_file_edit"},
    )
    def file_edit(self, call: IntrospectionCall) -> IntrospectionResult:
        state, _, context, defer_delivery = _session_file_tools(self, call)
        return _file_tool_result(
            FileEditTool(cache=state),
            call,
            defer_delivery=defer_delivery,
            context=context,
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
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="path",
        action_name="delete",
        guidance=ToolGuidance(
            purpose="Delete a file or directory at the given path.",
            use_when="Removing unwanted files or directories from the filesystem.",
            do_not_use_when="Moving or renaming files (use run_shell mv).",
            failure_next_steps="If SHA256_MISMATCH, inspect the file and retry with its current digest. If DIRECTORY_REQUIRES_RECURSIVE, set recursive=true.",
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
        if snapshots is not None and path and snapshots.manages_path(path, include_parents=True):
            raise ToolRejectedError("Output snapshots are retired with their context references.", error_code="immutable_result_snapshot")
        return _tool_capability_result(PathDeleteTool(), call.args)

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
        aliases=("file_state",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinStateInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinStateOutput,
        execution=INDIRECT_LOCAL_READ,
        metadata={"canonical_path": "op_file_state"},
    )
    def file_state(self, call: IntrospectionCall) -> IntrospectionResult:
        state, _, _, _ = _session_file_tools(self, call)
        return _tool_capability_result(FileStateTool(cache=state), call.args)
