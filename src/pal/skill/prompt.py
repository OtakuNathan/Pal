from __future__ import annotations

from dataclasses import dataclass

from pal.shared import PromptAssemblyContext, PromptFragment


@dataclass
class SkillPromptFragmentProvider:
    provider_id: str = "skill.prompt.default"
    module_id: str = "skill"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        return [
            PromptFragment(
                section="skill_guide",
                title="Skill Guide",
                content=(
                    "Skills answer: \"What reusable procedure should Pal follow?\"\n\n"
                    "Skills are reusable workflows, review methods, debugging methods, platform procedures, or task playbooks. "
                    "They are not durable facts, ordinary preferences, current runtime state, or one-off route hints. "
                    "Activated skills guide execution only for the matched task; they do not override the user's current "
                    "request, hard policy, source-of-truth requirements, capability policy, or mutation boundaries. Skill "
                    "tools define search, injection, learning, and persistence procedures."
                ),
                priority=72,
                metadata={"module_id": self.module_id, "kind": "skill_guide"},
            ),
        ]
