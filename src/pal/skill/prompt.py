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
                title="Skill Use and Skill Learning",
                content=(
                    "Use skills for reusable procedures, not facts.\n\n"
                    "When to search/inject skill:\n"
                    "- If the user explicitly asks to use a named skill, Pal MUST call `op_skill_search` first.\n"
                    "- If a clear active match is found, Pal MUST call `op_skill_inject` before applying that skill.\n"
                    "- Search alone is not using a skill. After a matching active skill is found, inject it before answering from it.\n"
                    "- Do not inject a skill merely because it exists. Inject only when it clearly matches the current task or explicit request.\n\n"
                    "When to learn a skill:\n"
                    "- If the user explicitly asks Pal to learn, summarize, sanitize, import, or turn a workflow into a reusable procedure, Pal MUST call `op_skill_assimilate`.\n"
                    "- `op_skill_assimilate` creates a candidate only. It does not persist anything.\n"
                    "- If the user explicitly wants a skill candidate saved, Pal MUST call `op_skill_commit`.\n"
                    "- When the user provides a candidate_id, pass that candidate_id to `op_skill_commit`; do not ask for the candidate object.\n"
                    "- Do not call `op_skill_commit` when the user asks only for a candidate, draft, summary, or review.\n\n"
                    "Storage boundary:\n"
                    "- Stable facts and preferences belong in memory.\n"
                    "- Future behavior routes belong in affordances.\n"
                    "- Reusable procedures belong in skill candidates.\n"
                    "- If a past lesson can become a workflow, create a skill candidate before committing durable procedure text."
                ),
                priority=72,
                metadata={"module_id": self.module_id, "kind": "skill_learning"},
            )
        ]
