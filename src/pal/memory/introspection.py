from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.memory.service import MemoryService
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
class MemorySnapshot:
    l1_count: int
    l2_count: int
    active_l3_provider: str
    available_l3_providers: list[str]


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:memory",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:memory",
    target_kind="module",
)
@dataclass
class MemoryIntrospectionProvider:
    service: MemoryService
    context: MainContext
    module_id: str = "memory"

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show", description="Show memory runtime state")
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = inspect_memory(self)
        return IntrospectionResult(status=RuntimeStatus.OK, text="memory snapshot", structured=snapshot.__dict__)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="list_providers",
        description="List registered L3 providers",
    )
    def list_providers(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        items = []
        for provider_id in sorted(self.context.execution_runtime.l3_plugin_registry.plugins):
            provider = self.context.execution_runtime.l3_plugin_registry.get(provider_id)
            items.append(
                {
                    "provider_id": provider_id,
                    "module_id": getattr(provider, "module_id", f"l3.{provider_id}") if provider is not None else f"l3.{provider_id}",
                    "mounted": bool(getattr(provider, "mounted", True)) if provider is not None else False,
                }
            )
        return IntrospectionResult(status=RuntimeStatus.OK, text="memory l3 providers", structured={"items": items})

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="active_provider",
        description="Show the current active L3 provider",
    )
    def active_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        provider_id = self.service.l3_selector.active_provider_id
        provider = self.context.execution_runtime.l3_plugin_registry.get(provider_id)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="memory active l3 provider",
            structured={
                "provider_id": provider_id,
                "module_id": getattr(provider, "module_id", f"l3.{provider_id}") if provider is not None else f"l3.{provider_id}",
                "mounted": bool(getattr(provider, "mounted", True)) if provider is not None else False,
            },
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="management",
        action_name="set_active_provider",
        description="Switch the active L3 provider for memory recall",
        args_schema={
            "type": "object",
            "properties": {
                "active_provider_id": {"type": "string"},
            },
            "required": ["active_provider_id"],
        },
    )
    def set_active_provider(self, call: IntrospectionCall) -> IntrospectionResult:
        provider_id = str(call.args.get("active_provider_id") or "").strip()
        if not provider_id:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="active_provider_id is required")
        if self.context.execution_runtime.l3_plugin_registry.get(provider_id) is None:
            return IntrospectionResult(status=RuntimeStatus.NOT_FOUND, text="unknown l3 provider", structured={"active_provider_id": provider_id})
        self.service.l3_selector.active_provider_id = provider_id
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="memory active l3 provider updated",
            structured={"active_provider_id": provider_id},
        )


def inspect_memory(provider: MemoryIntrospectionProvider) -> MemorySnapshot:
    service = provider.service
    return MemorySnapshot(
        l1_count=len(service.l1_store.items),
        l2_count=len(service.l2_store.items),
        active_l3_provider=service.l3_selector.active_provider_id,
        available_l3_providers=sorted(provider.context.execution_runtime.l3_plugin_registry.plugins),
    )


def register_with_core(context: MainContext, service: MemoryService) -> ModuleHandle:
    from pal.memory.prompt import MemoryPromptFragmentProvider

    provider = MemoryIntrospectionProvider(service=service, context=context)
    prompt_provider = MemoryPromptFragmentProvider(service=service)
    handle = ModuleHandle(
        module_id="memory",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        ports={"memory": service},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    return handle
