from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.memory.contracts import MemoryPack
from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider

if TYPE_CHECKING:
    from pal.core.runtime_config import RuntimeConfig

_DEFAULT_KEEP_RECENT_TOOL_MESSAGES = 10


@dataclass
class MemoryPromptFragmentProvider(PromptFragmentProvider):
    provider_id: str = "memory.prompt.default"
    module_id: str = "memory"
    config: RuntimeConfig | None = None

    @property
    def _keep_recent(self) -> int:
        return getattr(self.config, "keep_recent_tool_messages", _DEFAULT_KEEP_RECENT_TOOL_MESSAGES) if self.config else _DEFAULT_KEEP_RECENT_TOOL_MESSAGES

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        pack = context.metadata.get("memory_pack")
        if not isinstance(pack, MemoryPack):
            return []

        messages = list(pack.l1_recent_context)
        cleared_indices = _build_cleared_tool_indices(messages, keep_recent=self._keep_recent)

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

        behavior_entries = [entry for entry in pack.l2_working_memory if _is_behavior_guidance_entry(entry)]
        memory_entries = [entry for entry in pack.l2_working_memory if not _is_behavior_guidance_entry(entry)]

        hot_lines = _render_entry_lines(memory_entries)
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
        guidance_lines = _render_behavior_guidance_lines(behavior_entries)
        if guidance_lines:
            fragments.append(
                PromptFragment(
                    section="memory_system",
                    title="Behavior Guidance",
                    content=(
                        "These are current-task behavior routing hints, not durable facts. "
                        "Use them to choose workflow, skill injection, capability search, or optional recall.\n"
                        + "\n".join(guidance_lines)
                    ),
                    priority=57,
                    metadata={"block_id": "behavior_guidance"},
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
    seen_keys: set[str] = set()
    for entry in entries:
        dedupe_key = _entry_render_dedupe_key(entry)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        rendered = entry.rendered.strip() or entry.summary.strip() or entry.title.strip()
        if not rendered:
            continue
        if str(getattr(entry, "source_kind", "") or "").strip() == "l3_recall":
            rendered = f"{rendered} [L3 summary; origin available]"
        label = entry.title.strip() or entry.entry_id
        lines.append(f"- {label}: {rendered}")
    return lines


def _render_behavior_guidance_lines(entries) -> list[str]:
    lines: list[str] = []
    seen_keys: set[str] = set()
    for entry in entries:
        dedupe_key = _entry_render_dedupe_key(entry)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        rendered = entry.rendered.strip() or entry.summary.strip() or entry.title.strip()
        if not rendered:
            continue
        label = entry.title.strip() or entry.entry_id
        lines.append(f"- {label}: {rendered}")
    return lines


def _is_behavior_guidance_entry(entry) -> bool:
    kind = str(getattr(entry, "kind", "") or "").strip()
    source_kind = str(getattr(entry, "source_kind", "") or "").strip()
    return kind == "behavior_rule" or source_kind == "behavior_advice"


def _entry_render_dedupe_key(entry) -> str:
    for field_name in ("canonical_key", "dedupe_fingerprint", "source_ref"):
        value = str(getattr(entry, field_name, "") or "").strip()
        if value:
            return f"{field_name}:{value}"
    return f"entry:{str(getattr(entry, 'entry_id', '') or '').strip()}"
