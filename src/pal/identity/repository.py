from __future__ import annotations

from typing import Any

from pal.foundation import utc_now
from pal.identity.models import PalPersonaModel, UserPreferencesModel


DEFAULT_PERSONA_ID = "default"
DEFAULT_PREFERENCE_ID = "default"


class IdentityRepository:
    def ensure_defaults(
        self,
        *,
        display_name: str = "Pal",
        language: str = "Default to the user's language. Use English only when the user asks or the task context clearly requires it.",
        vibe: str | None = "thoughtful, direct, warm, humorous, non-preachy.",
        tone: str | None = "direct, humorous",
        core_policy: list[str] | None = None,
        timezone: str | None = "Asia/Shanghai",
        style_preference: str | None = (
            "Be concise. Do not repeat or over-explain. Prioritize specific situations over generalities. "
            "Adapt based on user corrections. Go deep on design discussions."
        ),
    ) -> None:
        now = utc_now()
        policy = list(
            core_policy
            or [
                "Never fabricate facts, memory, or runtime state.",
                "If completion is uncertain, treat it as unconfirmed.",
                "Verify before claiming success or repeating an action.",
            ]
        )
        persona, persona_created = PalPersonaModel.get_or_create(
            persona_id=DEFAULT_PERSONA_ID,
            defaults={
                "display_name": display_name,
                "language": language,
                "vibe": vibe,
                "tone": tone,
                "core_policy": policy,
                "created_at": now,
                "updated_at": now,
            },
        )
        if not persona_created:
            changed = False
            if display_name and str(persona.display_name or "").strip() in {"", "pal"}:
                persona.display_name = display_name
                changed = True
            if language and str(persona.language or "").strip() in {"", "en"}:
                persona.language = language
                changed = True
            if vibe and not str(persona.vibe or "").strip():
                persona.vibe = vibe
                changed = True
            if tone and not str(persona.tone or "").strip():
                persona.tone = tone
                changed = True
            if policy and not list(persona.core_policy or []):
                persona.core_policy = policy
                changed = True
            if changed:
                persona.updated_at = now
                persona.save()

        preferences, preferences_created = UserPreferencesModel.get_or_create(
            preference_id=DEFAULT_PREFERENCE_ID,
            defaults={
                "style_preference": style_preference,
                "timezone": timezone,
                "preferences_blob": {},
                "created_at": now,
                "updated_at": now,
            },
        )
        if not preferences_created:
            changed = False
            if style_preference and not str(preferences.style_preference or "").strip():
                preferences.style_preference = style_preference
                changed = True
            if timezone and not str(preferences.timezone or "").strip():
                preferences.timezone = timezone
                changed = True
            if changed:
                preferences.updated_at = now
                preferences.save()

    def get_persona(self, persona_id: str = DEFAULT_PERSONA_ID) -> PalPersonaModel | None:
        return PalPersonaModel.get_or_none(PalPersonaModel.persona_id == persona_id)

    def get_user_preferences(
        self,
        preference_id: str = DEFAULT_PREFERENCE_ID,
    ) -> UserPreferencesModel | None:
        return UserPreferencesModel.get_or_none(UserPreferencesModel.preference_id == preference_id)

    def update_user_preferences(
        self,
        *,
        style_preference: str | None = None,
        timezone: str | None = None,
        preferences_patch: dict[str, Any] | None = None,
    ) -> UserPreferencesModel:
        now = utc_now()
        preferences = self.get_user_preferences(DEFAULT_PREFERENCE_ID)
        if preferences is None:
            preferences = UserPreferencesModel.create(
                preference_id=DEFAULT_PREFERENCE_ID,
                style_preference=style_preference,
                timezone=timezone,
                preferences_blob=dict(preferences_patch or {}),
                created_at=now,
                updated_at=now,
            )
            return preferences

        merged = dict(preferences.preferences_blob or {})
        merged.update(dict(preferences_patch or {}))
        if style_preference is not None:
            preferences.style_preference = style_preference
        if timezone is not None:
            preferences.timezone = timezone
        preferences.preferences_blob = merged
        preferences.updated_at = now
        preferences.save()
        return preferences
