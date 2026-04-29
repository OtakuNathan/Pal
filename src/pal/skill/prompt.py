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
                section="skill_learning",
                title="Skill Learning",
                content=(
                    "Skill is reusable future procedure. Memory is past facts, preferences, or experience.\n"
                    "When the user explicitly asks to use a named skill, call `op_skill_search` first to resolve the skill identity, then `op_skill_inject` if a clear active match is found.\n"
                    "When the user explicitly asks Pal to learn, summarize, sanitize, or import a reusable workflow, call `op_skill_assimilate` first.\n"
                    "`op_skill_assimilate` only creates a candidate; it does not persist anything.\n"
                    "Only call `op_skill_commit` when the user explicitly wants the candidate saved.\n"
                    "If the user teaches a stable fact or a past lesson, write memory instead of skill.\n"
                    "If a past lesson can become a workflow, create a skill candidate rather than committing automatically."
                ),
                priority=72,
                metadata={"module_id": self.module_id, "kind": "skill_learning"},
            )
        ]
