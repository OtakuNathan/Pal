from __future__ import annotations

from typing import TYPE_CHECKING

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.memory.contracts import L3ProviderPort

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


def register_with_core(context: MainContext, plugin: L3ProviderPort) -> ModuleHandle:
    if getattr(plugin, "service", None) is None:
        try:
            plugin.service = context.require_port("memory:memory")
        except Exception:
            pass
    module_id = getattr(plugin, "module_id", f"l3.{plugin.provider_id}")
    handle = ModuleHandle(
        module_id=module_id,
        tier=MODULE_TIER_DETACHABLE,
        mounted=getattr(plugin, "mounted", True),
        detachable=True,
        introspection_provider=plugin,
        provider_refs=[plugin.provider_id],
        ports={f"provider:{plugin.provider_id}": plugin},
    )
    context.execution_runtime.l3_plugin_registry.register(plugin)
    context.execution_runtime.register_provider_ref(plugin.provider_id, plugin)
    context.register_module(handle)
    return handle
