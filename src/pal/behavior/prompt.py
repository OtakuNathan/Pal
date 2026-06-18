from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.behavior.contracts import AffordanceDescriptor
from pal.shared import PromptAssemblyContext, PromptFragment

if TYPE_CHECKING:
    from pal.behavior.service import BehaviorService


def _affordance_prompt_line(item: AffordanceDescriptor) -> str:
    title = item.title.strip()
    hint = (item.prompt_hint.strip() or item.scenario_text.strip()).strip()
    if title and hint:
        return f"- {title}: {hint}"
    if title:
        return f"- {title}"
    if hint:
        return f"- {hint}"
    return ""


def _render_affordance_lines(affordances: tuple[AffordanceDescriptor, ...]) -> list[str]:
    lines: list[str] = []
    seen_lines: set[str] = set()
    for item in affordances:
        line = _affordance_prompt_line(item)
        if not line:
            continue
        dedupe_key = line.casefold()
        if dedupe_key in seen_lines:
            continue
        seen_lines.add(dedupe_key)
        lines.append(line)
    return lines


@dataclass
class BehaviorPromptFragmentProvider:
    service: BehaviorService
    provider_id: str = "behavior.prompt.default"
    module_id: str = "behavior"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        fragments = [
            PromptFragment(
                section="task_flow",
                title="Task Flow",
                content=(
                    "1. Simple/current-context sufficient -> answer directly.\n"
                    "2. Clear single-capability action -> resolve contract if needed, then call it.\n"
                    "3. Ambiguous/risky/multi-step/unfamiliar/design/debug/recovery/route-unclear -> call behavior_advise; treat advisor output as route resources, not orders.\n"
                    "4. Skip advisor when route is already established, user gives a clear direct implementation command, or the failure is an obvious local/schema/input mistake. For recovery with likely Pal/project/capability history, targeted memory recall before retry."
                ),
                priority=70,
                metadata={
                    "module_id": self.module_id,
                    "kind": "task_flow",
                    "prompt_target": "runtime_reminder",
                    "source_priority": 70,
                },
            ),
            PromptFragment(
                section="behavior_guidance_guide",
                title="Behavior Guidance Guide",
                content=(
                    "Behavior guidance answers: \"When this situation appears, what route should Pal consider?\"\n\n"
                    "Use behavior guidance for future routing rules and recurring decision hints.\n"
                    "It is not durable factual memory and not a step-by-step procedure.\n\n"
                    "For normal reasoning, treat it as behavior guidance; internal IDs are runtime metadata.\n\n"
                    "Use behavior_save only when the user explicitly asks Pal to adopt/follow/save a future behavior rule, or clearly teaches a durable routing preference.\n"
                    "When the user asks to update existing behavior guidance, Pal MUST call behavior_affordance_update; pass the original rendered guidance line as affordance, not an internal ID.\n"
                    "When replacing or editing the text shown in the rendered behavior guidance block, set prompt_hint to the new guidance text. Use scenario_text only when the user explicitly asks to change the activation scenario.\n"
                    "When writing prompt_hint, provide only the hint body; do not repeat the title as a prefix.\n"
                    "When the user asks to delete existing behavior guidance, Pal MUST call behavior_affordance_delete; pass the original rendered guidance line as affordance.\n"
                    "Do not claim behavior guidance was updated or deleted unless the tool result confirms it.\n"
                    "Injected/plugin behavior guidance is read-only through these tools.\n"
                    "Do not save ordinary facts, preferences, runtime state, or reusable procedures as behavior guidance."
                ),
                priority=72,
                metadata={"module_id": self.module_id, "kind": "behavior_guidance_guide"},
            ),
        ]
        resident = self._resident_affordance_fragment()
        if resident is not None:
            fragments.append(resident)
        return fragments

    def _resident_affordance_fragment(self) -> PromptFragment | None:
        lines = _render_affordance_lines(self.service.resident_affordances())
        if not lines:
            return None
        return PromptFragment(
            section="resident_affordances",
            title="Resident Affordances",
            content="\n".join(lines),
            priority=75,
            metadata={
                "module_id": self.module_id,
                "kind": "resident_affordances",
                "prompt_target": "runtime_reminder",
                "source_priority": 75,
            },
        )


@dataclass
class DeclaredResidentAffordancePromptFragmentProvider:
    module_id: str
    affordances: tuple[AffordanceDescriptor, ...]

    @property
    def provider_id(self) -> str:
        return declared_resident_affordance_provider_id(self.module_id)

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        sorted_affordances = tuple(
            sorted(
                self.affordances,
                key=lambda item: (item.priority, item.title, item.affordance_id),
            )
        )
        lines = _render_affordance_lines(sorted_affordances)
        if not lines:
            return []
        return [
            PromptFragment(
                section="resident_affordances",
                title="Resident Affordances",
                content="\n".join(lines),
                priority=75,
                metadata={
                    "module_id": self.module_id,
                    "kind": "declared_resident_affordances",
                    "prompt_target": "runtime_reminder",
                    "source_priority": 75,
                },
            )
        ]


def declared_resident_affordance_provider_id(module_id: str) -> str:
    return f"behavior.prompt.declared_resident.{module_id}"
