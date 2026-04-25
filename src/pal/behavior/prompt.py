from __future__ import annotations

from dataclasses import dataclass

from pal.behavior.service import BehaviorService
from pal.shared import PromptAssemblyContext, PromptFragment


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
                    'Capability search answers: "what executable ability exists?"\n'
                    'Behavior advice answers: "what route should Pal consider for this scenario?"\n'
                    'Affordance submission records: "when this scenario appears again, what route should Pal consider?"\n\n'
                    "When Pal needs to act:\n"
                    "1. If the required capability is obvious, inspect/search capability inventory directly with `op_exec_disc_search`.\n"
                    "2. Advisor Gate: before starting a non-trivial task (research, coding, config changes, multi-step execution, or any task involving tools, external state, or user-specific procedure), check whether an applicable behavior rule already exists in prompt, hot memory, or conversation. If not, call `op_behavior_advise` before the first substantive action.\n"
                    "3. Do not call advisor for casual conversation, simple one-shot answers, or when the correct workflow is already available.\n"
                    "4. If advice returns a `skill_ref`, call `op_skill_inject` before executing that workflow. A skill is a playbook, not an executable action.\n"
                    "5. If advice returns `capability_refs`, resolve them against current capability inventory before relying on them.\n"
                    "6. Execute only through capability calls, respecting capability policy, approval, availability, and verification."
                ),
                priority=70,
                metadata={"module_id": self.module_id, "kind": "behavior_routing"},
            ),
            PromptFragment(
                section="memory_routing",
                title="Memory Routing",
                content=(
                    "If the user teaches a future behavior, submit an affordance.\n"
                    "If the user teaches a stable fact, preference, or reusable experience, write memory.\n"
                    "If both apply, ask for clarification or create separate records.\n\n"
                    "Use `op_behavior_affordance_submit` only when the user defines a durable behavior-routing rule.\n"
                    "Use `op_l3_recall_query` when past facts, user preferences, Pal history, commitments, or reusable prior lessons may affect the current answer.\n"
                    "Use `op_l3_commit_write` for durable facts, preferences, task experience, repair cases, and reusable solution cases.\n"
                    "Use `op_l3_correct_patch` to update an existing durable record instead of writing a duplicate.\n\n"
                    "Affordance is behavior routing knowledge. Memory is durable factual/task knowledge.\n"
                    "`memory_query_hints` from behavior advice are suggestions only; they do not automatically recall memory."
                ),
                priority=71,
                metadata={"module_id": self.module_id, "kind": "memory_routing"},
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
