from __future__ import annotations

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
        "Read selected lines from a UTF-8 text file and return line-numbered content. "
        "When an unchanged marker is returned, refer to the earlier read result and do not call "
        "read_file again unless the file changed or another range is needed."
    ),
    use_when=(
        "Reading local source, configuration, or other UTF-8 text. Use offset and limit for focused reads. "
        "A focused edit is authorized once every affected line is present in the current logical context."
    ),
    do_not_use_when=(
        "Binary files, images, PDFs, or channel-delivered artifacts. Do not re-read an unchanged covered range after "
        "read_file returns an unchanged marker; use the earlier result unless the file changed or another range is needed."
    ),
    failure_next_steps=(
        "For FILE_NOT_FOUND or NOT_A_FILE, correct the path and use run_shell with rg --files or a bounded listing if "
        "discovery is needed. For INVALID_ARGUMENT, correct offset/limit. For UNSUPPORTED_TEXT_ENCODING, do not retry "
        "as text; use the appropriate artifact or binary workflow."
    ),
    next_tool_hints=(
        NextToolHint(
            name="edit_file",
            use_when="The affected lines are visible and a focused exact replacement is required.",
        ),
        NextToolHint(
            name="write_file",
            use_when="A new text file is needed, or a fully-read existing file must be replaced completely.",
        ),
    ),
)

FILE_EDIT_GUIDANCE = ToolGuidance(
    purpose="Replace an exact string in a UTF-8 text file after reading every affected line.",
    use_when=(
        "Making a focused change to an existing text file whose affected lines are already in the current logical "
        "context. The match must be unique unless replace_all=true is intentionally requested."
    ),
    do_not_use_when=(
        "Creating a file or replacing its complete contents (use write_file). Do not edit from an unread, partial, "
        "retired, or stale snapshot."
    ),
    failure_next_steps=(
        "For NOT_READ, PARTIAL_READ, or STALE_FILE, call read_file for the missing/current affected range and then "
        "retry the exact edit. For NOT_FOUND_MATCH, copy old_string from the current read. For MULTIPLE_MATCHES, add "
        "enough surrounding context to make the match unique; use replace_all only when every match should change."
    ),
)

FILE_WRITE_GUIDANCE = ToolGuidance(
    purpose="Write complete UTF-8 text content, creating a file or replacing all of an existing file.",
    use_when=(
        "Creating a text file, or intentionally replacing an existing file's complete contents after its complete "
        "current version has been read. Missing parent directories are created."
    ),
    do_not_use_when=(
        "Focused changes to an existing file (use edit_file). Do not overwrite an existing file from a partial, "
        "retired, or stale read snapshot."
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
    result = _tool_capability_result(tool, call.args)
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
                visibility_scope=(
                    f"{context.execution_lifetime_id}:{context.context_epoch}"
                ),
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
        execution=INDIRECT_LOCAL_WRITE,
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
