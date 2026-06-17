from __future__ import annotations

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
        description=(
            "Read a UTF-8 text file and return a line-numbered slice. "
            "A successful read caches the full file for subsequent file_edit calls."
        ),
        aliases=("file_read",),
        args_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file to read."},
                "offset": {"type": "integer", "minimum": 1, "description": "1-based starting line number."},
                "limit": {"type": "integer", "minimum": 1, "description": "Maximum number of lines to return."},
            },
            "required": ["file_path"],
        },
        result_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "total_lines": {"type": "integer"},
                "truncated": {"type": "boolean"},
                "encoding": {"type": "string"},
                "error_code": {"type": "string"},
            },
        },
        metadata={"canonical_path": "op_file_read"},
    )
    def file_read(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "op_file_read", call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="edit",
        description=(
            "Edit a UTF-8 text file by replacing one exact old_string with new_string. "
            "The file must first be read with op_file_read so Pal can detect stale edits. "
            "Returns a unified diff patch on success."
        ),
        aliases=("file_edit",),
        args_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file to edit."},
                "old_string": {"type": "string", "description": "Exact text to find and replace."},
                "new_string": {"type": "string", "description": "Replacement text."},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
        result_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "error_code": {"type": "string"},
                "patch": {"type": "string"},
                "match_count": {"type": "integer"},
            },
        },
        metadata={"canonical_path": "op_file_edit"},
    )
    def file_edit(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "op_file_edit", call.args)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="file",
        action_name="write",
        description=(
            "Create, overwrite, or append to a UTF-8 text file. "
            "Create mode creates missing parent directories. "
            "Overwrite and append require a current prior op_file_read snapshot."
        ),
        aliases=("file_write",),
        args_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to write."},
                "content": {"type": "string", "description": "UTF-8 text content to write."},
                "mode": {
                    "type": "string",
                    "enum": ["create", "overwrite", "append"],
                    "default": "create",
                    "description": (
                        "Write mode. 'create' creates a new file and fails if it exists. "
                        "Create mode also creates missing parent directories. "
                        "'overwrite' replaces the full existing file after file_read. "
                        "'append' adds content to the end of an existing file after file_read."
                    ),
                },
            },
            "required": ["file_path", "content"],
        },
        result_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "bytes_written": {"type": "integer"},
                "created": {"type": "boolean"},
                "encoding": {"type": "string"},
                "mode": {"type": "string"},
                "error_code": {"type": "string"},
            },
        },
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
                "content_length": {"type": "integer"},
            },
        },
        metadata={"canonical_path": "op_file_state"},
    )
    def file_state(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "op_file_state", call.args)
