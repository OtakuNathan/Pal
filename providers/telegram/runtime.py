from __future__ import annotations

from dataclasses import dataclass

from pal.channel.provider_manager import (
    ChannelProviderBuildContext,
    ChannelProviderContext,
    FactoryChannelProvider,
)

from .endpoint import TelegramChannelEndpointFactory


@dataclass
class TelegramChannelProvider(FactoryChannelProvider):
    def set_auth_material(
        self,
        endpoint_id: str,
        material: dict,
        context: ChannelProviderContext,
    ):
        result = super().set_auth_material(endpoint_id, material, context)
        bot_token = str(material.get("bot_token") or "").strip()
        if bot_token:
            context.repository.merge_binding_metadata(
                endpoint_id,
                {"bot_token": bot_token},
            )
        return result


def build_channel_provider(
    context: ChannelProviderBuildContext,
) -> TelegramChannelProvider:
    factory = TelegramChannelEndpointFactory()
    return TelegramChannelProvider(
        provider_id=context.manifest.provider_id,
        endpoint_types=(factory.channel_kind,),
        factory=factory,
        reload_modules=(),
    )
