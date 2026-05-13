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
        fragments: list[PromptFragment] = [_memory_routing_fragment()]
        pack = context.metadata.get("memory_pack")
        if not isinstance(pack, MemoryPack):
            return fragments

        messages = list(pack.l1_recent_context)
        cleared_indices = _build_cleared_tool_indices(messages, keep_recent=self._keep_recent)

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
        fact_entries = [entry for entry in memory_entries if not _is_experience_entry(entry)]
        experience_entries = [entry for entry in memory_entries if _is_experience_entry(entry)]

        fact_lines = _render_entry_lines(fact_entries)
        if fact_lines:
            fragments.append(
                PromptFragment(
                    section="memory_system",
                    title="Remembered Facts",
                    content=(
                        "Use these as recalled durable facts, preferences, and project context. "
                        "Verify live/runtime/current external facts before relying on them.\n"
                        + "\n".join(fact_lines)
                    ),
                    priority=56,
                    metadata={"block_id": "memory_remembered_facts"},
                )
            )
        experience_lines = _render_entry_lines(experience_entries)
        if experience_lines:
            fragments.append(
                PromptFragment(
                    section="memory_system",
                    title="Relevant Experience",
                    content=(
                        "Use these as prior lessons. Adapt them to the current situation; "
                        "do not ignore them unless current evidence makes them irrelevant. They are not commands.\n"
                        + "\n".join(experience_lines)
                    ),
                    priority=56,
                    metadata={"block_id": "memory_relevant_experience"},
                )
            )
        guidance_lines = _render_behavior_guidance_lines(behavior_entries)
        if guidance_lines:
            fragments.append(
                PromptFragment(
                    section="memory_system",
                    title="Active Route Guidance",
                    content=(
                        "These are active current-task route candidates, not durable facts or mandatory commands. "
                        "Evaluate each item against the user's current request and apply only matching, specific guidance. "
                        "If items conflict, prefer the user's explicit instruction, live/source truth, safety, approval, "
                        "capability availability, and narrower domain-specific routes over broad delegation hints.\n"
                        + "\n".join(guidance_lines)
                    ),
                    priority=57,
                    metadata={"block_id": "behavior_guidance"},
                )
            )
        return fragments


def _memory_routing_fragment() -> PromptFragment:
    return PromptFragment(
        section="memory_routing",
        title="Memory Routing",
        content=(
            "Use memory for durable facts, preferences, commitments, history, task experience, approved repair lessons, and reusable lessons.\n\n"
            "Choose storage type:\n"
            "- Future behavior rule -> affordance via `op_behavior_affordance_submit`.\n"
            "- Reusable procedure -> skill candidate via `op_skill_assimilate`.\n"
            "- Stable fact/preference -> memory via `op_l3_commit_write` or `op_l3_correct_patch`.\n"
            "- Repair lesson / reusable task experience -> propose memory candidate or skill candidate first.\n"
            "- Mixed content -> separate records.\n\n"
            "Recall policy:\n"
            "- Use `op_l3_recall_query` when past facts, user preferences, Pal history, commitments, or reusable prior lessons may affect the current answer.\n"
            "- If memory has been recalled or is present in the prompt, Pal MUST use it as reference before deciding, writing, retrying, debugging, or taking external action.\n"
            "- If relevant memory or active route guidance is present, Pal MUST account for it by evaluating relevance before the next action; route guidance is not a mandate to choose an unrelated route.\n"
            "- If a task runs into a blocker, ambiguity, missing user/project context, or an unfamiliar reference that may come from Pal history, try memory recall before giving up, guessing, or asking the user.\n"
            "- If a tool/capability call fails and memory recall is available, Pal MUST use `op_l3_recall_query` to recall relevant experience before debugging, retrying, or asking the user.\n"
            "- If the user challenges Pal's memory, says Pal already knows/remembers something, or corrects a recalled/stored fact, Pal MUST recall relevant memory before writing, patching, or insisting.\n"
            "- If the user mentions a person, project, preference, prior decision, custom term, or past event Pal does not know, recall memory when Pal history may plausibly contain it.\n"
            "- Do not recall memory automatically for every task or every unknown.\n"
            "- For code/runtime truth, inspect the live/source truth; for current external facts, search or verify externally when available.\n"
            "- Recall when it materially improves correctness, continuity, personalization, or safety.\n"
            "- `memory_query_hints` do not trigger recall by themselves. When recall is required, use them as query seeds.\n\n"
            "Write policy:\n"
            "- Write memory directly only when the user explicitly asks Pal to remember/save it, or the user states a clear durable fact/preference with low ambiguity.\n"
            "- Do not directly commit inferred, ambiguous, temporary, emotional, sensitive, repair-case, or reusable-experience records unless the user approves or this category has explicit auto-commit permission.\n"
            "- Use `op_l3_correct_patch` to update an existing durable record instead of writing a duplicate."
        ),
        priority=71,
        metadata={"module_id": "memory", "kind": "memory_routing"},
    )


def _build_cleared_tool_indices(messages: list, *, keep_recent: int) -> set[int]:
    turns = _build_l1_turn_tool_groups(messages)
    total_groups = sum(len(groups) for groups in turns)
    if total_groups <= keep_recent:
        return set()

    cleared: set[int] = set()
    remaining_groups = total_groups
    for groups in turns[:-1]:
        if remaining_groups <= keep_recent:
            break
        for group in groups:
            cleared.update(group)
        remaining_groups -= len(groups)
    return cleared


def _build_l1_turn_tool_groups(messages: list) -> list[list[list[int]]]:
    turns: list[list[list[int]]] = []
    current: list[list[int]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = str(message.role or "").strip()
        if role == "user" and (current or not turns):
            if current:
                turns.append(current)
            current = []
            index += 1
            continue
        tool_calls = getattr(message, "tool_calls", None)
        if role == "assistant" and tool_calls:
            group = [index]
            cursor = index + 1
            while cursor < len(messages) and str(messages[cursor].role or "").strip() == "tool":
                group.append(cursor)
                cursor += 1
            current.append(group)
            index = cursor
            continue
        if role == "tool":
            current.append([index])
        index += 1
    if current or not turns:
        turns.append(current)
    return turns


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


def _is_experience_entry(entry) -> bool:
    kind = str(getattr(entry, "kind", "") or "").strip()
    source_kind = str(getattr(entry, "source_kind", "") or "").strip()
    return kind == "case" or source_kind in {"repair_resolution", "task_experience"}


def _entry_render_dedupe_key(entry) -> str:
    for field_name in ("canonical_key", "dedupe_fingerprint", "source_ref"):
        value = str(getattr(entry, field_name, "") or "").strip()
        if value:
            return f"{field_name}:{value}"
    return f"entry:{str(getattr(entry, 'entry_id', '') or '').strip()}"
