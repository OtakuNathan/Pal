from __future__ import annotations

from dataclasses import dataclass

from pal.memory.contracts import MemoryPack
from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider

_KEEP_RECENT_TOOL_MESSAGES = 10


@dataclass
class MemoryPromptFragmentProvider(PromptFragmentProvider):
    provider_id: str = "memory.prompt.default"
    module_id: str = "memory"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        pack = context.metadata.get("memory_pack")
        if not isinstance(pack, MemoryPack):
            return []

        messages = list(pack.l1_recent_context)
        cleared_indices = _build_cleared_tool_indices(messages, keep_recent=_KEEP_RECENT_TOOL_MESSAGES)

        fragments: list[PromptFragment] = []
        block_index = 0
        for i, message in enumerate(messages):
            role = str(message.role or "").strip()
            content = str(message.content or "").strip()
            tool_calls = getattr(message, "tool_calls", None)
            tool_call_id = getattr(message, "tool_call_id", None)
            tool_trace = getattr(message, "tool_trace", None)

            if i in cleared_indices:
                if role == "tool":
                    fragments.append(
                        PromptFragment(
                            section="memory",
                            title="Recent Context",
                            content="[old tool result cleared]",
                            priority=40 + block_index,
                            metadata={
                                "block_id": f"l1_recent_context_{block_index}",
                                "role": "tool",
                                "tool_call_id": tool_call_id,
                            },
                        )
                    )
                    block_index += 1
                elif role == "assistant" and tool_calls:
                    fragments.append(
                        PromptFragment(
                            section="memory",
                            title="Recent Context",
                            content=content,
                            priority=40 + block_index,
                            metadata={
                                "block_id": f"l1_recent_context_{block_index}",
                                "role": "assistant",
                                "tool_calls": tool_calls,
                            },
                        )
                    )
                    block_index += 1
                continue

            if role == "tool" and (content or tool_call_id):
                fragments.append(
                    PromptFragment(
                        section="memory",
                        title="Recent Context",
                        content=content,
                        priority=40 + block_index,
                        metadata={
                            "block_id": f"l1_recent_context_{block_index}",
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                        },
                    )
                )
                block_index += 1
            elif role == "assistant" and tool_calls:
                fragments.append(
                    PromptFragment(
                        section="memory",
                        title="Recent Context",
                        content=content,
                        priority=40 + block_index,
                        metadata={
                            "block_id": f"l1_recent_context_{block_index}",
                            "role": "assistant",
                            "tool_calls": tool_calls,
                        },
                    )
                )
                block_index += 1
            elif role in {"user", "assistant"} and content:
                rendered_content = content
                if role == "assistant" and tool_trace:
                    rendered_content = f"{content}\n\n<system-reminder>Tools used: {tool_trace}</system-reminder>"
                fragments.append(
                    PromptFragment(
                        section="memory",
                        title="Recent Context",
                        content=rendered_content,
                        priority=40 + block_index,
                        metadata={
                            "block_id": f"l1_recent_context_{block_index}",
                            "role": role,
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


def _build_cleared_tool_indices(messages: list, *, keep_recent: int) -> set[int]:
    groups: list[list[int]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = str(message.role or "").strip()
        tool_calls = getattr(message, "tool_calls", None)
        if role == "assistant" and tool_calls:
            group = [index]
            cursor = index + 1
            while cursor < len(messages) and str(messages[cursor].role or "").strip() == "tool":
                group.append(cursor)
                cursor += 1
            groups.append(group)
            index = cursor
            continue
        if role == "tool":
            groups.append([index])
        index += 1
    if len(groups) <= keep_recent:
        return set()
    clear_from = max(0, len(groups) - keep_recent)
    return {item for group in groups[:clear_from] for item in group}


def _render_entry_lines(entries) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        rendered = entry.rendered.strip() or entry.summary.strip() or entry.title.strip()
        if not rendered:
            continue
        label = entry.title.strip() or entry.entry_id
        lines.append(f"- {label}: {rendered}")
    return lines
