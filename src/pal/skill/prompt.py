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
                    "When to search/inject:\n"
                    "- If the user explicitly asks to use a named skill, Pal MUST call `skill_search` first.\n"
                    "- If a clear active match is found, Pal MUST call `skill_inject` before applying that skill.\n"
                    "- Search alone is not using a skill.\n"
                    "- Do not inject a skill merely because it exists or because an advisor hint lists a skill ref. Inject only when it clearly matches the current task or explicit request.\n\n"
                    "When to learn:\n"
                    "- If the user explicitly asks Pal to learn, summarize, sanitize, import, or turn a workflow into a reusable procedure, Pal MUST call `skill_assimilate`.\n"
                    "- If the user explicitly wants a skill candidate saved, Pal MUST call `skill_commit`.\n"
                    "- When the user provides a candidate_id, pass that candidate_id to `skill_commit`; do not ask for the candidate object.\n"
                    "- Do not call `skill_commit` when the user asks only for a candidate, draft, summary, or review."
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
