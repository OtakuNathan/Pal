from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pal.behavior.contracts import (
    AFFORDANCE_ACTIVATION_DELIBERATIVE,
    AFFORDANCE_MODE_SUGGEST,
    AFFORDANCE_SOURCE_DECLARED,
    AFFORDANCE_VISIBILITY_DISCOVERABLE,
)


@dataclass(frozen=True)
class AffordanceBlueprint:
    affordance_id: str
    title: str
    scenario_text: str
    prompt_hint: str
    visibility_mode: str = AFFORDANCE_VISIBILITY_DISCOVERABLE
    activation_kind: str = AFFORDANCE_ACTIVATION_DELIBERATIVE
    activation_mode: str = AFFORDANCE_MODE_SUGGEST
    source_kind: str = AFFORDANCE_SOURCE_DECLARED
    activation_terms: tuple[str, ...] = ()
    capability_refs: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    memory_query_hints: tuple[str, ...] = ()
    priority: int = 100
    activation_threshold: float = 0.25
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillBlueprint:
    skill_id: str
    title: str
    summary: str
    manual_text: str
    source_kind: str = AFFORDANCE_SOURCE_DECLARED
    activation_terms: tuple[str, ...] = ()
    capability_refs: tuple[str, ...] = ()
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


def affordance(
    *,
    affordance_id: str,
    title: str,
    scenario_text: str,
    prompt_hint: str,
    visibility_mode: str = AFFORDANCE_VISIBILITY_DISCOVERABLE,
    activation_kind: str = AFFORDANCE_ACTIVATION_DELIBERATIVE,
    activation_mode: str = AFFORDANCE_MODE_SUGGEST,
    source_kind: str = AFFORDANCE_SOURCE_DECLARED,
    activation_terms: tuple[str, ...] = (),
    capability_refs: tuple[str, ...] = (),
    skill_refs: tuple[str, ...] = (),
    memory_query_hints: tuple[str, ...] = (),
    priority: int = 100,
    activation_threshold: float = 0.25,
    enabled: bool = True,
    metadata: dict[str, Any] | None = None,
):
    def decorator(obj):
        existing = list(getattr(obj, "__behavior_affordance_blueprints__", ()))
        existing.append(
            AffordanceBlueprint(
                affordance_id=affordance_id,
                title=title,
                scenario_text=scenario_text,
                prompt_hint=prompt_hint,
                visibility_mode=visibility_mode,
                activation_kind=activation_kind,
                activation_mode=activation_mode,
                source_kind=source_kind,
                activation_terms=tuple(activation_terms),
                capability_refs=tuple(capability_refs),
                skill_refs=tuple(skill_refs),
                memory_query_hints=tuple(memory_query_hints),
                priority=int(priority),
                activation_threshold=float(activation_threshold),
                enabled=bool(enabled),
                metadata=dict(metadata or {}),
            )
        )
        obj.__behavior_affordance_blueprints__ = existing
        return obj

    return decorator


def skill(
    *,
    skill_id: str,
    title: str,
    summary: str,
    manual_text: str,
    source_kind: str = AFFORDANCE_SOURCE_DECLARED,
    activation_terms: tuple[str, ...] = (),
    capability_refs: tuple[str, ...] = (),
    enabled: bool = True,
    metadata: dict[str, Any] | None = None,
):
    def decorator(obj):
        existing = list(getattr(obj, "__behavior_skill_blueprints__", ()))
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
        obj.__behavior_skill_blueprints__ = existing
        return obj

    return decorator
