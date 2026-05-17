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
    core_policy: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PalPreferencesProfile:
    preference_id: str
    style_preference: str | None = None
    timezone: str | None = None
    preferences_blob: dict[str, Any] = field(default_factory=dict)


class IdentityServicePort(Protocol):
    def ensure_defaults(self) -> None:
        ...

    def get_persona(self) -> PalPersonaProfile | None:
        ...

    def get_preferences(self) -> PalPreferencesProfile | None:
        ...

    def update_preferences(self, *, timezone: str | None = None) -> PalPreferencesProfile:
        ...
