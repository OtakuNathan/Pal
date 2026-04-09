from __future__ import annotations

from dataclasses import dataclass, field

from pal.memory.contracts import L3ProviderPort


@dataclass
class L3PluginRegistry:
    plugins: dict[str, L3ProviderPort] = field(default_factory=dict)

    def register(self, plugin: L3ProviderPort) -> None:
        self.plugins[plugin.provider_id] = plugin

    def get(self, provider_id: str) -> L3ProviderPort | None:
        return self.plugins.get(provider_id)

    def require(self, provider_id: str) -> L3ProviderPort:
        plugin = self.get(provider_id)
        if plugin is None:
            raise KeyError(f"unknown l3 provider: {provider_id}")
        return plugin
