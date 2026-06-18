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
                    "Use skills for reusable workflows, review methods, debugging methods, platform procedures, or task playbooks.\n"
                    "Do not use skills for durable facts, ordinary preferences, current runtime state, or one-off route hints.\n"
                    "Activated skills guide execution only for the matched task; they do not override the user's current request, hard policy, source-of-truth requirements, capability policy, or mutation boundaries.\n\n"
                    "Skill persistence:\n"
                    "- `skill_assimilate` creates a candidate only. It does not persist anything.\n"
                    "- `skill_commit` persists an explicit candidate when the user asks to save it."
                ),
                priority=72,
                metadata={"module_id": self.module_id, "kind": "skill_guide"},
            ),
            PromptFragment(
                section="skill_guide",
                title="Skill Guidance",
                content=(
                    "Use skills: explicit named skill request -> skill_search; clear active match -> skill_inject before applying. Search alone is not using a skill. Advisor skill refs are candidates, not automatic injects.\n"
                    "Learn skills: explicit learn/summarize/sanitize/import/reusable-procedure request -> skill_assimilate. Explicit save candidate -> skill_commit; pass candidate_id when provided. Do not commit for candidate/draft/summary/review only."
                ),
                priority=72,
                metadata={
                    "module_id": self.module_id,
                    "kind": "skill_guidance",
                    "prompt_target": "runtime_reminder",
                    "source_priority": 72,
                },
            ),
        ]
