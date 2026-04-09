from __future__ import annotations

from dataclasses import dataclass, field

from pal.shared import PromptFragmentProvider


@dataclass
class PromptFragmentRegistry:
    providers: dict[str, PromptFragmentProvider] = field(default_factory=dict)
    by_module: dict[str, list[str]] = field(default_factory=dict)

    def register(self, provider: PromptFragmentProvider) -> None:
        self.providers[provider.provider_id] = provider
        bucket = self.by_module.setdefault(provider.module_id, [])
        if provider.provider_id not in bucket:
            bucket.append(provider.provider_id)

    def unregister(self, provider_id: str) -> None:
        provider = self.providers.pop(provider_id, None)
        if provider is None:
            return
        bucket = self.by_module.get(provider.module_id, [])
        if provider_id in bucket:
            bucket.remove(provider_id)

    def unregister_module(self, module_id: str) -> list[str]:
        provider_ids = list(self.by_module.pop(module_id, []))
        for provider_id in provider_ids:
            self.providers.pop(provider_id, None)
        return provider_ids

    def list_for_prompt(self) -> tuple[PromptFragmentProvider, ...]:
        return tuple(self.providers.values())
