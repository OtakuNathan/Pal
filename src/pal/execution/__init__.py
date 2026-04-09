from pal.execution.contracts import (
    CapabilityCall,
    CapabilityCallable,
    CapabilityDescriptor,
    CapabilityResult,
    ExecutionRuntimePort,
    Plugin,
    RegisteredCapability,
    Tool,
)

__all__ = [
    "CapabilityCall",
    "CapabilityCallable",
    "CapabilityDescriptor",
    "CapabilityResult",
    "ExecutionIntrospectionProvider",
    "ExecutionSnapshot",
    "ExecutionRuntime",
    "ExecutionRuntimePort",
    "Plugin",
    "RegisteredCapability",
    "ShellExecTool",
    "ToolReadTool",
    "ToolSearchTool",
    "Tool",
    "inspect_execution",
    "inspect_tools",
    "register_with_core",
]


def __getattr__(name: str):
    if name == "ExecutionRuntime":
        from pal.execution.runtime import ExecutionRuntime

        return ExecutionRuntime
    if name in {"ExecutionIntrospectionProvider", "ExecutionSnapshot", "inspect_execution", "register_with_core"}:
        from pal.execution.introspection import (
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
    if name in {"ToolSearchTool", "ToolReadTool", "inspect_tools"}:
        from pal.execution.tool_search import ToolReadTool, ToolSearchTool, inspect_tools

        return {
            "ToolSearchTool": ToolSearchTool,
            "ToolReadTool": ToolReadTool,
            "inspect_tools": inspect_tools,
        }[name]
    raise AttributeError(name)
