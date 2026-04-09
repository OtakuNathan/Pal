from __future__ import annotations

from dataclasses import dataclass

from pal.identity.contracts import (
    IdentityServicePort,
    PalPersonaProfile,
    PalPreferencesProfile,
    PalStateProfile,
)
from pal.identity.repository import IdentityRepository


@dataclass
class IdentityService(IdentityServicePort):
    repository: IdentityRepository

    def ensure_defaults(self) -> None:
        self.repository.ensure_defaults()

    def get_persona(self) -> PalPersonaProfile | None:
        persona = self.repository.get_persona()
        if persona is None:
            return None
        return PalPersonaProfile(
            persona_id=persona.persona_id,
            display_name=persona.display_name,
            language=persona.language,
            vibe=persona.vibe,
            tone=persona.tone,
            style_notes=persona.style_notes,
            core_policy=list(persona.core_policy or []),
        )

    def get_preferences(self) -> PalPreferencesProfile | None:
        preferences = self.repository.get_user_preferences()
        if preferences is None:
            return None
        return PalPreferencesProfile(
            preference_id=preferences.preference_id,
            language_preference=preferences.language_preference,
            style_preference=preferences.style_preference,
            timezone=preferences.timezone,
            preferences_blob=dict(preferences.preferences_blob or {}),
        )

    def get_state(self) -> PalStateProfile | None:
        state = self.repository.get_pal_state()
        if state is None:
            return None
        return PalStateProfile(
            persona_id=state.persona,
            status=state.status,
            top_of_mind_refs=list(state.top_of_mind_refs or []),
            last_active_at=state.last_active_at,
        )

    def update_preferences(self, *, timezone: str | None = None) -> PalPreferencesProfile:
        preferences = self.repository.update_user_preferences(timezone=timezone)
        return PalPreferencesProfile(
            preference_id=preferences.preference_id,
            language_preference=preferences.language_preference,
            style_preference=preferences.style_preference,
            timezone=preferences.timezone,
            preferences_blob=dict(preferences.preferences_blob or {}),
        )
