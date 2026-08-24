from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pal.memory.compact import strip_persistent_system_reminders
from pal.memory.contracts import MemoryPack
from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider

@dataclass
class MemoryPromptFragmentProvider(PromptFragmentProvider):
    provider_id: str = "memory.prompt.default"
    module_id: str = "memory"
    include_l1_recent_context: bool = True
    config: Any = field(default=None, repr=False, compare=False)

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        fragments: list[PromptFragment] = list(_memory_guide_fragments())
        pack = context.metadata.get("memory_pack")
        if not isinstance(pack, MemoryPack):
            return fragments

        summary_text = _current_summary_text(pack)
        summary_context = _render_current_summary_context(pack)
        messages = []
        if (
            not bool(context.metadata.get("typed_l1_projection"))
            and self.include_l1_recent_context
        ):
            messages = [
                message
                for message in list(pack.l1_recent_context)
                if not _is_synthetic_compaction_summary(message, summary_text, summary_context)
            ]
        block_index = 0
        i = 0
        while i < len(messages):
            message = messages[i]
            role = str(message.role or "").strip()
            content = str(message.content or "").strip()
            if role == "assistant":
                content = strip_persistent_system_reminders(content)
            tool_calls = getattr(message, "tool_calls", None)
            tool_call_id = getattr(message, "tool_call_id", None)
            if role == "assistant" and tool_calls:
                end = i + 1
                while end < len(messages) and str(messages[end].role or "").strip() == "tool":
                    end += 1
                group = messages[i:end]
                fragments.append(
                    PromptFragment(
                        section="memory",
                        title="Recent Context",
                        content=_render_closed_tool_interaction(group),
                        priority=40 + block_index,
                        metadata={
                            "prompt_target": "user_context",
                            "block_id": f"l1_recent_context_{block_index}",
                            "role": "assistant",
                            "runtime_context_kind": "closed_tool_interaction",
                        },
                    )
                )
                block_index += 1
                i = end
                continue

            if role == "tool" and (content or tool_call_id):
                fragments.append(
                    PromptFragment(
                        section="memory",
                        title="Recent Context",
                        content=_render_orphaned_tool_result(message),
                        priority=40 + block_index,
                        metadata={
                            "prompt_target": "user_context",
                            "block_id": f"l1_recent_context_{block_index}",
                            "role": "assistant",
                            "runtime_context_kind": "closed_tool_interaction",
                        },
                    )
                )
                block_index += 1
            elif role in {"user", "assistant"} and content:
                fragments.append(
                    PromptFragment(
                        section="memory",
                        title="Recent Context",
                        content=content,
                        priority=40 + block_index,
                        metadata={
                            "prompt_target": "user_context",
                            "block_id": f"l1_recent_context_{block_index}",
                            "role": role,
                        },
                    )
                )
                block_index += 1
            i += 1

        if summary_context:
            fragments.append(
                PromptFragment(
                    section="memory",
                    title="Conversation summary",
                    content=summary_context,
                    priority=55,
                    metadata={
                        "prompt_target": "user_context",
                        "block_id": "memory_current_summary",
                        "raw_user_context": True,
                        "runtime_context_kind": "conversation_summary",
                    },
                )
            )

        memory_entries = [entry for entry in pack.l2_working_memory if _is_recalled_memory_entry(entry)]
        memory_lines = _render_memory_entry_lines(memory_entries)
        if memory_lines:
            fragments.append(
                PromptFragment(
                    section="memory",
                    title="Recalled memories",
                    content=_render_recalled_memories_context(memory_lines),
                    priority=56,
                    metadata={
                        "prompt_target": "user_context",
                        "block_id": "memory_recalled_context",
                        "raw_user_context": True,
                        "runtime_context_kind": "memory",
                    },
                )
            )
        return fragments


def _render_closed_tool_interaction(messages: list) -> str:
    assistant = messages[0]
    assistant_text = str(getattr(assistant, "content", "") or "").strip()
    tool_calls = [
        dict(item)
        for item in list(getattr(assistant, "tool_calls", None) or ())
        if isinstance(item, dict)
    ]
    lines = [
        "<closed_tool_interaction>",
        "Historical tool evidence from a completed model turn; this is not an active tool protocol.",
    ]
    if assistant_text:
        lines.append(f"assistant_note: {assistant_text}")
    lines.append(
        "tool_calls: "
        + json.dumps(tool_calls, ensure_ascii=False, sort_keys=True)
    )
    for message in messages[1:]:
        call_id = str(getattr(message, "tool_call_id", "") or "").strip()
        result = str(getattr(message, "content", "") or "")
        lines.append(f"tool_result[{call_id}]: {result}")
    lines.append("</closed_tool_interaction>")
    return "\n".join(lines)


