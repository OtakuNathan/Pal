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
        identity_lines: list[str] = []
        if persona is not None:
            identity_lines.append(f"Name: {persona.display_name}")
            identity_lines.append(f"Language: {persona.language}")
            if persona.vibe:
                identity_lines.append(f"Vibe: {persona.vibe}")
            if persona.tone:
                identity_lines.append(f"Tone: {persona.tone}")
            if preferences is not None and preferences.style_preference:
                identity_lines.append(f"Style: {preferences.style_preference}")
            if persona.core_policy:
                identity_lines.append("Core policy:")
                identity_lines.extend(f"- {item}" for item in persona.core_policy)
        if preferences is not None and preferences.timezone:
            identity_lines.append(f"Timezone: {preferences.timezone}")
        if preferences is not None and preferences.preferences_blob:
            identity_lines.append("User preferences:")
            for key, value in sorted(preferences.preferences_blob.items()):
                rendered_value = str(value).strip()
                if rendered_value:
                    identity_lines.append(f"- {key}: {rendered_value}")
        fragments: list[PromptFragment] = []
        if identity_lines:
            fragments.append(PromptFragment(
                section="identity",
                title="Pal Identity",
                content="\n".join(identity_lines),
                priority=10,
            ))
        fragments.append(PromptFragment(
            section="runtime",
            title="Current Date",
            content=f"Today's date is {today_for_timezone(preferences.timezone if preferences is not None else None)}.",
            priority=10,
            metadata={
                "prompt_target": "runtime_reminder",
                "block_id": "current_date",
            },
        ))
        return fragments
