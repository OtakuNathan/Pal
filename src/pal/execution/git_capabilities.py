from __future__ import annotations

from pal.execution.file_capabilities import _tool_capability_result
from pal.execution.git_tool import GIT_TOOL_CMD_DESCRIPTION, GIT_TOOL_DESCRIPTION
from pal.shared import OPERATION_NAMESPACE, IntrospectionCall, IntrospectionResult, capability_action


class GitCapabilityMixin:
    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="git",
        action_name="git",
        description=GIT_TOOL_DESCRIPTION,
        aliases=("git",),
        args_schema={
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": GIT_TOOL_CMD_DESCRIPTION},
                "cwd": {"type": "string", "description": "Optional repository working directory."},
                "timeout_ms": {"type": "integer", "minimum": 1, "description": "Optional timeout in milliseconds."},
            },
            "required": ["cmd"],
            "additionalProperties": False,
        },
        result_schema={
            "type": "object",
            "properties": {
                "cmd": {"type": "string"},
                "classification": {"type": "object"},
                "returncode": {"type": "integer"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "audit_id": {"type": "string"},
                "error_code": {"type": "string"},
            },
        },
        metadata={"canonical_path": "op_git"},
    )
    def git(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(self.runtime, "op_git", call.args)
