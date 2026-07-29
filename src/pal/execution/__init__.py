from pal.execution.contracts import (
    CapabilityCall,
    CapabilityDescriptor,
    CapabilityResult,
    ExecutionRuntimePort,
    Plugin,
    ToolCallBudget,
)
from pal.execution.tool_facade import (
    CompleteResult,
    EffectOutcome,
    EffectReceipt,
    FailedResult,
    PagedResult,
    RejectedResult,
    ToolExecutionSemantics,
    ToolGuidance,
    ToolHandlerResult,
    ToolInvocationResult,
    ToolRejectedError,
)
__all__ = [
    "ApprovalExecutionDecorator",
    "CapabilityCall",
    "CapabilityDescriptor",
    "CapabilityResult",
    "CompleteResult",
    "ChannelSendAttachmentTool",
    "ExecutionIntrospectionProvider",
    "ExecutionSnapshot",
    "ExecutionRuntime",
    "ExecutionApprovalRequest",
    "ExecutionRuntimePort",
    "EffectOutcome",
    "EffectReceipt",
    "FailedResult",
    "FileEditTool",
    "FileReadTool",
    "FileStateCache",
    "FileStateTool",
    "FileWriteTool",
    "GitTool",
    "PathDeleteTool",
    "PagedResult",
    "Plugin",
    "RejectedResult",
    "ShellExecTool",
    "ToolExecutionSemantics",
    "ToolGuidance",
    "ToolHandlerResult",
    "ToolInvocationResult",
    "ToolRejectedError",
    "ToolRegistryGeneration",
    "ToolCallBudget",
    "get_file_state_cache",
    "inspect_execution",
    "inspect_tools",
    "register_with_core",
]


def __getattr__(name: str):
    if name == "ToolRegistryGeneration":
        from pal.execution.tool_registry import ToolRegistryGeneration

        return ToolRegistryGeneration
    if name in {"ApprovalExecutionDecorator", "ExecutionApprovalRequest"}:
        from pal.execution.approval import ApprovalExecutionDecorator, ExecutionApprovalRequest

        return {
            "ApprovalExecutionDecorator": ApprovalExecutionDecorator,
            "ExecutionApprovalRequest": ExecutionApprovalRequest,
        }[name]
    if name == "ExecutionRuntime":
        from pal.execution.runtime import ExecutionRuntime

        return ExecutionRuntime
    if name in {"ExecutionIntrospectionProvider", "ExecutionSnapshot", "inspect_execution", "register_with_core"}:
        from pal.execution.capabilities import (
            ExecutionIntrospectionProvider,
            ExecutionSnapshot,
            inspect_execution,
            register_with_core,
        )

        return {
            "ExecutionIntrospectionProvider": ExecutionIntrospectionProvider,
            "ExecutionSnapshot": ExecutionSnapshot,
            "inspect_execution": inspect_execution,
            "register_with_core": register_with_core,
        }[name]
    if name == "ShellExecTool":
        from pal.execution.shell_exec import ShellExecTool

        return ShellExecTool
    if name == "ChannelSendAttachmentTool":
        from pal.execution.channel_attachment import ChannelSendAttachmentTool

        return ChannelSendAttachmentTool
    if name == "inspect_tools":
        from pal.execution.tool_search import inspect_tools

        return inspect_tools
    if name == "FileReadTool":
        from pal.execution.file_read import FileReadTool

        return FileReadTool
    if name == "PathDeleteTool":
        from pal.execution.path_delete import PathDeleteTool

        return PathDeleteTool
    if name == "FileEditTool":
        from pal.execution.file_edit import FileEditTool

        return FileEditTool
    if name in {"FileStateCache", "FileStateTool"}:
        from pal.execution.file_state import FileStateCache, FileStateTool

        return {"FileStateCache": FileStateCache, "FileStateTool": FileStateTool}[name]
    if name == "FileWriteTool":
        from pal.execution.file_write import FileWriteTool

        return FileWriteTool
    if name == "GitTool":
        from pal.execution.git_tool import GitTool

        return GitTool
    if name == "get_file_state_cache":
        from pal.execution.capabilities import get_file_state_cache

        return get_file_state_cache
    raise AttributeError(name)
