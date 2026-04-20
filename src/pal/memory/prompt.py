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
        block_index = 0
        for message in pack.l1_recent_context:
            role = str(message.role or "").strip()
            content = str(message.content or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            fragments.append(
                PromptFragment(
                    section="memory",
                    title="Recent Context",
                    content=content,
                    priority=40 + block_index,
                    metadata={
                        "block_id": f"l1_recent_context_{block_index}",
                        "role": role,
                    },
                )
            )
            block_index += 1
            tool_trace = getattr(message, "tool_trace", None)
            if role == "assistant" and tool_trace:
                fragments.append(
                    PromptFragment(
                        section="memory",
                        title="Recent Context",
                        content=f"Tools used: {tool_trace}",
                        priority=40 + block_index,
                        metadata={
                            "block_id": f"l1_recent_context_{block_index}",
                            "role": "user",
                        },
                    )
                )
                block_index += 1

        if pack.current_summary is not None:
            summary_text = pack.current_summary.rendered.strip() or pack.current_summary.summary.strip()
            if summary_text:
                fragments.append(
                    PromptFragment(
                        section="memory_system",
                        title="Current Summary",
                        content=summary_text,
                        priority=55,
                        metadata={"block_id": "memory_current_summary"},
                    )
                )

        hot_lines = _render_entry_lines(pack.l2_working_memory)
        if hot_lines:
            fragments.append(
                PromptFragment(
                    section="memory_system",
                    title="Working Memory",
                    content="\n".join(hot_lines),
                    priority=56,
                    metadata={"block_id": "memory_working_memory"},
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
