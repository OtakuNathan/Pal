from __future__ import annotations

from dataclasses import dataclass

from pal.service.service import ServiceManager
from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider


@dataclass
class ServicePromptFragmentProvider(PromptFragmentProvider):
    manager: ServiceManager
    provider_id: str = "proactive.prompt.default"
    module_id: str = "proactive"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = self.manager
        if context.turn_kind != "proactive_trigger":
            return []
        return [
            PromptFragment(
                section="runtime",
                title="Task Directive",
                content=(
                    "This is a proactive task trigger for scheduled, reminder, recurring, or push work. "
                    "Execute the described task now and output the result directly.\n"
                    "Do not create, configure, or describe proactive tasks. Perform the action."
                ),
                priority=95,
            )
        ]