def _render_orphaned_tool_result(message) -> str:
    call_id = str(getattr(message, "tool_call_id", "") or "").strip()
    content = str(getattr(message, "content", "") or "")
    return (
        "<closed_tool_interaction>\n"
        "Historical tool evidence from a completed model turn; this is not an active tool protocol.\n"
        f"tool_result[{call_id}]: {content}\n"
        "</closed_tool_interaction>"
    )


def _memory_guide_fragments() -> tuple[PromptFragment, ...]:
    return (
        PromptFragment(
            section="memory_guide",
            title="Memory Guide",
            content=(
                "Memory is the source of truth for durable user facts, preferences, prior Pal decisions, project history, "
                "repair lessons, and reusable case knowledge. Use memory tools for durable records only; current runtime "
                "state and current external facts require live inspection or external verification.\n\n"
                "When work hits an error, regression, failed repair, repeated pitfall, or unfamiliar debugging situation, "
                "prefer recall_memory with kind=case and concrete error/symptom/fix terms before inventing a new repair.\n\n"
                "Boundary: memory answers \"what should Pal remember as true or reusable knowledge?\" Behavior guidance "
                "answers \"when this situation appears, what route/action should Pal consider?\" If the user teaches a "
                "future route trigger, condition, or recurring decision rule, use behavior guidance instead of memory. "
                "Reusable procedures/playbooks belong to the skill system, not memory.\n\n"
                "Memory tool descriptions define the recall, de-duplication, update, and delete procedures. Recalled "
                "mem_ref values are opaque; prefixes such as fact: and case: are part of the ref."
            ),
            priority=71,
            metadata={
                "module_id": "memory",
                "kind": "memory_guide",
                "prompt_target": "system",
            },
        ),
    )


def _current_summary_text(pack: MemoryPack) -> str:
    if pack.current_summary is None:
        return ""
    return pack.current_summary.summary.strip()


def _render_current_summary_context(pack: MemoryPack) -> str:
    if pack.current_summary is None:
        return ""
    entry = pack.current_summary
    summary = entry.summary.strip()
    text = entry.rendered.strip() or summary
    if text.startswith("<conversation_summary") or text.startswith("<compact_context"):
        return text
    return _render_conversation_summary_context(text)


def _is_synthetic_compaction_summary(message, *summary_texts: str) -> bool:
    candidates = {str(item or "").strip() for item in summary_texts if str(item or "").strip()}
    if not candidates:
        return False
    if str(getattr(message, "role", "") or "").strip() != "assistant":
        return False
    content = str(getattr(message, "content", "") or "").strip()
    return content in candidates


def _render_memory_entry_lines(entries) -> list[str]:
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
        mem_ref = _entry_mem_ref(entry)
        if not mem_ref:
            continue
        lines.append(f"[{mem_ref}]: {rendered}")
    return lines


def _render_recalled_memories_context(lines: list[str]) -> str:
    return "<recalled_memories view=\"summary\">\n" + "\n".join(lines) + "\n</recalled_memories>"


def _render_conversation_summary_context(summary_text: str) -> str:
    return "<conversation_summary>\n" + summary_text.strip() + "\n</conversation_summary>"


def _is_recalled_memory_entry(entry) -> bool:
    return str(getattr(entry, "scope", "") or "").strip() != "behavior"


def _entry_render_dedupe_key(entry) -> str:
    for field_name in ("canonical_key", "dedupe_fingerprint", "source_ref"):
        value = str(getattr(entry, field_name, "") or "").strip()
        if value:
            return f"{field_name}:{value}"
    return f"entry:{str(getattr(entry, 'entry_id', '') or '').strip()}"


def _entry_mem_ref(entry) -> str:
    source_ref = str(getattr(entry, "source_ref", "") or "").strip()
    if source_ref:
        return source_ref
    return str(getattr(entry, "entry_id", "") or "").strip()
