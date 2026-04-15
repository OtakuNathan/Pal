from __future__ import annotations

from dataclasses import dataclass

from pal.memory.contracts import MemoryPack
from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider


@dataclass
class MemoryPromptFragmentProvider(PromptFragmentProvider):
    provider_id: str = "memory.prompt.default"
    module_id: str = "memory"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        pack = context.metadata.get("memory_pack")
        if not isinstance(pack, MemoryPack):
            return []

        fragments: list[PromptFragment] = []
        for index, message in enumerate(pack.l1_recent_context):
            role = str(message.role or "").strip()
            content = str(message.content or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            fragments.append(
                PromptFragment(
                    section="memory",
                    title="Recent Context",
                    content=content,
                    priority=40 + index,
                    metadata={
                        "block_id": f"l1_recent_context_{index}",
                        "role": role,
                    },
                )
            )

        if pack.current_summary is not None:
            summary_text = pack.current_summary.rendered.strip() or pack.current_summary.summary.strip()
            if summary_text:
                fragments.append(
                    PromptFragment(
                        section="memory",
                        title="Current Summary",
                        content=summary_text,
                        priority=55,
                        metadata={"block_id": "memory_current_summary"},
                    )
                )

        top_lines = _render_entry_lines(pack.l2_top_of_mind)
        if top_lines:
            fragments.append(
                PromptFragment(
                    section="memory",
                    title="Top Of Mind",
                    content="\n".join(top_lines),
                    priority=56,
                    metadata={"block_id": "memory_top_of_mind"},
                )
            )

        active_lines = _render_entry_lines(pack.l2_active_entries)
        if active_lines:
            fragments.append(
                PromptFragment(
                    section="memory",
                    title="Active Memory",
                    content="\n".join(active_lines),
                    priority=57,
                    metadata={"block_id": "memory_active_entries"},
                )
            )
        return fragments


def _render_entry_lines(entries) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        rendered = entry.rendered.strip() or entry.summary.strip() or entry.title.strip()
        if not rendered:
            continue
        label = entry.title.strip() or entry.entry_id
        lines.append(f"- {label}: {rendered}")
    return lines
