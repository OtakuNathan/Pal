from __future__ import annotations

from dataclasses import dataclass

from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider
from pal.tasking.service import TaskingService


@dataclass
class TaskingPromptFragmentProvider(PromptFragmentProvider):
    service: TaskingService
    provider_id: str = "tasking.prompt.default"
    module_id: str = "tasking"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        return [
            PromptFragment(
                section="tasking",
                title="Tasking Context",
                content=f"Issued work orders: {len(self.service.issued_work_orders)}",
                priority=30,
            )
        ]
