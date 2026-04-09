from __future__ import annotations

from dataclasses import dataclass

from pal.service.service import ServiceManager
from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider


@dataclass
class ServicePromptFragmentProvider(PromptFragmentProvider):
    manager: ServiceManager
    provider_id: str = "service.prompt.default"
    module_id: str = "service"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        registered = sorted(self.manager.registered)
        content = "Registered services: " + (", ".join(registered) if registered else "none")
        return [
            PromptFragment(
                section="service",
                title="Service Context",
                content=content,
                priority=40,
            )
        ]
