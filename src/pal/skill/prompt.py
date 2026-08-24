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
                    "Skills answer: \"What reference manual or reusable procedure may help this task?\"\n\n"
                    "Skills are optional operation manuals, review methods, debugging methods, platform procedures, or task playbooks. "
                    "They are not durable facts, ordinary preferences, current runtime state, behavior guidance, or a replacement "
                    "for bunshin/profile-based delegation. Activated skills guide execution only for the matched task; they do not "
                    "override the user's current request, hard policy, source-of-truth requirements, capability policy, or mutation "
                    "boundaries. Skill tools define search, injection, learning, and persistence procedures."
                ),
                priority=72,
                metadata={
                    "module_id": self.module_id,
                    "kind": "skill_guide",
                    "prompt_target": "system",
                },
            ),
        ]
