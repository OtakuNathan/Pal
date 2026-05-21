from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

from peewee import DoesNotExist

from pal.behavior.contracts import AFFORDANCE_SOURCE_DECLARED, AffordanceDescriptor
from pal.behavior.models import BehaviorAffordanceModel
from pal.behavior.schema import ensure_behavior_schema
from pal.foundation.persistence import utc_now
from pal.shared.text_search import compile_jieba_fts_queries, jieba_fts_text, normalize_search_text
from pal.skill.contracts import SkillDescriptor
from pal.skill.repository import SkillRepository


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
    skill_repository: SkillRepository = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.skill_repository is None:
            self.skill_repository = SkillRepository()
        ensure_behavior_schema()
        self.ensure_fts_indexes_synced()

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
        stored = self.get_affordance(descriptor.affordance_id) or descriptor
        self.sync_fts_row(descriptor.affordance_id)
        return stored

    def upsert_skill(self, descriptor: SkillDescriptor) -> SkillDescriptor:
        return self.skill_repository.upsert_skill(descriptor)

    def get_affordance(self, affordance_id: str) -> AffordanceDescriptor | None:
        try:
            return _affordance_from_model(BehaviorAffordanceModel.get_by_id(affordance_id))
        except DoesNotExist:
            return None

    def delete_affordance(self, affordance_id: str) -> bool:
        normalized = str(affordance_id or "").strip()
        if not normalized:
            return False
        deleted = BehaviorAffordanceModel.delete().where(BehaviorAffordanceModel.affordance_id == normalized).execute()
        self.sync_fts_row(normalized)
        return bool(deleted)

    def get_skill(self, skill_id: str) -> SkillDescriptor | None:
        return self.skill_repository.get_skill(skill_id)

    def list_affordances(self, *, enabled_only: bool = False) -> tuple[AffordanceDescriptor, ...]:
        query = BehaviorAffordanceModel.select()
        if enabled_only:
            query = query.where(BehaviorAffordanceModel.enabled == True)  # noqa: E712
        query = query.order_by(BehaviorAffordanceModel.affordance_id)
        return tuple(_affordance_from_model(row) for row in query)

    def list_affordances_by_ids(self, affordance_ids: Iterable[str], *, enabled_only: bool = False) -> tuple[AffordanceDescriptor, ...]:
        ordered_ids = [str(item).strip() for item in affordance_ids if str(item).strip()]
        if not ordered_ids:
            return ()
        query = BehaviorAffordanceModel.select().where(BehaviorAffordanceModel.affordance_id.in_(ordered_ids))
        if enabled_only:
            query = query.where(BehaviorAffordanceModel.enabled == True)  # noqa: E712
        by_id = {row.affordance_id: _affordance_from_model(row) for row in query}
        return tuple(by_id[item] for item in ordered_ids if item in by_id)

    def list_skills(self, *, enabled_only: bool = False) -> tuple[SkillDescriptor, ...]:
        return self.skill_repository.list_skills(enabled_only=enabled_only)

    def delete_declared_affordances_for_module(self, module_id: str) -> int:
        doomed_ids = [
            row.affordance_id
            for row in BehaviorAffordanceModel.select(BehaviorAffordanceModel.affordance_id).where(
                (BehaviorAffordanceModel.module_id == module_id)
                & (BehaviorAffordanceModel.source_kind == AFFORDANCE_SOURCE_DECLARED)
            )
        ]
        deleted = (
            BehaviorAffordanceModel.delete()
            .where(
                (BehaviorAffordanceModel.module_id == module_id)
                & (BehaviorAffordanceModel.source_kind == AFFORDANCE_SOURCE_DECLARED)
            )
            .execute()
        )
        for affordance_id in doomed_ids:
            self.sync_fts_row(affordance_id)
        return deleted

    def delete_declared_skills_for_module(self, module_id: str) -> int:
        return self.skill_repository.delete_declared_skills_for_module(module_id)

    def ensure_fts_indexes_synced(self) -> None:
        ensure_behavior_schema()
        affordance_count = BehaviorAffordanceModel.select().count()
        if self._count_fts_rows("behavior_affordances_fts") == affordance_count:
            return
        self.rebuild_fts_indexes()

    def rebuild_fts_indexes(self) -> None:
        ensure_behavior_schema()
        db = BehaviorAffordanceModel._meta.database
        db.execute_sql("DELETE FROM behavior_affordances_fts")
        for affordance in self.list_affordances():
            self._insert_fts_row("behavior_affordances_fts", affordance)

    def sync_fts_row(self, affordance_id: str) -> None:
        ensure_behavior_schema()
        normalized = str(affordance_id or "").strip()
        if not normalized:
            return
        db = BehaviorAffordanceModel._meta.database
        db.execute_sql("DELETE FROM behavior_affordances_fts WHERE affordance_id = ?", (normalized,))
        affordance = self.get_affordance(normalized)
        if affordance is None:
            return
        self._insert_fts_row("behavior_affordances_fts", affordance)

    def collect_route_candidates(self, text: str, *, limit: int) -> tuple[dict[str, float], dict[str, int]]:
        normalized = _normalize_query_text(text)
        empty_sources = {"fts_jieba": 0, "like": 0}
        if not normalized or limit <= 0:
            return {}, empty_sources

        query_limit = max(int(limit) * 2, 12)
        compiled_queries = _compile_fts_queries(normalized)
        fts_scores = self._run_fts_queries("behavior_affordances_fts", compiled_queries, limit=query_limit)
        like_scores: dict[str, float] = {}
        if len(normalized) < 3 or not fts_scores:
            like_scores = self._run_like_candidates(normalized, limit=query_limit)

        combined: dict[str, float] = {}
        for source_scores in (fts_scores, like_scores):
            for affordance_id, score in source_scores.items():
                combined[affordance_id] = max(combined.get(affordance_id, 0.0), score)

        ordered = sorted(combined.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return dict(ordered), {
            "fts_jieba": len(fts_scores),
            "like": len(like_scores),
        }

    def _count_fts_rows(self, table_name: str) -> int:
        db = BehaviorAffordanceModel._meta.database
        try:
            cursor = db.execute_sql(f"SELECT COUNT(*) FROM {table_name}")
        except sqlite3.OperationalError:
            return -1
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def _insert_fts_row(self, table_name: str, affordance: AffordanceDescriptor) -> None:
        db = BehaviorAffordanceModel._meta.database
        db.execute_sql(
            f"""
            INSERT INTO {table_name}(affordance_id, title, scenario_text, prompt_hint, activation_terms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                affordance.affordance_id,
                jieba_fts_text(affordance.title),
                jieba_fts_text(affordance.scenario_text),
                jieba_fts_text(affordance.prompt_hint),
                jieba_fts_text(" ".join(affordance.activation_terms)),
            ),
        )

    def _run_fts_queries(self, table_name: str, queries: list[tuple[str, float]], *, limit: int) -> dict[str, float]:
        if not queries:
            return {}
        db = BehaviorAffordanceModel._meta.database
        scores: dict[str, float] = {}
        for query_text, query_weight in queries:
            try:
                cursor = db.execute_sql(
                    f"""
                    SELECT affordance_id, -bm25({table_name}, 0.0, 8.0, 3.0, 0.8, 9.0) AS score
                    FROM {table_name}
                    WHERE {table_name} MATCH ?
                    ORDER BY bm25({table_name}, 0.0, 8.0, 3.0, 0.8, 9.0)
                    LIMIT ?
                    """,
                    (query_text, limit),
                )
            except sqlite3.OperationalError:
                continue
            for affordance_id, score in cursor.fetchall():
                normalized = str(affordance_id)
                scores[normalized] = max(scores.get(normalized, 0.0), float(score) * float(query_weight))
        return scores

    def _run_like_candidates(self, text: str, *, limit: int) -> dict[str, float]:
        normalized = _normalize_query_text(text)
        if not normalized:
            return {}
        lowered = normalized.lower()
        db = BehaviorAffordanceModel._meta.database
        cursor = db.execute_sql(
            """
            SELECT
                affordance_id,
                CASE WHEN instr(lower(title), ?) > 0 THEN 8.0 ELSE 0.0 END
                + CASE WHEN instr(lower(scenario_text), ?) > 0 THEN 3.0 ELSE 0.0 END
                + CASE WHEN instr(lower(prompt_hint), ?) > 0 THEN 0.8 ELSE 0.0 END
                + CASE WHEN instr(lower(activation_terms), ?) > 0 THEN 9.0 ELSE 0.0 END AS score
            FROM behavior_affordances_fts
            WHERE instr(lower(title), ?) > 0
               OR instr(lower(scenario_text), ?) > 0
               OR instr(lower(prompt_hint), ?) > 0
               OR instr(lower(activation_terms), ?) > 0
            ORDER BY score DESC, affordance_id
            LIMIT ?
            """,
            (lowered, lowered, lowered, lowered, lowered, lowered, lowered, lowered, limit),
        )
        return {str(affordance_id): float(score) for affordance_id, score in cursor.fetchall() if float(score) > 0.0}


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


def _normalize_query_text(text: str) -> str:
    return normalize_search_text(text)


def _compile_fts_queries(text: str) -> list[tuple[str, float]]:
    return compile_jieba_fts_queries(text)
