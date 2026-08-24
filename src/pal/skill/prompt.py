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
                    "boundaries. When a task may benefit from a reusable procedure and no matching manual is already in conversation "
                    "context, call `skill_search` with a concise scenario. Search results are metadata, not activation: evaluate the "
                    "match, then call `skill_inject` only for a useful manual. The injected <skill> block arrives as separate user-role "
                    "reference context after the tool-result batch. Do not inject a skill merely because its name was mentioned, and "
                    "do not inject the same manual twice. Skill tools define learning and persistence procedures separately."
                ),
                priority=72,
                metadata={
                    "module_id": self.module_id,
                    "kind": "skill_guide",
                    "prompt_target": "developer",
                },
            ),
        ]
