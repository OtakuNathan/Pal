from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from peewee import DoesNotExist

from pal.behavior.contracts import (
    AFFORDANCE_SOURCE_DECLARED,
    AffordanceDescriptor,
    SkillDescriptor,
)
from pal.behavior.models import BehaviorAffordanceModel, BehaviorSkillModel
from pal.foundation.persistence import utc_now


def _tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


@dataclass
class BehaviorRepository:
    def upsert_affordance(self, descriptor: AffordanceDescriptor) -> AffordanceDescriptor:
        now = utc_now()
        existing = BehaviorAffordanceModel.get_or_none(BehaviorAffordanceModel.affordance_id == descriptor.affordance_id)
        created_at = descriptor.created_at or (existing.created_at if existing is not None else now)
        updated_at = descriptor.updated_at or now
        BehaviorAffordanceModel.insert(
            affordance_id=descriptor.affordance_id,
            module_id=descriptor.module_id,
            title=descriptor.title,
            scenario_text=descriptor.scenario_text,
            prompt_hint=descriptor.prompt_hint,
            visibility_mode=descriptor.visibility_mode,
            activation_kind=descriptor.activation_kind,
            activation_mode=descriptor.activation_mode,
            source_kind=descriptor.source_kind,
            activation_terms_blob=list(descriptor.activation_terms),
            capability_refs_blob=list(descriptor.capability_refs),
            skill_refs_blob=list(descriptor.skill_refs),
            memory_query_hints_blob=list(descriptor.memory_query_hints),
            evidence_refs_blob=list(descriptor.evidence_refs),
            metadata_blob=dict(descriptor.metadata),
            priority=descriptor.priority,
            activation_threshold=descriptor.activation_threshold,
            enabled=descriptor.enabled,
            created_at=created_at,
            updated_at=updated_at,
        ).on_conflict_replace().execute()
        return self.get_affordance(descriptor.affordance_id) or descriptor

    def upsert_skill(self, descriptor: SkillDescriptor) -> SkillDescriptor:
        now = utc_now()
        existing = BehaviorSkillModel.get_or_none(BehaviorSkillModel.skill_id == descriptor.skill_id)
        created_at = descriptor.created_at or (existing.created_at if existing is not None else now)
        updated_at = descriptor.updated_at or now
        BehaviorSkillModel.insert(
            skill_id=descriptor.skill_id,
            module_id=descriptor.module_id,
            title=descriptor.title,
            summary=descriptor.summary,
            manual_text=descriptor.manual_text,
            source_kind=descriptor.source_kind,
            activation_terms_blob=list(descriptor.activation_terms),
            capability_refs_blob=list(descriptor.capability_refs),
            metadata_blob=dict(descriptor.metadata),
            enabled=descriptor.enabled,
            created_at=created_at,
            updated_at=updated_at,
        ).on_conflict_replace().execute()
        return self.get_skill(descriptor.skill_id) or descriptor

    def get_affordance(self, affordance_id: str) -> AffordanceDescriptor | None:
        try:
            return _affordance_from_model(BehaviorAffordanceModel.get_by_id(affordance_id))
        except DoesNotExist:
            return None

    def get_skill(self, skill_id: str) -> SkillDescriptor | None:
        try:
            return _skill_from_model(BehaviorSkillModel.get_by_id(skill_id))
        except DoesNotExist:
            return None

    def list_affordances(self, *, enabled_only: bool = False) -> tuple[AffordanceDescriptor, ...]:
        query = BehaviorAffordanceModel.select()
        if enabled_only:
            query = query.where(BehaviorAffordanceModel.enabled == True)  # noqa: E712
        query = query.order_by(BehaviorAffordanceModel.affordance_id)
        return tuple(_affordance_from_model(row) for row in query)

    def list_skills(self, *, enabled_only: bool = False) -> tuple[SkillDescriptor, ...]:
        query = BehaviorSkillModel.select()
        if enabled_only:
            query = query.where(BehaviorSkillModel.enabled == True)  # noqa: E712
        query = query.order_by(BehaviorSkillModel.skill_id)
        return tuple(_skill_from_model(row) for row in query)

    def delete_declared_affordances_for_module(self, module_id: str) -> int:
        return (
            BehaviorAffordanceModel.delete()
            .where(
                (BehaviorAffordanceModel.module_id == module_id)
                & (BehaviorAffordanceModel.source_kind == AFFORDANCE_SOURCE_DECLARED)
            )
            .execute()
        )

    def delete_declared_skills_for_module(self, module_id: str) -> int:
        return (
            BehaviorSkillModel.delete()
            .where((BehaviorSkillModel.module_id == module_id) & (BehaviorSkillModel.source_kind == AFFORDANCE_SOURCE_DECLARED))
            .execute()
        )


def _affordance_from_model(row: BehaviorAffordanceModel) -> AffordanceDescriptor:
    return AffordanceDescriptor(
        affordance_id=row.affordance_id,
        module_id=row.module_id,
        title=row.title,
        scenario_text=row.scenario_text,
        prompt_hint=row.prompt_hint,
        visibility_mode=row.visibility_mode,
        activation_kind=row.activation_kind,
        activation_mode=row.activation_mode,
        source_kind=row.source_kind,
        activation_terms=_tuple(row.activation_terms_blob),
        capability_refs=_tuple(row.capability_refs_blob),
        skill_refs=_tuple(row.skill_refs_blob),
        memory_query_hints=_tuple(row.memory_query_hints_blob),
        priority=int(row.priority),
        activation_threshold=float(row.activation_threshold),
        enabled=bool(row.enabled),
        evidence_refs=_tuple(row.evidence_refs_blob),
        metadata=dict(row.metadata_blob or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _skill_from_model(row: BehaviorSkillModel) -> SkillDescriptor:
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
        metadata=dict(row.metadata_blob or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
