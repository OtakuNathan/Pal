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
                    "Skills are reference manuals for reusable procedures, debugging, review, and platform "
                    "operations. Reuse applicable loaded manuals. When you need a manual and know its ID, "
                    "use inject_skill directly. Otherwise use search_skills with a concise scenario, then "
                    "inject_skill for a useful match. Search results describe manuals; injection loads their contents.\n"
                    "A name mention alone does not require injection. Resolve "
                    "relevant runtime or platform constraints before acting; report a concrete blocker if required "
                    "guidance is unavailable."
                ),
                priority=72,
                metadata={
                    "module_id": self.module_id,
                    "kind": "skill_guide",
                    "prompt_target": "developer",
                },
            ),
        ]
