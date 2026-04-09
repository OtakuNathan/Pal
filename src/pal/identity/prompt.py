from __future__ import annotations

from dataclasses import dataclass

from pal.identity.service import IdentityService
from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider


@dataclass
class IdentityPromptFragmentProvider(PromptFragmentProvider):
    service: IdentityService
    provider_id: str = "identity.prompt.default"
    module_id: str = "identity"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        persona = self.service.get_persona()
        preferences = self.service.get_preferences()
        state = self.service.get_state()
        lines: list[str] = []
        if persona is not None:
            lines.append(f"Name: {persona.display_name}")
            lines.append(f"Language: {persona.language}")
            if persona.vibe:
                lines.append(f"Vibe: {persona.vibe}")
            if persona.tone:
                lines.append(f"Tone: {persona.tone}")
            if persona.style_notes:
                lines.append(f"Style notes: {persona.style_notes}")
            if persona.core_policy:
                lines.append("Core policy:")
                lines.extend(f"- {item}" for item in persona.core_policy)
        if preferences is not None and preferences.timezone:
            lines.append(f"Timezone: {preferences.timezone}")
        if preferences is not None and preferences.language_preference:
            lines.append(f"Preferred language: {preferences.language_preference}")
        if preferences is not None and preferences.style_preference:
            lines.append(f"Style preference: {preferences.style_preference}")
        if preferences is not None and preferences.preferences_blob:
            lines.append("User preferences:")
            for key, value in sorted(preferences.preferences_blob.items()):
                rendered_value = str(value).strip()
                if rendered_value:
                    lines.append(f"- {key}: {rendered_value}")
        if state is not None:
            lines.append(f"Runtime status: {state.status}")
        if not lines:
            return []
        return [
            PromptFragment(
                section="identity",
                title="Pal Identity",
                content="\n".join(lines),
                priority=10,
            )
        ]
