from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from peewee import DoesNotExist

from pal.behavior.models import BehaviorSkillModel
from pal.foundation.persistence import utc_now
from pal.skill.contracts import (
    SKILL_SOURCE_DECLARED,
    SKILL_STATUS_ACTIVE,
    SKILL_STATUS_DEPRECATED,
    SKILL_STATUS_DISABLED,
    SkillApplicabilitySTAR,
    SkillDescriptor,
)


def _tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


@dataclass
class SkillRepository:
    def upsert_skill(self, descriptor: SkillDescriptor) -> SkillDescriptor:
        now = utc_now()
        existing = BehaviorSkillModel.get_or_none(BehaviorSkillModel.skill_id == descriptor.skill_id)
        created_at = descriptor.created_at or (existing.created_at if existing is not None else now)
        updated_at = descriptor.updated_at or now
        metadata = dict(descriptor.metadata or {})
        metadata.update(
            {
                "status": descriptor.status,
                "applicability_star": descriptor.applicability_star.to_dict(),
                "use_when": descriptor.use_when,
                "avoid_when": descriptor.avoid_when,
                "sanitization_notes": list(descriptor.sanitization_notes),
                "source_format": descriptor.source_format,
                "source_refs": list(descriptor.source_refs),
                "version": int(descriptor.version),
            }
        )
        enabled = bool(descriptor.enabled) and descriptor.status not in {SKILL_STATUS_DISABLED, SKILL_STATUS_DEPRECATED}
        BehaviorSkillModel.insert(
            skill_id=descriptor.skill_id,
            module_id=descriptor.module_id,
            title=descriptor.title,
            summary=descriptor.summary,
            manual_text=descriptor.manual_text,
            source_kind=descriptor.source_kind,
            activation_terms_blob=list(descriptor.activation_terms),
            capability_refs_blob=list(descriptor.capability_refs),
            metadata_blob=metadata,
            enabled=enabled,
            created_at=created_at,
            updated_at=updated_at,
        ).on_conflict_replace().execute()
        return self.get_skill(descriptor.skill_id) or descriptor

    def get_skill(self, skill_id: str) -> SkillDescriptor | None:
        try:
            return _skill_from_model(BehaviorSkillModel.get_by_id(skill_id))
        except DoesNotExist:
            return None

    def list_skills(self, *, enabled_only: bool = False, active_only: bool = False) -> tuple[SkillDescriptor, ...]:
        query = BehaviorSkillModel.select()
        if enabled_only or active_only:
            query = query.where(BehaviorSkillModel.enabled == True)  # noqa: E712
        query = query.order_by(BehaviorSkillModel.skill_id)
        skills = tuple(_skill_from_model(row) for row in query)
        if active_only:
            skills = tuple(skill for skill in skills if skill.active)
        return skills

    def delete_declared_skills_for_module(self, module_id: str) -> int:
        return (
            BehaviorSkillModel.delete()
            .where((BehaviorSkillModel.module_id == module_id) & (BehaviorSkillModel.source_kind == SKILL_SOURCE_DECLARED))
            .execute()
        )

    def mark_deprecated(self, skill_id: str) -> SkillDescriptor | None:
        skill = self.get_skill(skill_id)
        if skill is None:
            return None
        return self.upsert_skill(
            SkillDescriptor(
                **{
                    **skill.to_dict(),
                    "applicability_star": skill.applicability_star,
                    "status": SKILL_STATUS_DEPRECATED,
                    "enabled": False,
                    "updated_at": utc_now(),
                }
            )
        )

    def disable_skill(self, skill_id: str) -> SkillDescriptor | None:
        skill = self.get_skill(skill_id)
        if skill is None:
            return None
        return self.upsert_skill(
            SkillDescriptor(
                **{
                    **skill.to_dict(),
                    "applicability_star": skill.applicability_star,
                    "status": SKILL_STATUS_DISABLED,
                    "enabled": False,
                    "updated_at": utc_now(),
                }
            )
        )


def _skill_from_model(row: BehaviorSkillModel) -> SkillDescriptor:
    metadata = dict(row.metadata_blob or {})
    status = str(metadata.get("status") or (SKILL_STATUS_ACTIVE if row.enabled else SKILL_STATUS_DISABLED))
    return SkillDescriptor(
        skill_id=row.skill_id,
        module_id=row.module_id,
        title=row.title,
        summary=row.summary,
        manual_text=row.manual_text,
        source_kind=row.source_kind,
        activation_terms=_tuple(row.activation_terms_blob),
        capability_refs=_tuple(row.capability_refs_blob),
        enabled=bool(row.enabled),
        status=status,
        applicability_star=SkillApplicabilitySTAR.from_value(metadata.get("applicability_star")),
        use_when=str(metadata.get("use_when") or ""),
        avoid_when=str(metadata.get("avoid_when") or ""),
        sanitization_notes=_tuple(metadata.get("sanitization_notes")),
        source_format=str(metadata.get("source_format") or ""),
        source_refs=_tuple(metadata.get("source_refs")),
        version=int(metadata.get("version") or 1),
        metadata={key: value for key, value in metadata.items() if key not in _RESERVED_METADATA_KEYS},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


_RESERVED_METADATA_KEYS = {
    "status",
    "applicability_star",
    "use_when",
    "avoid_when",
    "sanitization_notes",
    "source_format",
    "source_refs",
    "version",
}
