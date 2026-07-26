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

from pal.execution.file_tool_contracts import (
    FILE_EDIT_DESCRIPTION,
    FILE_READ_DESCRIPTION,
    FILE_WRITE_DESCRIPTION,
)
from pal.execution.file_edit import FileEditTool
from pal.execution.file_read import (
    FileReadTool,
    FileVisibilityCache,
    SessionFileVisibilityCache,
)
from pal.execution.file_state import (
    FileStateCache,
    FileStateTool,
    SessionFileStateCache,
)
from pal.execution.file_write import FileWriteTool
from pal.execution.path_delete import PathDeleteTool
from pal.execution.session_state import InMemoryLogicalExecutionState
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
    runtime = call.meta.get("execution_runtime")
    if runtime is not None and callable(getattr(runtime, "logical_context_for_turn", None)):
        context = runtime.logical_context_for_turn(call.meta.get("turn_id"))
        backend = runtime.logical_state
        return (
            SessionFileStateCache(backend=backend, context=context),
            SessionFileVisibilityCache(backend=backend, context=context),
            context,
            True,
        )
    turn_id = str(
        call.meta.get("turn_id")
        or call.meta.get("direct_context_id")
        or ""
    ).strip()
    if not turn_id:
        state = FileStateCache()
        visibility = FileVisibilityCache()
        turn_id = f"unscoped:{id(call)}"
    else:
        states = getattr(owner, "_direct_file_states", None)
        if states is None:
            states = {}
            setattr(owner, "_direct_file_states", states)
        state = states.setdefault(turn_id, FileStateCache())
        visibility_by_turn = getattr(owner, "_direct_file_visibility", None)
        if visibility_by_turn is None:
            visibility_by_turn = {}
            setattr(owner, "_direct_file_visibility", visibility_by_turn)
        visibility = visibility_by_turn.setdefault(turn_id, FileVisibilityCache())
    backend = InMemoryLogicalExecutionState()
    context = backend.begin_input(
        logical_session_id=f"direct:{id(owner)}:{turn_id}",
        input_id=turn_id,
    )
    return (
        state,
        visibility,
        context,
        False,
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
                    f"{context.logical_session_id}:{context.context_epoch}"
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
        description=(
            "Delete a file or directory. Regular files must first be read with read_file so Pal can detect stale deletes, "
            "or expected_sha256 must match the current file bytes. Directories require recursive=true."
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
        description=(
            "Inspect the read-before-edit file cache. "
            "Use this to check whether a file has a current cached read snapshot before edit_file."
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
