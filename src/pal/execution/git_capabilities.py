from __future__ import annotations

from pal.execution.generated_tool_models import (
    ExecutionGitCapabilitiesGitCapabilityMixinGitInput,
    ExecutionGitCapabilitiesGitCapabilityMixinGitOutput,
)

from pal.execution.file_capabilities import _tool_capability_result
from pal.execution.git_tool import GitTool
from pal.execution.tool_semantics import DIRECT_UNSAFE_LOCAL_WRITE
from pal.shared import OPERATION_NAMESPACE, IntrospectionCall, IntrospectionResult, capability_action


class GitCapabilityMixin:
    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="git",
        action_name="git",
        guidance=ToolGuidance(
            purpose="Run git through Pal's structured git wrapper instead of shell.",
            use_when="Repository status, diffs, history, changed-file evidence, and conservative audited git mutations.",
            do_not_use_when="Running arbitrary shell commands (use run_shell). Editing files (use edit_file). Dangerous history or destructive operations are refused.",
            failure_next_steps="If the command is rejected as dangerous, use a more conservative subcommand. If shell syntax is detected, simplify to plain git args. If a mutation outcome is uncertain, inspect git status and the relevant diff/log before retrying.",
        ),
        aliases=("git",),
        InputModel=ExecutionGitCapabilitiesGitCapabilityMixinGitInput,
        OutputModel=ExecutionGitCapabilitiesGitCapabilityMixinGitOutput,
        execution=DIRECT_UNSAFE_LOCAL_WRITE,
        metadata={"canonical_path": "op_git"},
    )
    def git(self, call: IntrospectionCall) -> IntrospectionResult:
        return _tool_capability_result(GitTool(), call.args)
