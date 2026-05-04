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
                    "1. Casual chat\n"
                    "- For casual conversation, greetings, simple reactions, or short replies that need no facts, files, tools, memory, design judgment, or external/runtime state, answer directly.\n"
                    "- Code, system, design, debugging, configuration, project, or memory-related requests are not casual chat.\n\n"
                    "2. Direct capability task\n"
                    "- If the task can be completed by exactly one capability, either already known from current context or found with `op_exec_disc_search`, read/resolve the capability contract when needed, then call that capability.\n"
                    "- Do not use this route when the capability call would only be a step toward analysis, diagnosis, design, research, repair, or code understanding.\n"
                    "- Do not ask the advisor to re-decide an already clear single-capability route.\n\n"
                    "3. Advisor Gate\n"
                    "- Otherwise, for code understanding, debugging, design, review, refactor, research, configuration, plugin/runtime investigation, repair, multi-step, risky, unclear, or user-specific work, call `op_behavior_advise` before reading files or taking action.\n"
                    "- \"Look at/read/check the code\" is not a direct capability task when the goal is understanding, diagnosis, design, review, or change.\n"
                    "- Skip advisor only when direct capability applies, or an active route, injected skill, or current conversation already gives the route.\n\n"
                    "4. Advice result\n"
                    "- If advice returns `skill_ref`, call `op_skill_inject` before executing that workflow.\n"
                    "- If advice returns `capability_refs`, resolve them against current capability inventory before relying on them.\n"
                    "- `memory_query_hints` do not trigger recall by themselves. When recall is required, use them as query seeds.\n\n"
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
