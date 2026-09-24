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

        legacy = not bool(context.metadata.get("typed_l1_projection"))
        summary_context = _render_current_summary_context(pack) if legacy else ""
        messages = []
        if (
            not bool(context.metadata.get("typed_l1_projection"))
            and self.include_l1_recent_context
        ):
            messages = [
                message
                for message in list(pack.l1_recent_context)
                if not _is_synthetic_compaction_summary(message)
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

        if summary_context and not context.metadata.get("typed_l1_projection"):
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
                (
                    "Recall relevant durable records when the task depends on information missing from the current "
                    "context. For recurring failures or past repair decisions, use recall_memory kind=case with "
                    "concrete error, symptom, or fix terms. A first error with sufficient evidence needs no memory "
                    "search. Treat recalled cases as leads and check their applicability before acting.\n"
                    "Memory tools define record maintenance. Preserve returned mem_ref values exactly, including "
                    "prefixes such as fact: and case:."
                )
            ),
            priority=71,
            metadata={
                "module_id": "memory",
                "kind": "memory_guide",
                "prompt_target": "developer",
            },
        ),
    )


def _render_current_summary_context(pack: MemoryPack) -> str:
    if pack.current_summary is None:
        return ""
    entry = pack.current_summary
    summary = entry.summary.strip()
    text = entry.rendered.strip() or summary
    if text.startswith("<conversation_summary") or text.startswith("<compact_context"):
        return text
    return _render_conversation_summary_context(text)


def _is_synthetic_compaction_summary(message) -> bool:
    from pal.memory.continuity import SUMMARY_CONTEXT_KEY
    payload = dict(getattr(message, "payload", {}) or {})
    return str(getattr(message, "kind", "")) == "runtime_context_summary" or (
        str(getattr(message, "kind", "")) == "pal_prompt_context"
        and payload.get("pal_authored") and payload.get("context_key") == SUMMARY_CONTEXT_KEY
    )


def _render_conversation_summary_context(summary_text: str) -> str:
    return "<conversation_summary>\n" + summary_text.strip() + "\n</conversation_summary>"
