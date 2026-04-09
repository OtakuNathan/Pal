from __future__ import annotations

from typing import Any

from pal.foundation import utc_now
from pal.identity.models import PalPersonaModel, PalStateModel, UserPreferencesModel


DEFAULT_PERSONA_ID = "default"
DEFAULT_PREFERENCE_ID = "default"


class IdentityRepository:
    def ensure_defaults(
        self,
        *,
        display_name: str = "pal",
        language: str = "en",
        vibe: str | None = None,
        tone: str | None = None,
        style_notes: str | None = None,
        core_policy: list[str] | None = None,
        timezone: str | None = None,
    ) -> None:
        now = utc_now()
        PalPersonaModel.get_or_create(
            persona_id=DEFAULT_PERSONA_ID,
            defaults={
                "display_name": display_name,
                "language": language,
                "vibe": vibe,
                "tone": tone,
                "style_notes": style_notes,
                "core_policy": list(core_policy or ()),
                "created_at": now,
                "updated_at": now,
            },
        )

        UserPreferencesModel.get_or_create(
            preference_id=DEFAULT_PREFERENCE_ID,
            defaults={
                "language_preference": language,
                "style_preference": None,
                "timezone": timezone,
                "preferences_blob": {},
                "created_at": now,
                "updated_at": now,
            },
        )

        PalStateModel.get_or_create(
            persona=DEFAULT_PERSONA_ID,
            defaults={
                "status": "idle",
                "top_of_mind_refs": [],
                "created_at": now,
                "updated_at": now,
            },
        )

    def get_persona(self, persona_id: str = DEFAULT_PERSONA_ID) -> PalPersonaModel | None:
        return PalPersonaModel.get_or_none(PalPersonaModel.persona_id == persona_id)

    def get_pal_state(self, persona_id: str = DEFAULT_PERSONA_ID) -> PalStateModel | None:
        return PalStateModel.get_or_none(PalStateModel.persona == persona_id)

    def get_user_preferences(
        self,
        preference_id: str = DEFAULT_PREFERENCE_ID,
    ) -> UserPreferencesModel | None:
        return UserPreferencesModel.get_or_none(UserPreferencesModel.preference_id == preference_id)

    def update_user_preferences(
        self,
        *,
        language_preference: str | None = None,
        style_preference: str | None = None,
        timezone: str | None = None,
        preferences_patch: dict[str, Any] | None = None,
    ) -> UserPreferencesModel:
        now = utc_now()
        preferences = self.get_user_preferences(DEFAULT_PREFERENCE_ID)
        if preferences is None:
            preferences = UserPreferencesModel.create(
                preference_id=DEFAULT_PREFERENCE_ID,
                language_preference=language_preference,
                style_preference=style_preference,
                timezone=timezone,
                preferences_blob=dict(preferences_patch or {}),
                created_at=now,
                updated_at=now,
            )
            return preferences

        merged = dict(preferences.preferences_blob or {})
        merged.update(dict(preferences_patch or {}))
        if language_preference is not None:
            preferences.language_preference = language_preference
        if style_preference is not None:
            preferences.style_preference = style_preference
        if timezone is not None:
            preferences.timezone = timezone
        preferences.preferences_blob = merged
        preferences.updated_at = now
        preferences.save()
        return preferences

