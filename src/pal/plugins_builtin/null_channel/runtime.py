from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pal.channel.contracts import EndpointConfig
from pal.channel.models import ChannelEndpointModel
from pal.channel.plugin import ChannelExtension
from pal.channel.provider_manager import FactoryChannelProvider
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.plugins.contracts import PluginBuildContext
from pal.plugins.lifecycle import PluginScope

from .endpoint import NullChannelEndpoint


@dataclass(frozen=True)
class NullChannelEndpointFactory:
    channel_kind: str = "null"
    reload_modules: tuple[str, ...] = ()

    def create(self, record: ChannelEndpointModel, *, runtime_root: Path) -> NullChannelEndpoint:
        endpoint = NullChannelEndpoint(endpoint=EndpointConfig(
            endpoint_id=record.endpoint_id,
            channel_kind=record.channel_kind,
            binding_key=record.binding_key,
        ))
        endpoint.enabled = bool(record.enabled)
        endpoint.attached = record.detached_at is None
        endpoint.paired = True
        return endpoint


class NullChannelPlugin:
    plugin_id = "null_channel"
    version = "1.0.0"

    def start(self, scope: PluginScope) -> ModuleHandle:
        manager = scope.context.require_port("channel:provider_manager")
        record = manager.repository.get("null")
        if record is None:
            manager.repository.upsert(
                endpoint_id="null", channel_kind="null", binding_key="/dev/null", enabled=True,
            )
        elif record.channel_kind != "null":
            raise ValueError("Channel endpoint 'null' already belongs to a different channel kind")
        # Preserve an existing endpoint's enabled/detached state on reload.
        provider = FactoryChannelProvider(
            provider_id=self.plugin_id, endpoint_types=("null",), factory=NullChannelEndpointFactory(),
        )
        return ModuleHandle(
            module_id=self.plugin_id, tier=MODULE_TIER_DETACHABLE, detachable=True,
            channel_extension=ChannelExtension(provider=provider),
        )


def build_plugin(context: PluginBuildContext) -> NullChannelPlugin:
    return NullChannelPlugin()
