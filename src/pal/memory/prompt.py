from __future__ import annotations

from dataclasses import dataclass

from pal.memory.service import MemoryService
from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider


@dataclass
class MemoryPromptFragmentProvider(PromptFragmentProvider):
    service: MemoryService
    provider_id: str = "memory.prompt.default"
    module_id: str = "memory"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        l2_entries = self.service.l2_store.list_entries()[:5]
        lines: list[str] = []
        if l2_entries:
            for entry in l2_entries:
                rendered = entry.rendered.strip() or entry.summary.strip() or entry.title.strip()
                if not rendered:
                    continue
                label = entry.title.strip() or entry.entry_id
                lines.append(f"- {label}: {rendered}")
        if not lines:
            return []
        return [
            PromptFragment(
                section="memory",
                title="Recent Summaries",
                content="\n".join(lines),
                priority=50,
            )
        ]
