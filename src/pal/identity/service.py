from __future__ import annotations

from dataclasses import dataclass, field

from pal.identity.contracts import (
    IdentityServicePort,
    PalPersonaProfile,
    PalPreferencesProfile,
)
from pal.identity.repository import IdentityRepository


@dataclass
class IdentityService(IdentityServicePort):
    repository: IdentityRepository
    _persona_projection: PalPersonaProfile | None = field(default=None, init=False, repr=False)
    _preferences_projection: PalPreferencesProfile | None = field(default=None, init=False, repr=False)
    _projection_loaded: bool = field(default=False, init=False, repr=False)

    def ensure_defaults(self) -> None:
        self.repository.ensure_defaults()
        self.refresh_projection()

    def get_persona(self) -> PalPersonaProfile | None:
        self._ensure_projection()
        return _copy_persona(self._persona_projection)

    def get_preferences(self) -> PalPreferencesProfile | None:
        self._ensure_projection()
        return _copy_preferences(self._preferences_projection)

    def refresh_projection(
        self,
    ) -> tuple[PalPersonaProfile | None, PalPreferencesProfile | None]:
        persona = self.repository.get_persona()
        preferences = self.repository.get_user_preferences()
        self._persona_projection = (
            PalPersonaProfile(
                persona_id=persona.persona_id,
                display_name=persona.display_name,
                language=persona.language,
                vibe=persona.vibe,
                tone=persona.tone,
                core_policy=list(persona.core_policy or []),
            )
            if persona is not None
            else None
        )
        self._preferences_projection = (
            PalPreferencesProfile(
                preference_id=preferences.preference_id,
                style_preference=preferences.style_preference,
                timezone=preferences.timezone,
                preferences_blob=dict(preferences.preferences_blob or {}),
            )
            if preferences is not None
            else None
        )
        self._projection_loaded = True
        return self.get_persona(), self.get_preferences()

    def update_preferences(self, *, timezone: str | None = None) -> PalPreferencesProfile:
        self.repository.update_user_preferences(timezone=timezone)
        _, preferences = self.refresh_projection()
        if preferences is None:
            raise RuntimeError("identity preferences projection is unavailable after update")
        return preferences

    def _ensure_projection(self) -> None:
        if not self._projection_loaded:
            self.refresh_projection()


def _copy_persona(value: PalPersonaProfile | None) -> PalPersonaProfile | None:
    if value is None:
        return None
    return PalPersonaProfile(
        persona_id=value.persona_id,
        display_name=value.display_name,
        language=value.language,
        vibe=value.vibe,
        tone=value.tone,
        core_policy=list(value.core_policy),
    )


def _copy_preferences(value: PalPreferencesProfile | None) -> PalPreferencesProfile | None:
    if value is None:
        return None
    return PalPreferencesProfile(
        preference_id=value.preference_id,
        style_preference=value.style_preference,
        timezone=value.timezone,
        preferences_blob=dict(value.preferences_blob),
    )
