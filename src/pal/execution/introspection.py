from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.execution.runtime import ExecutionRuntime
from pal.execution.shell_exec import ShellExecCapabilityMixin, ShellExecTool
from pal.execution.tool_search import (
    ExecutionDiscoveryCapabilityMixin,
    ExecutionToolSearchMixin,
    ToolReadTool,
    ToolSearchTool,
)
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


@dataclass(frozen=True)
class ExecutionSnapshot:
    capability_count: int
    tool_count: int


def inspect_tools(provider: "ExecutionIntrospectionProvider") -> list[dict[str, object]]:
    return provider.runtime.list_tool_specs()


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:execution",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:execution",
    target_kind="module",
)
@dataclass
class ExecutionIntrospectionProvider(
    ExecutionToolSearchMixin,
    ExecutionDiscoveryCapabilityMixin,
    ShellExecCapabilityMixin,
):
    runtime: ExecutionRuntime
    module_id: str = "execution"

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        description="Show execution runtime state",
        aliases=("introspection.module.execution.observe", "execution.introspection.observe"),
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = inspect_execution(self)
        return IntrospectionResult(status=RuntimeStatus.OK, text="execution snapshot", structured=snapshot.__dict__)


def inspect_execution(provider: ExecutionIntrospectionProvider) -> ExecutionSnapshot:
    runtime = provider.runtime
    return ExecutionSnapshot(
        capability_count=len(runtime.capabilities),
        tool_count=len(runtime.tools),
    )


def register_with_core(context: MainContext, runtime: ExecutionRuntime | None = None) -> ModuleHandle:
    resolved_runtime = runtime or context.execution_runtime
    resolved_runtime.register_tool(ShellExecTool())
    resolved_runtime.register_tool(ToolSearchTool(runtime=resolved_runtime))
    resolved_runtime.register_tool(ToolReadTool(runtime=resolved_runtime))
    provider = ExecutionIntrospectionProvider(runtime=resolved_runtime)
    handle = ModuleHandle(
        module_id="execution",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        ports={"execution": resolved_runtime},
    )
    context.register_module(handle)
    return handle
