from __future__ import annotations

import hashlib
import json
import math
import os
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
from pal.memory.schema import ensure_memory_schema, ensure_sqlite_vec_loaded

from pal.memory.contracts import L3ProviderPort, L3ProviderResolver
from pal.shared.text_search import compile_jieba_fts_queries, jieba_fts_text, normalize_search_text


@dataclass
class L3ProviderSelector:
    resolver: L3ProviderResolver
    active_provider_id: str = "null_l3"

    def resolve(self) -> L3ProviderPort:
        return self.resolver(self.active_provider_id)


class MemoryDurableRepository:
    def ensure_schema(self) -> None:
        if os.environ.get("PAL_DATABASE_READ_ONLY") == "1":
            return
        ensure_memory_schema()
        self.ensure_fts_indexes_synced()

    def find_fact_by_canonical_key(self, canonical_key: str) -> MemoryFactModel | None:
        normalized = str(canonical_key or "").strip()
        if not normalized:
            return None
        query = (
            MemoryFactModel.select()
            .where(MemoryFactModel.canonical_key == normalized)
            .order_by(MemoryFactModel.updated_at.desc(), MemoryFactModel.fact_id.desc())
        )
        return query.first()

    def find_fact_by_dedupe_fingerprint(self, dedupe_fingerprint: str) -> MemoryFactModel | None:
        normalized = str(dedupe_fingerprint or "").strip()
        if not normalized:
            return None
        query = (
            MemoryFactModel.select()
            .where(MemoryFactModel.dedupe_fingerprint == normalized)
            .order_by(MemoryFactModel.updated_at.desc(), MemoryFactModel.fact_id.desc())
        )
        return query.first()

    def find_case_by_dedupe_fingerprint(self, dedupe_fingerprint: str) -> MemoryCaseModel | None:
        normalized = str(dedupe_fingerprint or "").strip()
        if not normalized:
            return None
        query = (
            MemoryCaseModel.select()
            .where(MemoryCaseModel.dedupe_fingerprint == normalized)
            .order_by(MemoryCaseModel.updated_at.desc(), MemoryCaseModel.case_id.desc())
        )
        return query.first()

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
                "dedupe_fingerprint": fact.dedupe_fingerprint,
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
                "dedupe_fingerprint": case.dedupe_fingerprint,
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

    def ensure_fts_indexes_synced(self) -> None:
        projection_count = self._count_projection_rows()
        if self._count_fts_rows("memories_fts") == projection_count:
            return
        self.rebuild_fts_indexes()

    def rebuild_fts_indexes(self) -> None:
        db = MemoryFactModel._meta.database
        db.execute_sql("DELETE FROM memories_fts")
        for row in self.list_projection_rows():
            self._insert_fts_row("memories_fts", row)

    def _count_projection_rows(self) -> int:
        db = MemoryFactModel._meta.database
        cursor = db.execute_sql("SELECT COUNT(*) FROM memory_document_projection")
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def _count_fts_rows(self, table_name: str) -> int:
        db = MemoryFactModel._meta.database
        cursor = db.execute_sql(f"SELECT COUNT(*) FROM {table_name}")
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def sync_fts_row(self, document_id: str) -> None:
        row = self.get_document(document_id)
        db = MemoryFactModel._meta.database
        db.execute_sql("DELETE FROM memories_fts WHERE document_id = ?", (document_id,))
        if row is None:
            return
        self._insert_fts_row("memories_fts", row)

    def delete_document(self, document_id: str) -> dict[str, Any] | None:
        normalized = str(document_id or "").strip()
        if not normalized:
            return None
        existing = self.get_document(normalized)
        if existing is None:
            return None
        kind, _, raw_id = normalized.partition(":")
        db = MemoryFactModel._meta.database
        embedding_ids = [
            str(row.embedding_id)
            for row in MemoryEmbeddingModel.select(MemoryEmbeddingModel.embedding_id).where(
                MemoryEmbeddingModel.document_id == normalized
            )
        ]
        vector_rowids = [rowid for embedding_id in embedding_ids if (rowid := self._get_vector_rowid(embedding_id)) is not None]
        with db.atomic():
            db.execute_sql("DELETE FROM memories_fts WHERE document_id = ?", (normalized,))
            MemoryTopicModel.delete().where(MemoryTopicModel.document_id == normalized).execute()
            if vector_rowids:
                self._delete_vec_index_rows(vector_rowids)
            if embedding_ids:
                MemoryEmbeddingVecModel.delete().where(MemoryEmbeddingVecModel.embedding_id.in_(embedding_ids)).execute()
            MemoryEmbeddingModel.delete().where(MemoryEmbeddingModel.document_id == normalized).execute()
            if kind == "fact":
                MemoryFactModel.delete().where(MemoryFactModel.fact_id == raw_id).execute()
            elif kind == "case":
                MemoryCaseModel.delete().where(MemoryCaseModel.case_id == raw_id).execute()
            else:
                return None
        return existing

    def _delete_vec_index_rows(self, rowids: list[int]) -> None:
        if not rowids:
            return
        db = MemoryEmbeddingVecModel._meta.database
        cursor = db.execute_sql("SELECT name FROM sqlite_master WHERE sql LIKE '%USING vec0%'")
        table_names = [str(row[0]) for row in cursor.fetchall()]
        for table_name in table_names:
            try:
                for rowid in rowids:
                    db.execute_sql(f"DELETE FROM {table_name} WHERE rowid = ?", (int(rowid),))
            except sqlite3.OperationalError:
                continue

    def _insert_fts_row(self, table_name: str, row: dict[str, Any]) -> None:
        db = MemoryFactModel._meta.database
        db.execute_sql(
            f"INSERT INTO {table_name}(document_id, title, summary, search_text) VALUES (?, ?, ?, ?)",
            (
                str(row.get("document_id") or ""),
                jieba_fts_text(row.get("title")),
                jieba_fts_text(row.get("summary")),
                jieba_fts_text(row.get("search_text")),
            ),
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

    def list_document_topics(self, document_id: str) -> list[str]:
        query = (
            MemoryTopicModel.select()
            .where(MemoryTopicModel.document_id == document_id)
            .order_by(MemoryTopicModel.normalized_topic, MemoryTopicModel.topic_id)
        )
        return [str(row.topic).strip() for row in query if str(row.topic or "").strip()]

    def queue_embedding(
        self,
        *,
        document_id: str,
        source_text: str,
        provider_id: str,
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
                provider_id=provider_id,
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
        instance.provider_id = provider_id
        instance.model_name = model_name
        instance.source_text_hash = source_text_hash
        instance.index_status = index_status
        instance.last_error = last_error
        instance.updated_at = now
        instance.save()
        return instance

    def retarget_embeddings(
        self,
        *,
        provider_id: str,
        model_name: str,
        embedding_kind: str = "primary",
    ) -> int:
        now = utc_now()
        query = MemoryEmbeddingModel.select().where(MemoryEmbeddingModel.embedding_kind == embedding_kind)
        updated = 0
        for instance in query:
            current_provider_id = str(getattr(instance, "provider_id", "") or "").strip()
            current_model_name = str(instance.model_name or "").strip()
            if current_provider_id == provider_id and current_model_name == model_name:
                continue
            instance.provider_id = provider_id
            instance.model_name = model_name
            instance.index_status = "stale"
            instance.last_error = None
            instance.updated_at = now
            instance.save()
            updated += 1
        return updated

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
                "provider_id": str(getattr(row, "provider_id", "") or ""),
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

    def _get_vector_rowid(self, embedding_id: str) -> int | None:
        db = MemoryEmbeddingVecModel._meta.database
        cursor = db.execute_sql(
            "SELECT rowid FROM memory_embedding_vec WHERE embedding_id = ?",
            (embedding_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return int(row[0])

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
        metadata = self.get_embedding(embedding_id)
        if metadata is not None:
            self._sync_vec_index_row(
                embedding_id=embedding_id,
                provider_id=str(getattr(metadata, "provider_id", "") or ""),
                model_name=str(metadata.model_name or ""),
                dimension=dimension,
                vector_blob=vector_blob,
            )

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
        scores, _ = self.collect_lexical_candidates(text, limit=limit)
        return scores

    def collect_lexical_candidates(self, text: str, *, limit: int) -> tuple[dict[str, float], dict[str, int]]:
        normalized = _normalize_query_text(text)
        empty_sources = {"fts_jieba": 0, "like": 0}
        if not normalized:
            return {}, empty_sources

        query_limit = max(limit * 2, 12)
        compiled_queries = _compile_fts_queries(normalized)
        fts_scores = self._run_fts_queries("memories_fts", compiled_queries, limit=query_limit)
        like_scores: dict[str, float] = {}
        if len(normalized) < 3 or not fts_scores:
            like_scores = self._run_like_candidates(normalized, limit=query_limit)

        combined: dict[str, float] = {}
        for source_scores in (fts_scores, like_scores):
            for document_id, score in source_scores.items():
                combined[document_id] = max(combined.get(document_id, 0.0), score)

        ordered = sorted(combined.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return dict(ordered), {
            "fts_jieba": len(fts_scores),
            "like": len(like_scores),
        }

    def _run_fts_queries(self, table_name: str, queries: list[tuple[str, float]], *, limit: int) -> dict[str, float]:
        if not queries:
            return {}
        db = MemoryFactModel._meta.database
        scores: dict[str, float] = {}
        for query_text, query_weight in queries:
            try:
                cursor = db.execute_sql(
                    f"""
                    SELECT document_id, -bm25({table_name}) AS score
                    FROM {table_name}
                    WHERE {table_name} MATCH ?
                    ORDER BY bm25({table_name})
                    LIMIT ?
                    """,
                    (query_text, limit),
                )
            except sqlite3.OperationalError:
                continue
            for document_id, score in cursor.fetchall():
                normalized_document_id = str(document_id)
                scores[normalized_document_id] = max(scores.get(normalized_document_id, 0.0), float(score) * float(query_weight))
        return scores

    def _run_like_candidates(self, text: str, *, limit: int) -> dict[str, float]:
        normalized = _normalize_query_text(text)
        if not normalized:
            return {}
        lowered = normalized.lower()
        db = MemoryFactModel._meta.database
        cursor = db.execute_sql(
            """
            SELECT
                document_id,
                CASE
                    WHEN instr(lower(title), ?) > 0 THEN 3.0 ELSE 0.0
                END
                + CASE
                    WHEN instr(lower(summary), ?) > 0 THEN 2.0 ELSE 0.0
                END
                + CASE
                    WHEN instr(lower(search_text), ?) > 0 THEN 1.0 ELSE 0.0
                END AS score
            FROM memory_document_projection
            WHERE instr(lower(title), ?) > 0
               OR instr(lower(summary), ?) > 0
               OR instr(lower(search_text), ?) > 0
            ORDER BY score DESC, document_id
            LIMIT ?
            """,
            (lowered, lowered, lowered, lowered, lowered, lowered, limit),
        )
        return {str(document_id): float(score) for document_id, score in cursor.fetchall() if float(score) > 0.0}

    def list_vector_rows(self, *, provider_id: str, model_name: str) -> list[tuple[MemoryEmbeddingModel, bytes]]:
        rows: list[tuple[MemoryEmbeddingModel, bytes]] = []
        query = (
            MemoryEmbeddingModel.select()
            .where(
                (MemoryEmbeddingModel.index_status == "ready")
                & (MemoryEmbeddingModel.provider_id == str(provider_id))
                & (MemoryEmbeddingModel.model_name == str(model_name))
            )
            .order_by(MemoryEmbeddingModel.embedding_id)
        )
        for metadata in query:
            blob = self.get_vector_blob(metadata.embedding_id)
            if blob is None:
                continue
            rows.append((metadata, blob))
        return rows

    def query_vector_candidates_sqlite_vec(
        self,
        *,
        provider_id: str,
        model_name: str,
        query_vector: list[float],
        limit: int,
    ) -> dict[str, float] | None:
        if not query_vector:
            return {}
        sqlite_vec_status = ensure_sqlite_vec_loaded()
        if not sqlite_vec_status.available:
            return None
        dimension = len(query_vector)
        table_name = self.ensure_vec_index_synced(
            provider_id=provider_id,
            model_name=model_name,
            dimension=dimension,
        )
        if not table_name:
            return {}
        db = MemoryEmbeddingModel._meta.database
        query_payload = json.dumps([float(value) for value in query_vector], ensure_ascii=True, separators=(",", ":"))
        try:
            cursor = db.execute_sql(
                f"""
                SELECT me.document_id, matches.distance
                FROM (
                    SELECT rowid, distance
                    FROM {table_name}
                    WHERE embedding MATCH ?
                    ORDER BY distance
                    LIMIT ?
                ) AS matches
                JOIN memory_embedding_vec mev ON mev.rowid = matches.rowid
                JOIN memory_embeddings me ON me.embedding_id = mev.embedding_id
                WHERE me.index_status = 'ready'
                  AND me.provider_id = ?
                  AND me.model_name = ?
                ORDER BY matches.distance, me.document_id
                """,
                (query_payload, limit, str(provider_id), str(model_name)),
            )
        except sqlite3.OperationalError:
            return None
        scores: dict[str, float] = {}
        for document_id, distance in cursor.fetchall():
            normalized_document_id = str(document_id)
            distance_value = float(distance)
            score = 1.0 / (1.0 + max(distance_value, 0.0))
            scores[normalized_document_id] = max(scores.get(normalized_document_id, 0.0), score)
        return scores

    def ensure_vec_index_synced(self, *, provider_id: str, model_name: str, dimension: int) -> str | None:
        if dimension <= 0:
            return None
        sqlite_vec_status = ensure_sqlite_vec_loaded()
        if not sqlite_vec_status.available:
            return None
        table_name = self._vec_index_table_name(provider_id=provider_id, model_name=model_name, dimension=dimension)
        self._ensure_vec_index_table(table_name=table_name, dimension=dimension)
        ready_count = self._count_ready_vector_group(
            provider_id=provider_id,
            model_name=model_name,
            dimension=dimension,
        )
        if ready_count <= 0:
            return table_name
        index_count = self._count_vec_index_rows(table_name)
        if index_count != ready_count:
            self.rebuild_vec_index_group(
                provider_id=provider_id,
                model_name=model_name,
                dimension=dimension,
                table_name=table_name,
            )
        return table_name

    def rebuild_vec_index_group(
        self,
        *,
        provider_id: str,
        model_name: str,
        dimension: int,
        table_name: str | None = None,
    ) -> str | None:
        sqlite_vec_status = ensure_sqlite_vec_loaded()
        if not sqlite_vec_status.available:
            return None
        resolved_table_name = table_name or self._vec_index_table_name(
            provider_id=provider_id,
            model_name=model_name,
            dimension=dimension,
        )
        self._ensure_vec_index_table(table_name=resolved_table_name, dimension=dimension)
        db = MemoryEmbeddingVecModel._meta.database
        db.execute_sql(f"DELETE FROM {resolved_table_name}")
        for rowid, vector_blob in self._list_ready_vector_group_rows(
            provider_id=provider_id,
            model_name=model_name,
            dimension=dimension,
        ):
            db.execute_sql(
                f"INSERT INTO {resolved_table_name}(rowid, embedding) VALUES (?, ?)",
                (rowid, bytes(vector_blob).decode("utf-8")),
            )
        return resolved_table_name

    def _sync_vec_index_row(
        self,
        *,
        embedding_id: str,
        provider_id: str,
        model_name: str,
        dimension: int,
        vector_blob: bytes,
    ) -> None:
        sqlite_vec_status = ensure_sqlite_vec_loaded()
        if not sqlite_vec_status.available or dimension <= 0:
            return
        rowid = self._get_vector_rowid(embedding_id)
        if rowid is None:
            return
        table_name = self._vec_index_table_name(provider_id=provider_id, model_name=model_name, dimension=dimension)
        self._ensure_vec_index_table(table_name=table_name, dimension=dimension)
        db = MemoryEmbeddingVecModel._meta.database
        try:
            db.execute_sql(f"DELETE FROM {table_name} WHERE rowid = ?", (rowid,))
            db.execute_sql(
                f"INSERT INTO {table_name}(rowid, embedding) VALUES (?, ?)",
                (rowid, bytes(vector_blob).decode("utf-8")),
            )
        except sqlite3.OperationalError:
            return

    def _ensure_vec_index_table(self, *, table_name: str, dimension: int) -> None:
        db = MemoryEmbeddingVecModel._meta.database
        db.execute_sql(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} USING vec0(embedding float[{int(dimension)}])"
        )

    def _vec_index_table_name(self, *, provider_id: str, model_name: str, dimension: int) -> str:
        suffix = hashlib.sha1(
            f"{provider_id}\x1f{model_name}\x1f{dimension}".encode("utf-8"),
        ).hexdigest()[:16]
        return f"memory_vec_idx_{int(dimension)}_{suffix}"

    def _count_ready_vector_group(self, *, provider_id: str, model_name: str, dimension: int) -> int:
        db = MemoryEmbeddingModel._meta.database
        cursor = db.execute_sql(
            """
            SELECT COUNT(*)
            FROM memory_embeddings me
            JOIN memory_embedding_vec mev ON mev.embedding_id = me.embedding_id
            WHERE me.index_status = 'ready'
              AND me.provider_id = ?
              AND me.model_name = ?
              AND mev.dimension = ?
            """,
            (str(provider_id), str(model_name), int(dimension)),
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def _count_vec_index_rows(self, table_name: str) -> int:
        db = MemoryEmbeddingVecModel._meta.database
        try:
            cursor = db.execute_sql(f"SELECT COUNT(*) FROM {table_name}")
        except sqlite3.OperationalError:
            return 0
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def _list_ready_vector_group_rows(
        self,
        *,
        provider_id: str,
        model_name: str,
        dimension: int,
    ) -> list[tuple[int, bytes]]:
        db = MemoryEmbeddingModel._meta.database
        cursor = db.execute_sql(
            """
            SELECT mev.rowid, mev.vector_blob
            FROM memory_embeddings me
            JOIN memory_embedding_vec mev ON mev.embedding_id = me.embedding_id
            WHERE me.index_status = 'ready'
              AND me.provider_id = ?
              AND me.model_name = ?
              AND mev.dimension = ?
            ORDER BY me.embedding_id
            """,
            (str(provider_id), str(model_name), int(dimension)),
        )
        rows: list[tuple[int, bytes]] = []
        for rowid, vector_blob in cursor.fetchall():
            if vector_blob is None:
                continue
            rows.append((int(rowid), bytes(vector_blob)))
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


def _normalize_query_text(text: str) -> str:
    return normalize_search_text(text)


def _compile_fts_queries(text: str) -> list[tuple[str, float]]:
    return compile_jieba_fts_queries(text)


def serialize_vector(vector: list[float]) -> bytes:
    return bytes(json.dumps([float(value) for value in vector]).encode("utf-8"))


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
