from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class PalPersonaProfile:
    persona_id: str
    display_name: str
    language: str
    vibe: str | None = None
    tone: str | None = None
    style_notes: str | None = None
    core_policy: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PalPreferencesProfile:
    preference_id: str
    language_preference: str | None = None
    style_preference: str | None = None
    timezone: str | None = None
    preferences_blob: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PalStateProfile:
    persona_id: str
    status: str
    top_of_mind_refs: list[str] = field(default_factory=list)
    last_active_at: str | None = None


class IdentityServicePort(Protocol):
    def ensure_defaults(self) -> None:
        ...

    def get_persona(self) -> PalPersonaProfile | None:
        ...

    def get_preferences(self) -> PalPreferencesProfile | None:
        ...

    def get_state(self) -> PalStateProfile | None:
        ...

    def update_preferences(self, *, timezone: str | None = None) -> PalPreferencesProfile:
        ...
