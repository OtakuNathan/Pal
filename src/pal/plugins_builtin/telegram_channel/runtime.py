from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.channel.endpoints.telegram_endpoint import TelegramChannelEndpointFactory
from pal.channel.provider_manager import ChannelProviderContext, FactoryChannelProvider
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.plugins.contracts import PluginBuildContext


@dataclass
class TelegramChannelProvider(FactoryChannelProvider):
    manager: Any = None
    module_id: str = "telegram_channel"
    mounted: bool = False

    def attach(self, call) -> None:
        _ = call
        if self.manager is not None:
            self.manager.register_provider(self)
        self.mounted = True

    def detach(self, call) -> None:
        _ = call
        if self.manager is not None:
            self.manager.unload_provider_endpoints(self.provider_id)
            self.manager.unregister_provider(self.provider_id)
        self.mounted = False

    def set_auth_material(self, endpoint_id: str, material: dict, context: ChannelProviderContext):
        result = super().set_auth_material(endpoint_id, material, context)
        bot_token = str(material.get("bot_token") or "").strip()
        if bot_token:
            context.repository.merge_binding_metadata(endpoint_id, {"bot_token": bot_token})
        return result


@dataclass
class TelegramChannelBuiltinBundle:
    plugin_id: str = "telegram_channel"
    version: str = "0.1.0"

    def register_with_core(self, context) -> ModuleHandle:
        factory = TelegramChannelEndpointFactory()
        provider = TelegramChannelProvider(
            provider_id="telegram",
            endpoint_types=(factory.channel_kind,),
            factory=factory,
            reload_modules=factory.reload_modules,
            manager=context.require_port("channel:provider_manager"),
        )
        handle = ModuleHandle(
            module_id="telegram_channel",
            tier=MODULE_TIER_DETACHABLE,
            detachable=True,
            introspection_provider=provider,
            supports_lifecycle_capabilities=True,
            ports={"provider": provider},
        )
        context.register_module(handle)
        return handle


def build_plugin(context: PluginBuildContext) -> TelegramChannelBuiltinBundle:
    _ = context
    return TelegramChannelBuiltinBundle()
