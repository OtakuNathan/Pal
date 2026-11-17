from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

from pal.memory.contracts import L3ProviderPort
from pal.shared.registration import RegistrationKey, OwnershipEvent, owner_relation, reduce_ownership


@dataclass
class L3PluginRegistry:
    plugins: dict[str, L3ProviderPort] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock, repr=False)

    def validate(self, plugin: L3ProviderPort, *, module_id: str) -> None:
        key = RegistrationKey("l3", plugin.provider_id)
        if module_id != f"{key.domain}.{key.name}":
            raise ValueError(f"L3 provider {key.qualified} does not belong to module {module_id}")
        reduce_ownership(owner_relation(self.plugins.get(plugin.provider_id), plugin), OwnershipEvent.CLAIM)

    def register(self, plugin: L3ProviderPort) -> None:
        with self._lock:
            self.validate(plugin, module_id=f"l3.{plugin.provider_id}")
            self.plugins[plugin.provider_id] = plugin

    def unregister(self, plugin: L3ProviderPort) -> None:
        with self._lock:
            reduce_ownership(owner_relation(self.plugins.get(plugin.provider_id), plugin), OwnershipEvent.RELEASE)
            if self.plugins.get(plugin.provider_id) is plugin:
                self.plugins.pop(plugin.provider_id)

    def get(self, provider_id: str) -> L3ProviderPort | None:
        return self.plugins.get(provider_id)

    def require(self, provider_id: str) -> L3ProviderPort:
        plugin = self.get(provider_id)
        if plugin is None:
            raise KeyError(f"unknown l3 provider: {provider_id}")
        return plugin
