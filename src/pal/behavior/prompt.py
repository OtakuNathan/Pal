from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pal.behavior.contracts import AffordanceDescriptor
from pal.shared import PromptAssemblyContext, PromptFragment

if TYPE_CHECKING:
    from pal.behavior.service import BehaviorService


@dataclass
class BehaviorPromptFragmentProvider:
    service: BehaviorService
    provider_id: str = "behavior.prompt.default"
    module_id: str = "behavior"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        fragments = [
            PromptFragment(
                section="behavior_routing",
                title="Behavior Routing",
                content=(
                    "When Pal needs to act:\n"
                    "1. Answer-only task\n"
                    "- If no tool is needed, answer directly.\n\n"
                    "2. Obvious single-capability task\n"
                    "- If the needed capability is obvious, inspect/search capability inventory directly with `op_exec_disc_search`.\n"
                    "- Resolve the capability before using it.\n"
                    "- Execute only through capability calls.\n\n"
                    "3. Non-trivial workflow\n"
                    "- Advisor Gate: if the task is risky, unclear, user-specific, external-state-dependent, multi-step, or involves research/coding/configuration/system state, call `op_behavior_advise` unless an active prompt rule, injected skill, affordance, hot memory, or conversation context already gives a clear route.\n\n"
                    "4. Advice result\n"
                    "- If advice returns `skill_ref`, call `op_skill_inject` before executing that workflow.\n"
                    "- If advice returns `capability_refs`, resolve them against current capability inventory before relying on them.\n"
                    "- Treat `memory_query_hints` as suggestions only; they do not automatically recall memory.\n\n"
                    "5. Execution\n"
                    "- Execute only through capability calls.\n"
                    "- Respect capability policy, approval requirements, availability, and verification.\n"
                    "- Prefer the simplest viable action path.\n\n"
                    "If the user teaches a future behavior rule, submit an affordance with `op_behavior_affordance_submit`."
                ),
                priority=70,
                metadata={"module_id": self.module_id, "kind": "behavior_routing"},
            ),
        ]
        resident = self._resident_affordance_fragment()
        if resident is not None:
            fragments.append(resident)
        return fragments

    def _resident_affordance_fragment(self) -> PromptFragment | None:
        lines = []
        for item in self.service.resident_affordances():
            hint = item.prompt_hint.strip() or item.scenario_text.strip()
            if not hint:
                continue
            lines.append(f"- {item.title}: {hint}")
        if not lines:
            return None
        return PromptFragment(
            section="resident_affordances",
            title="Resident Affordances",
            content="\n".join(lines),
            priority=75,
            metadata={"module_id": self.module_id, "kind": "resident_affordances"},
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
        lines = []
        for item in sorted(self.affordances, key=lambda item: (item.priority, item.title, item.affordance_id)):
            hint = item.prompt_hint.strip() or item.scenario_text.strip()
            if not hint:
                continue
            lines.append(f"- {item.title}: {hint}")
        if not lines:
            return []
        return [
            PromptFragment(
                section="resident_affordances",
                title="Resident Affordances",
                content="\n".join(lines),
                priority=75,
                metadata={"module_id": self.module_id, "kind": "declared_resident_affordances"},
            )
        ]


def declared_resident_affordance_provider_id(module_id: str) -> str:
    return f"behavior.prompt.declared_resident.{module_id}"
