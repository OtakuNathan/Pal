from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider


@dataclass
class ControlPromptFragmentProvider(PromptFragmentProvider):
    provider: Any
    provider_id: str = "control.prompt.default"
    module_id: str = "control"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        status = "degraded" if self.provider.degraded else "normal"
        if not self.provider.degraded and context.turn_kind != "control":
            return []
        return [
            PromptFragment(
                section="runtime",
                title="Control Constraints",
                content=f"Control plane is deterministic. Mounted={self.provider.mounted}. Status={status}.",
                priority=20,
            )
        ]
