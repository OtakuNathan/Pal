from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pal.skill.contracts import SKILL_SOURCE_DECLARED


@dataclass(frozen=True)
class SkillBlueprint:
    skill_id: str
    title: str
    summary: str
    manual_text: str
    source_kind: str = SKILL_SOURCE_DECLARED
    activation_terms: tuple[str, ...] = ()
    capability_refs: tuple[str, ...] = ()
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


def skill(
    *,
    skill_id: str,
    title: str,
    summary: str,
    manual_text: str,
    source_kind: str = SKILL_SOURCE_DECLARED,
    activation_terms: tuple[str, ...] = (),
    capability_refs: tuple[str, ...] = (),
    enabled: bool = True,
    metadata: dict[str, Any] | None = None,
):
    def decorator(obj):
        existing = list(getattr(obj, "__skill_blueprints__", ()))
        existing.append(
            SkillBlueprint(
                skill_id=skill_id,
                title=title,
                summary=summary,
                manual_text=manual_text,
                source_kind=source_kind,
                activation_terms=tuple(activation_terms),
                capability_refs=tuple(capability_refs),
                enabled=bool(enabled),
                metadata=dict(metadata or {}),
            )
        )
        obj.__skill_blueprints__ = existing
        return obj

    return decorator
