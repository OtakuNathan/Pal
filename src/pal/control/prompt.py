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
        if not self.provider.degraded and context.turn_kind != "control":
            return []
        if not self.provider.mounted:
            guidance = "Control operations are unavailable."
        elif self.provider.degraded:
            guidance = "Control operations are degraded. Check tool results before assuming success."
        else:
            guidance = "Control operations are available through the control tools."
        return [
            PromptFragment(
                section="runtime",
                title="Control Constraints",
                content=guidance,
                priority=20,
                metadata={
                    "prompt_target": "runtime_reminder",
                    "block_id": "control_constraints",
                },
            )
        ]
