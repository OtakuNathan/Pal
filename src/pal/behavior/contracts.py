from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pal.skill.contracts import SkillDescriptor, SkillInjectRequest


AFFORDANCE_VISIBILITY_RESIDENT = "resident"
AFFORDANCE_VISIBILITY_DISCOVERABLE = "discoverable"

AFFORDANCE_ACTIVATION_DELIBERATIVE = "deliberative"
AFFORDANCE_ACTIVATION_REACTIVE = "reactive"

AFFORDANCE_MODE_SUGGEST = "suggest"
AFFORDANCE_MODE_AUTOMATIC = "automatic"
AFFORDANCE_MODE_REQUIRE_APPROVAL = "require_approval"

AFFORDANCE_SOURCE_DECLARED = "declared"
AFFORDANCE_SOURCE_INSTRUCTED = "instructed"
AFFORDANCE_SOURCE_LEARNED = "learned"

AFFORDANCE_AVAILABLE = "available"
AFFORDANCE_PARTIAL = "partial"
AFFORDANCE_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class AffordanceDescriptor:
    affordance_id: str
    module_id: str
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
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class BehaviorAdviceRequest:
    scenario: str
    intent: str = ""
    turn_kind: str = "chat"
    constraints: tuple[str, ...] = ()
    already_considered: tuple[str, ...] = ()
    top_k: int = 5


@dataclass(frozen=True)
class BehaviorRouteCandidate:
    affordance_id: str
    title: str
    confidence: float
    availability: str
    reason: str
    prompt_hint: str
    visibility_mode: str
    activation_kind: str
    activation_mode: str
    source_kind: str
    capability_refs: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    memory_query_hints: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "affordance_id": self.affordance_id,
            "title": self.title,
            "confidence": self.confidence,
            "availability": self.availability,
            "reason": self.reason,
            "prompt_hint": self.prompt_hint,
            "visibility_mode": self.visibility_mode,
            "activation_kind": self.activation_kind,
            "activation_mode": self.activation_mode,
            "source_kind": self.source_kind,
            "capability_refs": list(self.capability_refs),
            "skill_refs": list(self.skill_refs),
            "memory_query_hints": list(self.memory_query_hints),
            "evidence_refs": list(self.evidence_refs),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class BehaviorAdviceResult:
    candidates: tuple[BehaviorRouteCandidate, ...] = ()
    fallback_used: bool = False
    router_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "fallback_used": self.fallback_used,
            "router_error": self.router_error,
        }
