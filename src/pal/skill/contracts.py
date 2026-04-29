from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SKILL_STATUS_DRAFT = "draft"
SKILL_STATUS_ACTIVE = "active"
SKILL_STATUS_DISABLED = "disabled"
SKILL_STATUS_DEPRECATED = "deprecated"
SKILL_STATUS_NEEDS_REVIEW = "needs_review"

SKILL_STATUSES = {
    SKILL_STATUS_DRAFT,
    SKILL_STATUS_ACTIVE,
    SKILL_STATUS_DISABLED,
    SKILL_STATUS_DEPRECATED,
    SKILL_STATUS_NEEDS_REVIEW,
}

SKILL_SOURCE_DECLARED = "declared"
SKILL_SOURCE_INSTRUCTED = "instructed"
SKILL_SOURCE_LEARNED = "learned"

SKILL_INJECT_MANUAL_CHAR_BUDGET = 12_000


@dataclass(frozen=True)
class SkillApplicabilitySTAR:
    situation: str = ""
    task: str = ""
    action: str = ""
    result: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "situation": self.situation,
            "task": self.task,
            "action": self.action,
            "result": self.result,
        }

    @classmethod
    def from_value(cls, value: object) -> "SkillApplicabilitySTAR":
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(
                situation=str(value.get("situation") or ""),
                task=str(value.get("task") or ""),
                action=str(value.get("action") or ""),
                result=str(value.get("result") or ""),
            )
        return cls()


@dataclass(frozen=True)
class SkillDescriptor:
    skill_id: str
    module_id: str
    title: str
    summary: str
    manual_text: str
    source_kind: str = SKILL_SOURCE_DECLARED
    activation_terms: tuple[str, ...] = ()
    capability_refs: tuple[str, ...] = ()
    enabled: bool = True
    status: str = SKILL_STATUS_ACTIVE
    applicability_star: SkillApplicabilitySTAR = field(default_factory=SkillApplicabilitySTAR)
    use_when: str = ""
    avoid_when: str = ""
    sanitization_notes: tuple[str, ...] = ()
    source_format: str = ""
    source_refs: tuple[str, ...] = ()
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @property
    def active(self) -> bool:
        return bool(self.enabled) and self.status == SKILL_STATUS_ACTIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "module_id": self.module_id,
            "title": self.title,
            "summary": self.summary,
            "manual_text": self.manual_text,
            "source_kind": self.source_kind,
            "activation_terms": list(self.activation_terms),
            "capability_refs": list(self.capability_refs),
            "enabled": self.enabled,
            "status": self.status,
            "applicability_star": self.applicability_star.to_dict(),
            "use_when": self.use_when,
            "avoid_when": self.avoid_when,
            "sanitization_notes": list(self.sanitization_notes),
            "source_format": self.source_format,
            "source_refs": list(self.source_refs),
            "version": self.version,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class SkillAssimilationCandidate:
    candidate_id: str
    decision: str
    skill: SkillDescriptor
    affordance: dict[str, Any]
    duplicate_candidates: tuple[dict[str, Any], ...] = ()
    conflict_candidates: tuple[dict[str, Any], ...] = ()
    removed_risks: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "decision": self.decision,
            "skill": self.skill.to_dict(),
            "affordance": dict(self.affordance),
            "duplicate_candidates": [dict(item) for item in self.duplicate_candidates],
            "conflict_candidates": [dict(item) for item in self.conflict_candidates],
            "removed_risks": list(self.removed_risks),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class SkillInjectRequest:
    skill_id: str
