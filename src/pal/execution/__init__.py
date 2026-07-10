from pal.execution.contracts import (
    CapabilityCall,
    CapabilityCallable,
    CapabilityDescriptor,
    CapabilityResult,
    ExecutionRuntimePort,
    Plugin,
    Tool,
    ToolCallBudget,
)
__all__ = [
    "ApprovalExecutionDecorator",
    "CapabilityCall",
    "CapabilityCallable",
    "CapabilityDescriptor",
    "CapabilityResult",
    "ChannelSendAttachmentTool",
    "ExecutionIntrospectionProvider",
    "ExecutionSnapshot",
    "ExecutionRuntime",
    "ExecutionApprovalRequest",
    "ExecutionRuntimePort",
    "FileEditTool",
    "FileReadTool",
    "FileStateCache",
    "FileStateTool",
    "FileWriteTool",
    "GitTool",
    "PathDeleteTool",
    "Plugin",
    "ShellExecTool",
    "ToolCallTool",
    "ToolReadTool",
    "ToolResultPageTool",
    "ToolSearchTool",
    "Tool",
    "ToolCallBudget",
    "get_file_state_cache",
    "inspect_execution",
    "inspect_tools",
    "register_with_core",
]


def __getattr__(name: str):
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
    if name in {"ToolCallTool", "ToolSearchTool", "ToolReadTool", "inspect_tools"}:
        from pal.execution.tool_search import ToolCallTool, ToolReadTool, ToolSearchTool, inspect_tools

        return {
            "ToolCallTool": ToolCallTool,
            "ToolSearchTool": ToolSearchTool,
            "ToolReadTool": ToolReadTool,
            "inspect_tools": inspect_tools,
        }[name]
    if name == "ToolResultPageTool":
        from pal.execution.tool_result_pager import ToolResultPageTool

        return ToolResultPageTool
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
