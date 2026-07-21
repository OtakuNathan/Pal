from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.execution.file_capabilities import FileCapabilityMixin, get_file_state_cache as _get_file_state_cache
from pal.execution.file_state import FileStateCache
from pal.execution.git_capabilities import GitCapabilityMixin
from pal.execution.runtime import ExecutionRuntime
from pal.execution.shell_exec import ShellExecCapabilityMixin
from pal.execution.tool_search import (
    ExecutionDiscoveryCapabilityMixin,
    ExecutionToolSearchMixin,
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
from pal.shared.result_rendering import render_titled_structured_for_llm

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


# Singleton file-state cache shared by FileEditTool and future file-read tools.
def get_file_state_cache() -> FileStateCache:
    """Return the module-level singleton :class:`FileStateCache`."""
    return _get_file_state_cache()


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
    GitCapabilityMixin,
    FileCapabilityMixin,
):
    runtime: ExecutionRuntime
    module_id: str = "execution"

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        description="Show execution runtime state",
        aliases=("introspection_module_execution_observe", "execution_introspection_observe"),
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = inspect_execution(self)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="execution snapshot",
            structured=snapshot.__dict__,
            llm_text=render_titled_structured_for_llm("Execution snapshot", snapshot.__dict__),
        )


def inspect_execution(provider: ExecutionIntrospectionProvider) -> ExecutionSnapshot:
    runtime = provider.runtime
    return ExecutionSnapshot(
        capability_count=len(runtime.bound_action_index.actions),
        tool_count=len(runtime.tools),
    )


def register_with_core(context: MainContext, runtime: ExecutionRuntime | None = None) -> ModuleHandle:
    resolved_runtime = runtime or context.execution_runtime
    provider = ExecutionIntrospectionProvider(runtime=resolved_runtime)
    handle = ModuleHandle(
        module_id="execution",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        ports={"execution": resolved_runtime},
        shutdown_sync=resolved_runtime.shutdown,
    )
    context.register_module(handle)
    return handle
