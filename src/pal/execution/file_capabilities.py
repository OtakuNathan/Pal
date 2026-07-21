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
from pal.execution.file_read import FileReadTool
from pal.execution.file_state import FileStateCache, FileStateTool
from pal.execution.file_write import FileWriteTool
from pal.execution.path_delete import PathDeleteTool
from pal.execution.tool_semantics import (
    DIRECT_LOCAL_READ,
    DIRECT_LOCAL_WRITE,
    INDIRECT_LOCAL_READ,
    INDIRECT_LOCAL_WRITE,
)
from pal.shared import OPERATION_NAMESPACE, IntrospectionCall, IntrospectionResult, RuntimeStatus, capability_action


_FILE_STATE_CACHE = FileStateCache()


def get_file_state_cache() -> FileStateCache:
    return _FILE_STATE_CACHE


def _tool_capability_result(tool: object, args: dict[str, object]) -> IntrospectionResult:
    return tool.invoke(dict(args))


class FileCapabilityMixin:
    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="read",
        description=FILE_READ_DESCRIPTION,
        aliases=("file_read",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinReadInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinReadOutput,
        execution=DIRECT_LOCAL_READ,
        metadata={"canonical_path": "op_file_read"},
    )
    def file_read(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(FileReadTool(cache=_FILE_STATE_CACHE), call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="edit",
        description=FILE_EDIT_DESCRIPTION,
        aliases=("file_edit",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinEditInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinEditOutput,
        execution=DIRECT_LOCAL_WRITE,
        metadata={"canonical_path": "op_file_edit"},
    )
    def file_edit(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(FileEditTool(cache=_FILE_STATE_CACHE), call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="write",
        description=FILE_WRITE_DESCRIPTION,
        aliases=("file_write",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinWriteInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinWriteOutput,
        execution=DIRECT_LOCAL_WRITE,
        metadata={"canonical_path": "op_file_write"},
    )
    def file_write(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(FileWriteTool(cache=_FILE_STATE_CACHE), call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="path",
        action_name="delete",
        description=(
            "Delete a file or directory. Regular files must first be read with op_file_read so Pal can detect stale deletes, "
            "or expected_sha256 must match the current file bytes. Directories require recursive=true."
        ),
        aliases=("path_delete",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinDeleteInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinDeleteOutput,
        execution=INDIRECT_LOCAL_WRITE,
        metadata={"canonical_path": "op_path_delete"},
    )
    def path_delete(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(PathDeleteTool(cache=_FILE_STATE_CACHE), call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="state",
        description=(
            "Inspect the read-before-edit file cache. "
            "Use this to check whether a file has a current cached read snapshot before op_file_edit."
        ),
        aliases=("file_state",),
        InputModel=ExecutionFileCapabilitiesFileCapabilityMixinStateInput,
        OutputModel=ExecutionFileCapabilitiesFileCapabilityMixinStateOutput,
        execution=INDIRECT_LOCAL_READ,
        metadata={"canonical_path": "op_file_state"},
    )
    def file_state(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(FileStateTool(cache=_FILE_STATE_CACHE), call.args)
