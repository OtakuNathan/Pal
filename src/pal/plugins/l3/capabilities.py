from __future__ import annotations

from typing import TYPE_CHECKING

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.memory.contracts import L3ProviderPort

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


def register_with_core(context: MainContext, plugin: L3ProviderPort) -> ModuleHandle:
    module_id = f"l3.{plugin.provider_id}"
    handle = ModuleHandle(
        module_id=module_id,
        tier=MODULE_TIER_DETACHABLE,
        mounted=True,
        detachable=True,
        introspection_provider=plugin,
        l3_provider=plugin,
        ports={f"provider:{plugin.provider_id}": plugin},
    )
    context.register_module(handle)
    return handle
