from __future__ import annotations

from dataclasses import dataclass

from pal.identity.service import IdentityService
from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider
from pal.shared.prompt_dates import today_for_timezone


@dataclass
class IdentityPromptFragmentProvider(PromptFragmentProvider):
    service: IdentityService
    provider_id: str = "identity.prompt.default"
    module_id: str = "identity"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        persona = self.service.get_persona()
        preferences = self.service.get_preferences()
        lines: list[str] = []
        if persona is not None:
            lines.append(f"Name: {persona.display_name}")
            lines.append(f"Language: {persona.language}")
            if persona.vibe:
                lines.append(f"Vibe: {persona.vibe}")
            if persona.tone:
                lines.append(f"Tone: {persona.tone}")
            if preferences is not None and preferences.style_preference:
                lines.append(f"Style: {preferences.style_preference}")
            if persona.core_policy:
                lines.append("Core policy:")
                lines.extend(f"- {item}" for item in persona.core_policy)
        if preferences is not None and preferences.timezone:
            lines.append(f"Timezone: {preferences.timezone}")
        lines.append(f"Today's date is {today_for_timezone(preferences.timezone if preferences is not None else None)}.")
        if preferences is not None and preferences.preferences_blob:
            lines.append("User preferences:")
            for key, value in sorted(preferences.preferences_blob.items()):
                rendered_value = str(value).strip()
                if rendered_value:
                    lines.append(f"- {key}: {rendered_value}")
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
