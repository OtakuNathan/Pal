from __future__ import annotations

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
from pal.execution.tool_facade import ToolGuidance

from pal.execution.file_tool_contracts import (
    FILE_EDIT_DESCRIPTION,
    FILE_READ_DESCRIPTION,
    FILE_WRITE_DESCRIPTION,
)
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
from pal.shared import OPERATION_NAMESPACE, IntrospectionCall, IntrospectionResult, RuntimeStatus, capability_action


def get_file_state_cache() -> FileStateCache:
    """Return an isolated legacy cache for direct business-handler callers."""

    return FileStateCache()


def _tool_capability_result(tool: object, args: dict[str, object]) -> IntrospectionResult:
    return tool.invoke(dict(args))


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
        description=FILE_READ_DESCRIPTION,
        aliases=("read_file",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinReadInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinReadOutput,
        execution=DIRECT_LOCAL_READ,
        metadata={"canonical_path": "op_file_read"},
    )
    def file_read(self, call: IntrospectionCall) -> IntrospectionResult:
        state, visibility, context, defer_delivery = _session_file_tools(self, call)
        return _tool_capability_result(
            FileReadTool(
                cache=state,
                visibility_cache=visibility,
                visibility_scope=(
                    f"{context.execution_lifetime_id}:{context.context_epoch}"
                ),
                defer_delivery=defer_delivery,
            ),
            call.args,
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="edit",
        description=FILE_EDIT_DESCRIPTION,
        aliases=("edit_file",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinEditInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinEditOutput,
        execution=DIRECT_LOCAL_WRITE,
        metadata={"canonical_path": "op_file_edit"},
    )
    def file_edit(self, call: IntrospectionCall) -> IntrospectionResult:
        state, _, _, _ = _session_file_tools(self, call)
        return _tool_capability_result(FileEditTool(cache=state), call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="write",
        description=FILE_WRITE_DESCRIPTION,
        aliases=("write_file",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinWriteInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinWriteOutput,
        execution=DIRECT_LOCAL_WRITE,
        metadata={"canonical_path": "op_file_write"},
    )
    def file_write(self, call: IntrospectionCall) -> IntrospectionResult:
        state, _, _, _ = _session_file_tools(self, call)
        return _tool_capability_result(FileWriteTool(cache=state), call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="path",
        action_name="delete",
        description="Delete a file or directory at the given path.",
        guidance=ToolGuidance(
            purpose="Delete a file or directory at the given path.",
            use_when="Regular files must be read first (or expected_sha256 must match). Directories require recursive=true.",
            do_not_use_when="Not before reading the file first. Not without user confirmation for destructive deletes.",
            failure_next_steps="Read the file first, or provide correct expected_sha256.",
        ),
        aliases=("delete_path",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinDeleteInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinDeleteOutput,
        execution=INDIRECT_LOCAL_WRITE,
        metadata={"canonical_path": "op_path_delete"},
    )
    def path_delete(self, call: IntrospectionCall) -> IntrospectionResult:
        state, _, _, _ = _session_file_tools(self, call)
        return _tool_capability_result(PathDeleteTool(cache=state), call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="state",
        description="Inspect the read-before-edit file cache. Use this to check whether a file has a current cached read snapshot before edit_file.",
        guidance=ToolGuidance(purpose="Inspect the read-before-edit file cache. Use this to check whether a file has a current cached read snapshot before edit_file."),
        aliases=("file_state",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinStateInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinStateOutput,
        execution=INDIRECT_LOCAL_READ,
        metadata={"canonical_path": "op_file_state"},
    )
    def file_state(self, call: IntrospectionCall) -> IntrospectionResult:
        state, _, _, _ = _session_file_tools(self, call)
        return _tool_capability_result(FileStateTool(cache=state), call.args)
