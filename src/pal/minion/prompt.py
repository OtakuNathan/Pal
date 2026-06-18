from __future__ import annotations

from dataclasses import dataclass

from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider
from pal.minion.service import TaskingService


@dataclass
class TaskingPromptFragmentProvider(PromptFragmentProvider):
    service: TaskingService | None = None
    manager: object | None = None
    provider_id: str = "minion.prompt.default"
    module_id: str = "minion"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context, self.manager
        fragments: list[PromptFragment] = []
        if self.service is not None:
            fragments.append(
                PromptFragment(
                    section="runtime",
                    title="Tasking Context",
                    content=f"Issued work orders: {len(self.service.issued_work_orders)}",
                    priority=45,
                    metadata={"block_id": "tasking_context"},
                )
            )
        return fragments
