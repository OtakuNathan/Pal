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
                title="Operating Rules",
                content=(
                    "Answer directly when you have enough information.\n"
                    "Use tools only when needed to act, search, or recall.\n"
                    "Never infer memory or runtime state when it can be queried.\n"
                    "Capability names use stable snake_case paths grouped by namespace and domain.\n"
                    "Use operation_execution_discovery_search to find capabilities.\n"
                    "Use operation_execution_capability_call to invoke a discovered capability by name.\n"
                    "Memory usage:\n"
                    "- Use operation_l3_recall_query when you need durable context from past interactions.\n"
                    "- Use operation_l3_commit_write only for information worth keeping as durable memory.\n"
                    "- Use operation_l3_correct_patch to update an existing durable record instead of writing a duplicate.\n"
                    "- For mistakes, lessons, or completed repairs, prefer kind=\"case\" with situation/task/action/result.\n"
                    "- Never claim memory was written unless the write is confirmed."
                ),
                priority=90,
            ),
        ]
