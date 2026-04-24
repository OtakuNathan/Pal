from __future__ import annotations

from dataclasses import dataclass

from pal.behavior.service import BehaviorService
from pal.shared import PromptAssemblyContext, PromptFragment


@dataclass
class BehaviorPromptFragmentProvider:
    service: BehaviorService
    provider_id: str = "behavior.resident_affordances"
    module_id: str = "behavior"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        fragments = [
            PromptFragment(
                section="system",
                title="Behavior Tools",
                content=(
                    "- Use op_behavior_advise when you intend to act and want relevant affordances, skills, capability routes, or memory query hints.\n"
                    "- Use op_skill_inject when behavior advice returns a skill_ref and you need the full skill manual.\n"
                    "- Use op_behavior_affordance_submit only when the user explicitly teaches a recurring behavior rule; ordinary task memories belong in memory, not affordance."
                ),
                priority=70,
                metadata={"module_id": self.module_id, "kind": "behavior_tools"},
            )
        ]
        affordances = self.service.resident_affordances()
        if not affordances:
            return fragments
        lines = []
        for item in affordances:
            hint = item.prompt_hint.strip() or item.scenario_text.strip()
            if not hint:
                continue
            lines.append(f"- {item.title}: {hint}")
        if not lines:
            return fragments
        fragments.append(
            PromptFragment(
                section="system",
                title="Resident Affordances",
                content="\n".join(lines),
                priority=75,
                metadata={"module_id": self.module_id, "kind": "resident_affordances"},
            )
        )
        return fragments
