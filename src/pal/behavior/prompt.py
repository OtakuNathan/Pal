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
                section="advisor_gate",
                title="Advisor Gate",
                content=(
                    "Use op_behavior_advise for ambiguous, risky, multi-step, unfamiliar, route-unclear, or recovery work.\n\n"
                    "Recovery work includes:\n"
                    "- a capability/tool call failed and the next route is unclear\n"
                    "- the available capability does not seem sufficient to complete the user's goal\n"
                    "- Pal tried the obvious path but did not achieve the intended result\n"
                    "- repeated retries would be guesswork\n\n"
                    "Skip advisor when:\n"
                    "- the route is already established by the current conversation\n"
                    "- the user gives a direct implementation command for a clear, bounded, already-routed action\n"
                    "- the task is a clear single-capability action\n"
                    "- the failure is a simple local/schema/input mistake with an obvious correction"
                ),
                priority=70,
                metadata={"module_id": self.module_id, "kind": "advisor_gate"},
            ),
            PromptFragment(
                section="advisor_recovery_memory",
                title="Advisor Recovery Memory",
                content=(
                    "When op_behavior_advise is used for recovery, debugging, unfamiliar work, or route repair, Pal must consider whether prior experience may help.\n\n"
                    "If advice returns memory_query_hints and the task may depend on prior Pal/project/user history, call op_memory_recall with targeted queries before retrying or choosing a new route.\n\n"
                    "If no memory_query_hints are returned but the failure is Pal-specific, capability-specific, project-specific, or resembles a repeated issue, call op_memory_recall using the failed capability/tool name, error text, and task goal as query seeds.\n\n"
                    "Use a small recall budget:\n"
                    "- limit 3-5\n"
                    "- targeted queries\n"
                    "- usually one recall before retry\n\n"
                    "Do not recall for obvious schema/parameter mistakes with a clear correction."
                ),
                priority=71,
                metadata={"module_id": self.module_id, "kind": "advisor_recovery_memory"},
            ),
            PromptFragment(
                section="behavior_routing",
                title="Behavior Routing",
                content=(
                    "Direct routes:\n"
                    "- Casual chat: for greetings, simple reactions, or short replies that need no facts, files, tools, memory, design judgment, or external/runtime state, answer directly.\n"
                    "- Direct capability task: if the task can be completed by exactly one capability, either already known from current context or found with `op_tool_search`, read/resolve the capability contract when needed, then call that capability.\n"
                    "- Do not use the single-capability route when the capability call would only be a step toward analysis, diagnosis, design, research, repair, or code understanding.\n\n"
                    "Advice result:\n"
                    "- Advice is a resource package, not an order. Evaluate matching `skill_refs`, `capability_refs`, and `memory_query_hints` against the current request.\n"
                    "- MUST NOT call `op_skill_inject` merely because `skill_refs` are present.\n"
                    "- Call `op_skill_inject` only when workflow, domain rules, design, review, repair, or multi-step work needs that guidance.\n"
                    "- If a listed capability directly completes the request, use the capability without injecting a skill.\n"
                    "- If advice returns `capability_refs`, resolve them against current capability inventory before relying on them.\n"
                    "- `memory_query_hints` do not trigger recall by themselves. When recall is required, use them as query seeds.\n\n"
                    "Execution:\n"
                    "- Execute only through capability calls.\n"
                    "- Respect capability policy, approval requirements, availability, and verification.\n"
                    "- Prefer the simplest viable action path.\n\n"
                    "Route map:\n"
                    "- Future, scheduled, recurring, reminder, timer, periodic check, or proactive push work -> Proactive route. Use `op_proactive_mgmt_create` for new proactive tasks and reminders, then configure schedule/output as needed.\n"
                    "- One-shot delegated implementation, research, review, or bounded async worker handoff -> Minion route.\n"
                    "- Immediate answer or immediate single capability action -> direct Pal/capability route."
                ),
                priority=72,
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
