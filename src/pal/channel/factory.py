from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pal.channel.channel_endpoint_queue_base import ChannelEndpointBase
from pal.channel.contracts import EndpointConfig
from pal.channel.models import ChannelEndpointModel


class ChannelEndpointFactory(Protocol):
    channel_kind: str
    reload_modules: tuple[str, ...]

    def create(
        self,
        record: ChannelEndpointModel,
        *,
        runtime_root: Path,
    ) -> ChannelEndpointBase | None:
        ...


@dataclass
class ChannelEndpointFactoryRegistry:
    factories: dict[str, ChannelEndpointFactory] = field(default_factory=dict)

    def register(self, factory: ChannelEndpointFactory) -> None:
        self.factories[factory.channel_kind] = factory

    def reload_modules_for_kind(self, channel_kind: str) -> tuple[str, ...]:
        factory = self.factories.get(channel_kind)
        if factory is None:
            return ()
        return tuple(str(item) for item in getattr(factory, "reload_modules", ()) if str(item).strip())

    def create(
        self,
        record: ChannelEndpointModel,
        *,
        runtime_root: Path,
    ) -> ChannelEndpointBase | None:
        factory = self.factories.get(record.channel_kind)
        if factory is None:
            return None
        return factory.create(record, runtime_root=runtime_root)


@dataclass(frozen=True)
class SocketChannelEndpointFactory:
    channel_kind: str = "socket"
    reload_modules: tuple[str, ...] = (
        "pal.channel.factory",
        "pal.channel.endpoints.socket_endpoint",
        "pal.channel.endpoints.socket_protocol",
    )

    def create(
        self,
        record: ChannelEndpointModel,
        *,
        runtime_root: Path,
    ) -> ChannelEndpointBase | None:
        _ = runtime_root
        from pal.channel.endpoints.socket_endpoint import SocketChannelEndpoint

        endpoint = EndpointConfig(
            endpoint_id=record.endpoint_id,
            channel_kind=record.channel_kind,
            binding_key=record.binding_key,
            send_policy=dict(record.send_policy_blob or {}),
        )
        runtime_endpoint = SocketChannelEndpoint(endpoint=endpoint)
        runtime_endpoint.enabled = bool(record.enabled)
        runtime_endpoint.attached = record.detached_at is None
        runtime_endpoint.paired = True
        return runtime_endpoint


def build_default_factory_registry() -> ChannelEndpointFactoryRegistry:
    from pal.channel.endpoints.telegram_endpoint import TelegramChannelEndpointFactory

    registry = ChannelEndpointFactoryRegistry()
    registry.register(SocketChannelEndpointFactory())
    registry.register(TelegramChannelEndpointFactory())
    return registry
