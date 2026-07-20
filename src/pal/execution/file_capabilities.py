from __future__ import annotations

from pal.execution.file_tool_contracts import (
    FILE_EDIT_DESCRIPTION,
    FILE_EDIT_RESULT_SCHEMA,
    FILE_READ_DESCRIPTION,
    FILE_READ_RESULT_SCHEMA,
    FILE_WRITE_DESCRIPTION,
    FILE_WRITE_RESULT_SCHEMA,
    file_edit_args_schema,
    file_read_args_schema,
    file_write_args_schema,
)
from pal.shared import OPERATION_NAMESPACE, IntrospectionCall, IntrospectionResult, RuntimeStatus, capability_action


def _tool_capability_result(runtime, tool_name: str, args: dict[str, object]) -> IntrospectionResult:
    result = runtime.execute_tool(type("ToolCall", (), {"name": tool_name, "args": dict(args)})())
    return IntrospectionResult(
        status=RuntimeStatus.OK if result.ok else RuntimeStatus.ERROR,
        text=result.text,
        structured=result.structured,
        llm_text=result.llm_text,
    )


class FileCapabilityMixin:
    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="read",
        description=FILE_READ_DESCRIPTION,
        aliases=("file_read",),
        args_schema=file_read_args_schema(),
        result_schema=FILE_READ_RESULT_SCHEMA,
        metadata={"canonical_path": "op_file_read"},
    )
    def file_read(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "op_file_read", call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="edit",
        description=FILE_EDIT_DESCRIPTION,
        aliases=("file_edit",),
        args_schema=file_edit_args_schema(),
        result_schema=FILE_EDIT_RESULT_SCHEMA,
        metadata={"canonical_path": "op_file_edit"},
    )
    def file_edit(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "op_file_edit", call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="write",
        description=FILE_WRITE_DESCRIPTION,
        aliases=("file_write",),
        args_schema=file_write_args_schema(),
        result_schema=FILE_WRITE_RESULT_SCHEMA,
        metadata={"canonical_path": "op_file_write"},
    )
    def file_write(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "op_file_write", call.args)

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
        args_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to delete."},
                "expected_sha256": {
                    "type": "string",
                    "description": "Optional current SHA-256 digest for regular files. If supplied, a prior file_read snapshot is not required.",
                },
                "recursive": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required for directory deletion. Regular file deletion does not require this.",
                },
            },
            "required": ["file_path"],
        },
        result_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "deleted": {"type": "boolean"},
                "path_kind": {"type": "string"},
                "recursive": {"type": "boolean"},
                "sha256": {"type": "string"},
                "error_code": {"type": "string"},
            },
        },
        metadata={"canonical_path": "op_path_delete"},
    )
    def path_delete(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "op_path_delete", call.args)

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
        args_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Optional file path to check against the read-before-edit cache.",
                },
            },
        },
        result_schema={
            "type": "object",
            "properties": {
                "cached_file_count": {"type": "integer"},
                "file_path": {"type": "string"},
                "cached": {"type": "boolean"},
                "valid": {"type": "boolean"},
                "full_view": {"type": "boolean"},
                "content_length": {"type": "integer"},
            },
        },
        metadata={"canonical_path": "op_file_state"},
    )
    def file_state(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "op_file_state", call.args)
