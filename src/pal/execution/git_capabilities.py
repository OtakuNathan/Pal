from __future__ import annotations

from pal.execution.generated_tool_models import (
    ExecutionGitCapabilitiesGitCapabilityMixinGitInput,
    ExecutionGitCapabilitiesGitCapabilityMixinGitOutput,
)

from pal.execution.file_capabilities import _tool_capability_result
from pal.execution.git_tool import GIT_TOOL_DESCRIPTION, GitTool
from pal.execution.tool_semantics import DIRECT_UNSAFE_LOCAL_WRITE
from pal.shared import OPERATION_NAMESPACE, IntrospectionCall, IntrospectionResult, capability_action


class GitCapabilityMixin:
    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="git",
        action_name="git",
        description=GIT_TOOL_DESCRIPTION,
        aliases=("git",),
        InputModel=ExecutionGitCapabilitiesGitCapabilityMixinGitInput,
        OutputModel=ExecutionGitCapabilitiesGitCapabilityMixinGitOutput,
        execution=DIRECT_UNSAFE_LOCAL_WRITE,
        metadata={"canonical_path": "op_git"},
    )
    def git(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(GitTool(), call.args)
