from __future__ import annotations

from dataclasses import dataclass, field

from pal.identity.service import IdentityService
from pal.shared import PromptAssemblyContext, PromptFragment, PromptFragmentProvider
from pal.shared.prompt_dates import today_for_timezone


@dataclass
class IdentityPromptFragmentProvider(PromptFragmentProvider):
    service: IdentityService
    provider_id: str = "identity.prompt.default"
    module_id: str = "identity"
    _system_identity_content: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        persona = self.service.get_persona()
        lines: list[str] = []
        if persona is not None:
            lines.append(f"Name: {persona.display_name}")
            if persona.core_policy:
                lines.append("Core policy:")
                lines.extend(f"- {item}" for item in persona.core_policy)
        self._system_identity_content = "\n".join(lines)

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        _ = context
        persona = self.service.get_persona()
        preferences = self.service.get_preferences()
        persona_lines: list[str] = []
        if persona is not None:
            persona_lines.append(f"Language: {persona.language}")
            if persona.vibe:
                persona_lines.append(f"Vibe: {persona.vibe}")
            if persona.tone:
                persona_lines.append(f"Tone: {persona.tone}")
            if preferences is not None and preferences.style_preference:
                persona_lines.append(f"Style: {preferences.style_preference}")
        if preferences is not None and preferences.timezone:
            persona_lines.append(f"Timezone: {preferences.timezone}")
        if preferences is not None and preferences.preferences_blob:
            persona_lines.append("User preferences:")
            for key, value in sorted(preferences.preferences_blob.items()):
                rendered_value = str(value).strip()
                if rendered_value:
                    persona_lines.append(f"- {key}: {rendered_value}")
        fragments: list[PromptFragment] = []
        if self._system_identity_content:
            fragments.append(PromptFragment(
                section="identity",
                title="Pal Identity",
                content=self._system_identity_content,
                priority=10,
                metadata={"prompt_target": "system"},
            ))
        if persona_lines:
            fragments.append(PromptFragment(
                section="persona",
                title="Pal Persona Defaults",
                content="\n".join(persona_lines),
                priority=11,
                metadata={"prompt_target": "developer"},
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
