from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Any

from pal.foundation import utc_now
from pal.memory import (
    L2Entry,
    L3CommitRequest,
    L3CorrectRequest,
    L3MutationResult,
    L3RecallResult,
    MemoryQuery,
    MemoryService,
)
from pal.memory.embedding import EmbeddingRuntimePort, SentenceTransformerBGEEmbedder
from pal.memory.repository import (
    MemoryDurableRepository,
    cosine_similarity,
    deserialize_vector,
    normalize_topic,
    parse_iso_timestamp,
    serialize_vector,
)
from pal.memory.schema import ensure_sqlite_vec_loaded
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    LLMStreamEventKind,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm


def _stable_document_search_text(*parts: str) -> str:
    return "\n".join(part.strip() for part in parts if str(part or "").strip())


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="provider",
    kind="provider",
    source="plugin:l3",
    target_kind="provider",
    path_module_id="l3",
    iterable_resolver="iter_providers",
    target_id_resolver="resolve_provider_id",
    target_label_resolver="resolve_provider_label",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="provider",
    kind="provider",
    source="plugin:l3",
    target_kind="provider",
    path_module_id="l3",
    iterable_resolver="iter_providers",
    target_id_resolver="resolve_provider_id",
    target_label_resolver="resolve_provider_label",
)
@dataclass
class SQLiteVecL3Plugin:
    service: MemoryService
    repository: MemoryDurableRepository = field(default_factory=MemoryDurableRepository)
    embedder: EmbeddingRuntimePort | None = None
    provider_id: str = "sqlite_vec_l3"
    mounted: bool = True
    fusion_weights: dict[str, float] = field(
        default_factory=lambda: {
            "vector": 0.40,
            "fts": 0.35,
            "topic": 0.15,
            "use_count": 0.05,
            "recency": 0.05,
        }
    )
    min_vector_similarity: float = 0.35

    def __post_init__(self) -> None:
        self.repository.ensure_schema()
        if self.embedder is None:
            self.embedder = SentenceTransformerBGEEmbedder()

    @property
    def module_id(self) -> str:
        return f"l3.{self.provider_id}"

    def iter_providers(self) -> list["SQLiteVecL3Plugin"]:
        return [self]

    def resolve_provider_id(self, provider: "SQLiteVecL3Plugin") -> str:
        return provider.provider_id

    def resolve_provider_label(self, provider: "SQLiteVecL3Plugin") -> str:
        return provider.provider_id

    def inspect(self) -> dict[str, Any]:
        sqlite_vec_status = ensure_sqlite_vec_loaded()
        inventory = self.repository.inventory()
        inventory.update(
            {
                "provider_id": self.provider_id,
                "mounted": self.mounted,
                "vector_backend": "sqlite-vec" if sqlite_vec_status.available else "python-fallback",
                "vector_backend_detail": sqlite_vec_status.detail,
                "embedding_model": getattr(self.embedder, "model_name", "unavailable"),
            }
        )
        return inventory

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="provider", action_name="show", description="Show sqlite-backed l3 provider state")
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = self.inspect()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="sqlite vec l3 provider",
            structured=payload,
            llm_text=render_titled_structured_for_llm("SQLite vec L3 provider", payload),
        )

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="provider", action_name="inventory", description="Inspect sqlite-backed l3 inventory")
    def inventory(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = self.inspect()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="sqlite vec l3 inventory",
            structured=payload,
            llm_text=render_titled_structured_for_llm("SQLite vec L3 inventory", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="recall",
        action_name="query",
        description="Recall durable L3 memory records",
        args_schema={
            "type": "object",
            "properties": {
                "level": {"type": "string"},
                "queries": {"type": "array", "items": {"type": "string"}},
                "topic_scope": {"type": "array", "items": {"type": "string"}},
                "task_id": {"type": "string"},
                "limit": {"type": "integer"},
                "kind": {"type": "string"},
                "scope": {"type": "string"},
            },
        },
    )
    def recall_query(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.recall(
            MemoryQuery(
                level=str(call.args.get("level") or "warm"),
                queries=[str(value) for value in list(call.args.get("queries") or [])],
                topic_scope=[str(value) for value in list(call.args.get("topic_scope") or [])],
                task_id=str(call.args.get("task_id")) if call.args.get("task_id") is not None else None,
                limit=int(call.args.get("limit") or 8),
                kind=str(call.args.get("kind")) if call.args.get("kind") is not None else None,
                scope=str(call.args.get("scope")) if call.args.get("scope") is not None else None,
            )
        )
        payload = {
            "hits": result.hits,
            "projected_entries": [entry.__dict__ for entry in result.projected_entries],
            "metadata": result.metadata,
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="l3 recall result",
            structured=payload,
            llm_text=render_titled_structured_for_llm("L3 recall result", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="commit",
        action_name="write",
        description="Commit durable L3 memory",
        args_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "scope": {"type": "string"},
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "canonical_key": {"type": "string"},
                "payload": {"type": "object"},
                "topics": {"type": "array", "items": {"type": "string"}},
                "situation_text": {"type": "string"},
                "task_text": {"type": "string"},
                "action_text": {"type": "string"},
                "result_text": {"type": "string"},
            },
            "required": ["kind"],
        },
    )
    def commit_write(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.commit(
            L3CommitRequest(
                kind=str(call.args.get("kind") or ""),
                scope=str(call.args.get("scope") or "system"),
                task_id=str(call.args.get("task_id")) if call.args.get("task_id") is not None else None,
                title=str(call.args.get("title")) if call.args.get("title") is not None else None,
                summary=str(call.args.get("summary") or ""),
                canonical_key=str(call.args.get("canonical_key")) if call.args.get("canonical_key") is not None else None,
                payload=dict(call.args.get("payload") or {}),
                topics=[str(value) for value in list(call.args.get("topics") or [])],
                situation_text=str(call.args.get("situation_text") or ""),
                task_text=str(call.args.get("task_text") or ""),
                action_text=str(call.args.get("action_text") or ""),
                result_text=str(call.args.get("result_text") or ""),
            )
        )
        payload = result.hit | {"metadata": result.metadata}
        return IntrospectionResult(
            status=result.status,
            text="l3 commit result",
            structured=payload,
            llm_text=render_titled_structured_for_llm("L3 commit result", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="correct",
        action_name="patch",
        description="Correct durable L3 memory",
        args_schema={
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "payload_patch": {"type": "object"},
                "topics": {"type": "array", "items": {"type": "string"}},
                "situation_text": {"type": "string"},
                "task_text": {"type": "string"},
                "action_text": {"type": "string"},
                "result_text": {"type": "string"},
            },
            "required": ["document_id"],
        },
    )
    def correct_patch(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.correct(
            L3CorrectRequest(
                document_id=str(call.args.get("document_id") or ""),
                title=str(call.args.get("title")) if call.args.get("title") is not None else None,
                summary=str(call.args.get("summary")) if call.args.get("summary") is not None else None,
                payload_patch=dict(call.args.get("payload_patch") or {}),
                topics=[str(value) for value in list(call.args.get("topics") or [])] if call.args.get("topics") is not None else None,
                situation_text=str(call.args.get("situation_text")) if call.args.get("situation_text") is not None else None,
                task_text=str(call.args.get("task_text")) if call.args.get("task_text") is not None else None,
                action_text=str(call.args.get("action_text")) if call.args.get("action_text") is not None else None,
                result_text=str(call.args.get("result_text")) if call.args.get("result_text") is not None else None,
            )
        )
        payload = result.hit | {"metadata": result.metadata}
        return IntrospectionResult(
            status=result.status,
            text="l3 correction result",
            structured=payload,
            llm_text=render_titled_structured_for_llm("L3 correction result", payload),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="provider", family="lifecycle", action_name="attach", description="Attach l3 provider")
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = True
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="l3 provider attached",
            structured={"mounted": True},
            llm_text=render_titled_structured_for_llm("L3 provider attached", {"mounted": True}),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="provider", family="lifecycle", action_name="detach", description="Detach l3 provider")
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = False
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="l3 provider detached",
            structured={"mounted": False},
            llm_text=render_titled_structured_for_llm("L3 provider detached", {"mounted": False}),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="maintenance",
        action_name="refresh_indexes",
        description="Refresh provider indexes and embedding state",
        args_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
                "retry_failed": {"type": "boolean"},
            },
        },
    )
    def refresh_indexes_action(self, call: IntrospectionCall) -> IntrospectionResult:
        limit = int(call.args.get("limit") or 8)
        retry_failed = bool(call.args.get("retry_failed", False))
        refreshed = self.refresh_indexes(limit=limit, retry_failed=retry_failed)
        payload = dict(refreshed)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="l3 provider indexes refreshed",
            structured=payload,
            llm_text=render_titled_structured_for_llm("L3 provider indexes refreshed", payload),
        )

    def commit(self, request: L3CommitRequest) -> L3MutationResult:
        if not self.mounted:
            return L3MutationResult(status=RuntimeStatus.UNAVAILABLE, document_id="")
        now = utc_now()
        if request.kind == "fact":
            fact_id = f"fact_{uuid.uuid4().hex[:12]}"
            summary = request.summary or (request.title or "")
            topic_text = " ".join(request.topics) if request.topics else ""
            search_text = _stable_document_search_text(request.title or "", summary, topic_text)
            model = self.repository.upsert_fact(
                fact_id=fact_id,
                payload={
                    "scope": request.scope,
                    "task_id": request.task_id,
                    "title": request.title,
                    "summary": summary,
                    "search_text": search_text,
                    "canonical_key": request.canonical_key,
                    "dedupe_fingerprint": request.dedupe_fingerprint,
                    "payload_blob": dict(request.payload),
                    "last_used_at": now,
                },
            )
            document_id = f"fact:{model.fact_id}"
        elif request.kind == "case":
            case_id = f"case_{uuid.uuid4().hex[:12]}"
            summary = request.summary or (request.title or request.task_text or request.situation_text)
            topic_text = " ".join(request.topics) if request.topics else ""
            search_text = _stable_document_search_text(request.title or "", summary, request.situation_text, request.task_text, topic_text)
            model = self.repository.upsert_case(
                case_id=case_id,
                payload={
                    "scope": request.scope,
                    "task_id": request.task_id,
                    "title": request.title,
                    "summary": summary,
                    "situation_text": request.situation_text,
                    "task_text": request.task_text,
                    "action_text": request.action_text,
                    "result_text": request.result_text,
                    "search_text": search_text,
                    "dedupe_fingerprint": request.dedupe_fingerprint,
                    "payload_blob": dict(request.payload),
                    "last_used_at": now,
                },
            )
            document_id = f"case:{model.case_id}"
        else:
            return L3MutationResult(status=RuntimeStatus.INVALID, document_id="")
        self.repository.sync_fts_row(document_id)
        self.repository.replace_topics(document_id, request.topics)
        self._mark_document_pending(document_id)
        hit = self.repository.get_document(document_id) or {"document_id": document_id}
        entry = self._project_entry(hit, source_kind="explicit_commit", candidate_state="stable")
        result = L3MutationResult(
            status=RuntimeStatus.OK,
            document_id=document_id,
            hit=hit,
            projected_entry=entry,
            metadata={"index_status": "pending"},
        )
        self.service.project_mutation(result)
        return result

    def correct(self, request: L3CorrectRequest) -> L3MutationResult:
        if not self.mounted:
            return L3MutationResult(status=RuntimeStatus.UNAVAILABLE, document_id=request.document_id)
        kind, _, raw_id = request.document_id.partition(":")
        topic_values = list(request.topics) if request.topics is not None else self.repository.list_document_topics(request.document_id)
        topic_text = " ".join(topic_values) if topic_values else ""
        if kind == "fact":
            model = self.repository.get_fact(raw_id)
            if model is None:
                return L3MutationResult(status=RuntimeStatus.NOT_FOUND, document_id=request.document_id)
            if request.title is not None:
                model.title = request.title
            if request.summary is not None:
                model.summary = request.summary
            payload = dict(model.payload_blob or {})
            payload.update(request.payload_patch)
            model.payload_blob = payload
            model.search_text = _stable_document_search_text(model.title or "", model.summary, topic_text)
            model.updated_at = utc_now()
            model.save()
        elif kind == "case":
            model = self.repository.get_case(raw_id)
            if model is None:
                return L3MutationResult(status=RuntimeStatus.NOT_FOUND, document_id=request.document_id)
            if request.title is not None:
                model.title = request.title
            if request.summary is not None:
                model.summary = request.summary
            if request.situation_text is not None:
                model.situation_text = request.situation_text
            if request.task_text is not None:
                model.task_text = request.task_text
            if request.action_text is not None:
                model.action_text = request.action_text
            if request.result_text is not None:
                model.result_text = request.result_text
            payload = dict(model.payload_blob or {})
            payload.update(request.payload_patch)
            model.payload_blob = payload
            model.search_text = _stable_document_search_text(model.title or "", model.summary, model.situation_text, model.task_text, topic_text)
            model.updated_at = utc_now()
            model.save()
        else:
            return L3MutationResult(status=RuntimeStatus.NOT_FOUND, document_id=request.document_id)
        if request.topics is not None:
            self.repository.replace_topics(request.document_id, request.topics)
        self.repository.sync_fts_row(request.document_id)
        self._mark_document_pending(request.document_id, stale=True)
        hit = self.repository.get_document(request.document_id) or {"document_id": request.document_id}
        entry = self._project_entry(hit, source_kind="correction", candidate_state="stable")
        result = L3MutationResult(
            status=RuntimeStatus.OK,
            document_id=request.document_id,
            hit=hit,
            projected_entry=entry,
            metadata={"index_status": "stale"},
        )
        self.service.project_mutation(result)
        return result

    def recall(self, query: MemoryQuery) -> L3RecallResult:
        if not self.mounted:
            return L3RecallResult()
        refreshed = self.refresh_indexes(limit=min(max(query.limit, 1), 8))
        search_text = _stable_document_search_text(*query.queries)
        fts_candidates = self.repository.list_fts_candidates(search_text, limit=max(query.limit * 3, 12))
        topic_candidates = self.repository.list_topic_candidates(query.topic_scope, limit=max(query.limit * 3, 12))
        vector_query_text = _stable_document_search_text(search_text, " ".join(query.topic_scope) if query.topic_scope else "")
        vector_candidates = self._vector_candidates(vector_query_text, limit=max(query.limit * 3, 12))
        candidate_ids = set(fts_candidates) | set(topic_candidates) | set(vector_candidates)
        hits = []
        scored_hits = []
        docs = {document_id: self.repository.get_document(document_id) for document_id in candidate_ids}
        docs = {document_id: doc for document_id, doc in docs.items() if doc is not None}
        normalized_fts = self._normalize_scores(fts_candidates)
        normalized_topic = self._normalize_scores(topic_candidates)
        normalized_vector = self._normalize_scores(vector_candidates)
        use_counts = self._normalize_scores({doc_id: float(doc.get("use_count", 0) or 0) for doc_id, doc in docs.items()}, log_scale=True)
        recency = self._normalize_recency({doc_id: str(doc.get("last_used_at") or doc.get("updated_at") or "") for doc_id, doc in docs.items()})
        for document_id, hit in docs.items():
            if query.kind and hit.get("document_kind") != query.kind:
                continue
            if query.scope and hit.get("scope") != query.scope:
                continue
            if query.task_id and hit.get("task_id") not in (None, query.task_id):
                continue
            final_score = (
                self.fusion_weights["fts"] * normalized_fts.get(document_id, 0.0)
                + self.fusion_weights["topic"] * normalized_topic.get(document_id, 0.0)
                + self.fusion_weights["vector"] * normalized_vector.get(document_id, 0.0)
                + self.fusion_weights["use_count"] * use_counts.get(document_id, 0.0)
                + self.fusion_weights["recency"] * recency.get(document_id, 0.0)
            )
            merged = dict(hit)
            merged["scores"] = {
                "fts": normalized_fts.get(document_id, 0.0),
                "topic": normalized_topic.get(document_id, 0.0),
                "vector": normalized_vector.get(document_id, 0.0),
                "use_count": use_counts.get(document_id, 0.0),
                "recency": recency.get(document_id, 0.0),
                "final": final_score,
            }
            scored_hits.append((final_score, document_id, merged))
        scored_hits.sort(key=lambda item: (-item[0], item[1]))
        limited = scored_hits[: max(query.limit, 1)]
        hits = [item[2] for item in limited]
        projected_entries = [self._project_entry(hit, source_kind="l3_recall", candidate_state="candidate") for hit in hits]
        self.repository.bump_usage([hit["document_id"] for hit in hits])
        self.service.project_l3_entries(projected_entries, touch=True)
        return L3RecallResult(
            hits=hits,
            projected_entries=projected_entries,
            metadata={
                "refreshed_embeddings": refreshed.get("refreshed", 0),
                "vector_available": refreshed.get("vector_available", False),
                "candidate_count": len(candidate_ids),
            },
        )

    def refresh_indexes(self, *, limit: int = 8, retry_failed: bool = False) -> dict[str, Any]:
        if self.embedder is None:
            return {"refreshed": 0, "vector_available": False, "detail": "embedder-unavailable"}
        refreshed = 0
        failed_retried = 0
        failed_again = 0
        for metadata in self.repository.list_retryable_embeddings(limit=limit, retry_failed=retry_failed):
            document = self.repository.get_document(metadata.document_id)
            if document is None:
                continue
            if metadata.index_status == "failed":
                failed_retried += 1
            try:
                vector = self.embedder.embed_document(str(document.get("search_text") or ""))
                self.repository.upsert_vector_blob(
                    embedding_id=metadata.embedding_id,
                    vector_blob=serialize_vector(vector),
                    dimension=len(vector),
                )
                norm = math.sqrt(sum(value * value for value in vector))
                self.repository.mark_embedding_ready(embedding_id=metadata.embedding_id, norm=norm)
                refreshed += 1
            except Exception as exc:
                self.repository.mark_embedding_failed(embedding_id=metadata.embedding_id, error_text=str(exc))
                failed_again += 1
        sqlite_vec_status = ensure_sqlite_vec_loaded()
        return {
            "refreshed": refreshed,
            "vector_available": bool(sqlite_vec_status.available and self.embedder is not None),
            "vector_backend_detail": sqlite_vec_status.detail,
            "retry_failed": retry_failed,
            "failed_retried": failed_retried,
            "failed_again": failed_again,
        }

    def _mark_document_pending(self, document_id: str, *, stale: bool = False) -> None:
        document = self.repository.get_document(document_id)
        if document is None:
            return
        self.repository.queue_embedding(
            document_id=document_id,
            source_text=str(document.get("search_text") or ""),
            model_name=getattr(self.embedder, "model_name", "unavailable"),
            index_status="stale" if stale else "pending",
        )

    def _project_entry(self, hit: dict[str, Any], *, source_kind: str, candidate_state: str) -> L2Entry:
        return L2Entry(
            entry_id=str(hit["document_id"]),
            kind=str(hit.get("document_kind", "fact")),
            scope=str(hit.get("scope", "system")),
            task_id=str(hit.get("task_id")) if hit.get("task_id") is not None else None,
            title=str(hit.get("title", "")),
            summary=str(hit.get("summary", "")),
            source_kind=source_kind,
            source_ref=str(hit["document_id"]),
            candidate_state=candidate_state,
            touched_at=utc_now(),
            rendered=str(hit.get("rendered") or hit.get("summary") or ""),
            payload=dict(hit.get("payload") or {}),
        )

    def _vector_candidates(self, search_text: str, *, limit: int) -> dict[str, float]:
        if not search_text or self.embedder is None:
            return {}
        try:
            query_vector = self.embedder.embed_query(search_text)
        except Exception:
            return {}
        scores: dict[str, float] = {}
        for metadata, blob in self.repository.list_vector_rows():
            candidate = deserialize_vector(blob)
            similarity = cosine_similarity(query_vector, candidate)
            if similarity < self.min_vector_similarity:
                continue
            scores[metadata.document_id] = max(scores.get(metadata.document_id, 0.0), similarity)
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return dict(ordered)

    def _normalize_scores(self, scores: dict[str, float], *, log_scale: bool = False) -> dict[str, float]:
        if not scores:
            return {}
        values = list(scores.values())
        if log_scale:
            values = [math.log1p(max(value, 0.0)) for value in values]
            scores = {key: math.log1p(max(value, 0.0)) for key, value in scores.items()}
        low = min(values)
        high = max(values)
        if math.isclose(low, high):
            return {key: 1.0 for key in scores}
        return {key: (value - low) / (high - low) for key, value in scores.items()}

    def _normalize_recency(self, values: dict[str, str]) -> dict[str, float]:
        parsed = {key: parse_iso_timestamp(value) for key, value in values.items()}
        timestamps = [value.timestamp() for value in parsed.values() if value is not None]
        if not timestamps:
            return {}
        low = min(timestamps)
        high = max(timestamps)
        if math.isclose(low, high):
            return {key: 1.0 for key, value in parsed.items() if value is not None}
        scores: dict[str, float] = {}
        for key, value in parsed.items():
            if value is None:
                scores[key] = 0.0
            else:
                scores[key] = (value.timestamp() - low) / (high - low)
        return scores
