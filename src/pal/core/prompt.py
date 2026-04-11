from __future__ import annotations

from dataclasses import dataclass

from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider


@dataclass
class MinimalOperatingRulesPromptFragmentProvider(PromptFragmentProvider):
    provider_id: str = "core.prompt.minimal_rules"
    module_id: str = "core"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        return [
            PromptFragment(
                section="rules",
                title="Minimal Operating Rules",
                content=(
                    "Directly answer the user when you already have enough information.\n"
                    "Use capabilities only when they are actually needed to answer, inspect, or act.\n"
                    "If you need to discover available capabilities or confirm how to call one, use "
                    "`operation_execution_discovery_search` and `operation_execution_discovery_read`.\n"
                    "Do not call discovery by default when a direct answer is enough.\n"
                    "You operate through capabilities, not raw tools."
                ),
                priority=90,
            )
        ]
