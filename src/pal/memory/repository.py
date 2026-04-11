from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pal.foundation import utc_now
from pal.memory.models import (
    MemoryCaseModel,
    MemoryEmbeddingModel,
    MemoryEmbeddingVecModel,
    MemoryFactModel,
    MemoryTopicModel,
)
from pal.memory.schema import ensure_memory_schema

from pal.memory.contracts import L3ProviderPort, L3ProviderResolver


@dataclass
class L3ProviderSelector:
    resolver: L3ProviderResolver
    active_provider_id: str = "null_l3"

    def resolve(self) -> L3ProviderPort:
        return self.resolver(self.active_provider_id)


class MemoryDurableRepository:
    def ensure_schema(self) -> None:
        ensure_memory_schema()

    def upsert_fact(self, *, fact_id: str, payload: dict[str, Any]) -> MemoryFactModel:
        now = utc_now()
        instance = MemoryFactModel.get_or_none(MemoryFactModel.fact_id == fact_id)
        if instance is None:
            return MemoryFactModel.create(
                fact_id=fact_id,
                created_at=now,
                updated_at=now,
                **payload,
            )
        for key, value in payload.items():
            setattr(instance, key, value)
        instance.updated_at = now
        instance.save()
        return instance

    def upsert_case(self, *, case_id: str, payload: dict[str, Any]) -> MemoryCaseModel:
        now = utc_now()
        instance = MemoryCaseModel.get_or_none(MemoryCaseModel.case_id == case_id)
        if instance is None:
            return MemoryCaseModel.create(
                case_id=case_id,
                created_at=now,
                updated_at=now,
                **payload,
            )
        for key, value in payload.items():
            setattr(instance, key, value)
        instance.updated_at = now
        instance.save()
        return instance

    def get_fact(self, fact_id: str) -> MemoryFactModel | None:
        return MemoryFactModel.get_or_none(MemoryFactModel.fact_id == fact_id)

    def get_case(self, case_id: str) -> MemoryCaseModel | None:
        return MemoryCaseModel.get_or_none(MemoryCaseModel.case_id == case_id)

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        kind, _, raw_id = document_id.partition(":")
        if kind == "fact":
            fact = self.get_fact(raw_id)
            if fact is None:
                return None
            return {
                "document_id": document_id,
                "document_kind": "fact",
                "scope": fact.scope,
                "task_id": fact.task_id,
                "title": fact.title or "",
                "summary": fact.summary,
                "search_text": fact.search_text,
                "rendered": fact.summary,
                "payload": dict(fact.payload_blob or {}),
                "canonical_key": fact.canonical_key,
                "use_count": fact.use_count,
                "last_used_at": fact.last_used_at,
                "created_at": fact.created_at,
                "updated_at": fact.updated_at,
            }
        if kind == "case":
            case = self.get_case(raw_id)
            if case is None:
                return None
            return {
                "document_id": document_id,
                "document_kind": "case",
                "scope": case.scope,
                "task_id": case.task_id,
                "title": case.title or "",
                "summary": case.summary,
                "search_text": case.search_text,
                "rendered": "\n".join(
                    part
                    for part in (
                        case.summary,
                        f"Situation: {case.situation_text}" if case.situation_text else "",
                        f"Task: {case.task_text}" if case.task_text else "",
                        f"Action: {case.action_text}" if case.action_text else "",
                        f"Result: {case.result_text}" if case.result_text else "",
                    )
                    if part
                ),
                "payload": {
                    **dict(case.payload_blob or {}),
                    "situation_text": case.situation_text,
                    "task_text": case.task_text,
                    "action_text": case.action_text,
                    "result_text": case.result_text,
                },
                "use_count": case.use_count,
                "last_used_at": case.last_used_at,
                "created_at": case.created_at,
                "updated_at": case.updated_at,
            }
        return None

    def list_projection_rows(self) -> list[dict[str, Any]]:
        db = MemoryFactModel._meta.database
        cursor = db.execute_sql(
            """
            SELECT
                document_id,
                document_kind,
                scope,
                task_id,
                title,
                summary,
                search_text,
                rendered,
                use_count,
                last_used_at,
                created_at,
                updated_at
            FROM memory_document_projection
            ORDER BY document_id
            """
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def sync_fts_row(self, document_id: str) -> None:
        row = self.get_document(document_id)
        db = MemoryFactModel._meta.database
        db.execute_sql("DELETE FROM memories_fts WHERE document_id = ?", (document_id,))
        if row is None:
            return
        db.execute_sql(
            "INSERT INTO memories_fts(document_id, title, summary, search_text) VALUES (?, ?, ?, ?)",
            (document_id, row["title"], row["summary"], row["search_text"]),
        )

    def replace_topics(self, document_id: str, topics: list[str]) -> None:
        MemoryTopicModel.delete().where(MemoryTopicModel.document_id == document_id).execute()
        if not topics:
            return
        now = utc_now()
        seen: set[str] = set()
        for topic in topics:
            normalized = normalize_topic(topic)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            MemoryTopicModel.create(
                topic_id=f"{document_id}:{normalized}",
                document_id=document_id,
                topic=str(topic).strip(),
                normalized_topic=normalized,
                created_at=now,
            )

    def queue_embedding(
        self,
        *,
        document_id: str,
        source_text: str,
        model_name: str,
        embedding_kind: str = "primary",
        index_status: str = "pending",
        last_error: str | None = None,
    ) -> MemoryEmbeddingModel:
        embedding_id = f"{document_id}:{embedding_kind}"
        source_text_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        now = utc_now()
        instance = MemoryEmbeddingModel.get_or_none(MemoryEmbeddingModel.embedding_id == embedding_id)
        if instance is None:
            return MemoryEmbeddingModel.create(
                embedding_id=embedding_id,
                document_id=document_id,
                embedding_kind=embedding_kind,
                model_name=model_name,
                model_revision=None,
                source_text_hash=source_text_hash,
                embedding_norm=None,
                index_status=index_status,
                last_error=last_error,
                created_at=now,
                updated_at=now,
            )
        instance.document_id = document_id
        instance.embedding_kind = embedding_kind
        instance.model_name = model_name
        instance.source_text_hash = source_text_hash
        instance.index_status = index_status
        instance.last_error = last_error
        instance.updated_at = now
        instance.save()
        return instance

    def list_pending_embeddings(self, *, limit: int) -> list[MemoryEmbeddingModel]:
        query = (
            MemoryEmbeddingModel.select()
            .where(MemoryEmbeddingModel.index_status.in_(("pending", "stale")))
            .order_by(MemoryEmbeddingModel.updated_at, MemoryEmbeddingModel.embedding_id)
            .limit(limit)
        )
        return list(query)

    def list_retryable_embeddings(self, *, limit: int, retry_failed: bool = False) -> list[MemoryEmbeddingModel]:
        statuses = ["pending", "stale"]
        if retry_failed:
            statuses.append("failed")
        query = (
            MemoryEmbeddingModel.select()
            .where(MemoryEmbeddingModel.index_status.in_(tuple(statuses)))
            .order_by(MemoryEmbeddingModel.updated_at, MemoryEmbeddingModel.embedding_id)
            .limit(limit)
        )
        return list(query)

    def list_failed_embeddings(self, *, limit: int = 5) -> list[dict[str, Any]]:
        rows = (
            MemoryEmbeddingModel.select()
            .where(MemoryEmbeddingModel.index_status == "failed")
            .order_by(MemoryEmbeddingModel.updated_at.desc(), MemoryEmbeddingModel.embedding_id.desc())
            .limit(limit)
        )
        return [
            {
                "embedding_id": row.embedding_id,
                "document_id": row.document_id,
                "embedding_kind": row.embedding_kind,
                "model_name": row.model_name,
                "last_error": row.last_error,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]

    def get_embedding(self, embedding_id: str) -> MemoryEmbeddingModel | None:
        return MemoryEmbeddingModel.get_or_none(MemoryEmbeddingModel.embedding_id == embedding_id)

    def get_vector_blob(self, embedding_id: str) -> bytes | None:
        vector = MemoryEmbeddingVecModel.get_or_none(MemoryEmbeddingVecModel.embedding_id == embedding_id)
        if vector is None:
            return None
        return bytes(vector.vector_blob)

    def upsert_vector_blob(self, *, embedding_id: str, vector_blob: bytes, dimension: int) -> None:
        now = utc_now()
        instance = MemoryEmbeddingVecModel.get_or_none(MemoryEmbeddingVecModel.embedding_id == embedding_id)
        if instance is None:
            MemoryEmbeddingVecModel.create(
                embedding_id=embedding_id,
                vector_blob=vector_blob,
                dimension=dimension,
                updated_at=now,
            )
            return
        instance.vector_blob = vector_blob
        instance.dimension = dimension
        instance.updated_at = now
        instance.save()

    def mark_embedding_ready(self, *, embedding_id: str, norm: float) -> None:
        instance = self.get_embedding(embedding_id)
        if instance is None:
            return
        instance.index_status = "ready"
        instance.embedding_norm = str(norm)
        instance.last_error = None
        instance.updated_at = utc_now()
        instance.save()

    def mark_embedding_failed(self, *, embedding_id: str, error_text: str) -> None:
        instance = self.get_embedding(embedding_id)
        if instance is None:
            return
        instance.index_status = "failed"
        instance.last_error = error_text
        instance.updated_at = utc_now()
        instance.save()

    def list_topic_candidates(self, topics: list[str], *, limit: int) -> dict[str, float]:
        normalized = [normalize_topic(topic) for topic in topics if normalize_topic(topic)]
        if not normalized:
            return {}
        query = (
            MemoryTopicModel.select(MemoryTopicModel.document_id)
            .where(MemoryTopicModel.normalized_topic.in_(normalized))
        )
        scores: dict[str, float] = {}
        for row in query:
            scores[row.document_id] = scores.get(row.document_id, 0.0) + 1.0
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return dict(ordered)

    def list_fts_candidates(self, text: str, *, limit: int) -> dict[str, float]:
        query_text = " ".join(part.strip() for part in str(text).splitlines() if part.strip()).strip()
        if not query_text:
            return {}
        db = MemoryFactModel._meta.database
        cursor = db.execute_sql(
            """
            SELECT document_id, -bm25(memories_fts) AS score
            FROM memories_fts
            WHERE memories_fts MATCH ?
            ORDER BY bm25(memories_fts)
            LIMIT ?
            """,
            (query_text, limit),
        )
        return {str(document_id): float(score) for document_id, score in cursor.fetchall()}

    def list_vector_rows(self) -> list[tuple[MemoryEmbeddingModel, bytes]]:
        rows: list[tuple[MemoryEmbeddingModel, bytes]] = []
        query = (
            MemoryEmbeddingModel.select()
            .where(MemoryEmbeddingModel.index_status == "ready")
            .order_by(MemoryEmbeddingModel.embedding_id)
        )
        for metadata in query:
            blob = self.get_vector_blob(metadata.embedding_id)
            if blob is None:
                continue
            rows.append((metadata, blob))
        return rows

    def bump_usage(self, document_ids: list[str]) -> None:
        now = utc_now()
        for document_id in document_ids:
            kind, _, raw_id = document_id.partition(":")
            if kind == "fact":
                fact = self.get_fact(raw_id)
                if fact is None:
                    continue
                fact.use_count += 1
                fact.last_used_at = now
                fact.updated_at = now
                fact.save()
            elif kind == "case":
                case = self.get_case(raw_id)
                if case is None:
                    continue
                case.use_count += 1
                case.last_used_at = now
                case.updated_at = now
                case.save()

    def inventory(self) -> dict[str, Any]:
        ready_embeddings = MemoryEmbeddingModel.select().where(MemoryEmbeddingModel.index_status == "ready").count()
        pending_embeddings = MemoryEmbeddingModel.select().where(MemoryEmbeddingModel.index_status == "pending").count()
        stale_embeddings = MemoryEmbeddingModel.select().where(MemoryEmbeddingModel.index_status == "stale").count()
        failed_embeddings = MemoryEmbeddingModel.select().where(MemoryEmbeddingModel.index_status == "failed").count()
        return {
            "fact_count": MemoryFactModel.select().count(),
            "case_count": MemoryCaseModel.select().count(),
            "topic_count": MemoryTopicModel.select().count(),
            "embedding_count": MemoryEmbeddingModel.select().count(),
            "ready_embeddings": ready_embeddings,
            "pending_embeddings": pending_embeddings,
            "stale_embeddings": stale_embeddings,
            "failed_embeddings": failed_embeddings,
            "retryable_embeddings": pending_embeddings + stale_embeddings + failed_embeddings,
            "recent_embedding_errors": self.list_failed_embeddings(limit=5),
        }


def normalize_topic(topic: str) -> str:
    return " ".join(str(topic or "").strip().lower().split())


def serialize_vector(vector: list[float]) -> bytes:
    return sqlite3.Binary(json.dumps([float(value) for value in vector]).encode("utf-8"))  # type: ignore[return-value]


def deserialize_vector(blob: bytes) -> list[float]:
    return [float(value) for value in json.loads(blob.decode("utf-8"))]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def parse_iso_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
