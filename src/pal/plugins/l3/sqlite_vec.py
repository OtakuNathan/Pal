from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import InitVar, dataclass, field
from typing import Any

from pal.foundation import utc_now
from pal.memory import (
    L2Entry,
    L3CommitRequest,
    L3CorrectRequest,
    L3DeleteRequest,
    L3MutationResult,
    L3RecallResult,
    L3RetireResult,
    MemoryQuery,
)
from pal.memory.contracts import RECALL_PROMOTION_THRESHOLD, VECTOR_DEDUP_THRESHOLD
from pal.memory.embedding import EmbeddingProviderPort, OllamaEmbeddingProvider
from pal.memory.repository import (
    MemoryDurableRepository,
    cosine_similarity,
    deserialize_vector,
    normalize_topic,
    parse_iso_timestamp,
    serialize_vector,
)
from pal.memory.rendering import (
    build_mutation_structured_payload,
    build_recall_structured_payload,
    normalize_recall_view,
    render_mutation_result_for_llm,
    render_recall_result_for_llm,
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


def _read_mem_ref(args: dict[str, Any]) -> str:
    return str(args.get("mem_ref") or args.get("document_id") or "").strip()


def _stable_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _stable_fact_fingerprint(
    *,
    title: str,
    summary: str,
    payload: dict[str, Any],
    canonical_key: str | None,
    scope: str,
    task_id: str | None,
) -> str:
    return _stable_hash(
        {
            "canonical_key": canonical_key or "",
            "kind": "fact",
            "payload": payload,
            "scope": scope,
            "summary": summary,
            "task_id": task_id or "",
            "title": title,
        }
    )


def _stable_case_fingerprint(
    *,
    title: str,
    summary: str,
    situation_text: str,
    task_text: str,
    action_text: str,
    result_text: str,
    payload: dict[str, Any],
    scope: str,
    task_id: str | None,
) -> str:
    return _stable_hash(
        {
            "action_text": action_text,
            "kind": "case",
            "payload": payload,
            "result_text": result_text,
            "scope": scope,
            "situation_text": situation_text,
            "summary": summary,
            "task_id": task_id or "",
            "task_text": task_text,
            "title": title,
        }
    )


def _extract_entry_topics(entry: L2Entry) -> list[str]:
    raw_topics = entry.payload.get("topics") if isinstance(entry.payload, dict) else None
    if not isinstance(raw_topics, list):
        return []
    return [str(value).strip() for value in raw_topics if str(value).strip()]


@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="provider",
    kind="provider",
    source="plugin:l3",
    target_kind="provider",
    path_module_id="memory",
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
    path_module_id="memory",
    iterable_resolver="iter_providers",
    target_id_resolver="resolve_provider_id",
    target_label_resolver="resolve_provider_label",
)
@dataclass
class SQLiteVecL3Plugin:
    service: MemoryService
    repository: MemoryDurableRepository = field(default_factory=MemoryDurableRepository)
    embedding_provider: EmbeddingProviderPort | None = None
    embedder: InitVar[EmbeddingProviderPort | None] = None
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
    last_embedding_error: str = ""

    def __post_init__(self, embedder: EmbeddingProviderPort | None) -> None:
        self.repository.ensure_schema()
        if self.embedding_provider is None and embedder is not None:
            self.embedding_provider = embedder
        if self.embedding_provider is None:
            self.embedding_provider = OllamaEmbeddingProvider()

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
        provider_health = self._embedding_health()
        inventory.update(
            {
                "provider_id": self.provider_id,
                "mounted": self.mounted,
                "vector_backend": "sqlite-vec" if sqlite_vec_status.available else "python-fallback",
                "vector_backend_detail": sqlite_vec_status.detail,
                "embedding_provider_id": self._embedding_provider_id(),
                "embedding_model": self._embedding_model_name(),
                "embedding_transport": self._embedding_transport(),
                "embedding_health": provider_health,
                "last_embedding_error": self.last_embedding_error,
            }
        )
        return inventory

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="provider", action_name="show", description="Show sqlite-backed memory provider state")
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = self.inspect()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="sqlite vec memory provider",
            structured=payload,
            llm_text=render_titled_structured_for_llm("SQLite vec memory provider", payload),
        )

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="provider", action_name="inventory", description="Inspect sqlite-backed memory inventory")
    def inventory(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = self.inspect()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="sqlite vec memory inventory",
            structured=payload,
            llm_text=render_titled_structured_for_llm("SQLite vec memory inventory", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="recall",
        action_name="recall",
        description=(
            "Recall durable memory records by searching against the source-of-truth text. "
            "queries: natural language search terms — the system will match against original verbatim facts. "
            "Provide descriptive, specific queries for best recall results."
        ),
        metadata={"omit_family_in_canonical": True},
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
                "view": {"type": "string", "enum": ["summary", "origin"]},
            },
        },
    )
    def recall_query(self, call: IntrospectionCall) -> IntrospectionResult:
        query = MemoryQuery(
            level=str(call.args.get("level") or "warm"),
            queries=[str(value) for value in list(call.args.get("queries") or [])],
            topic_scope=[str(value) for value in list(call.args.get("topic_scope") or [])],
            task_id=str(call.args.get("task_id")) if call.args.get("task_id") is not None else None,
            limit=int(call.args.get("limit") or 8),
            kind=str(call.args.get("kind")) if call.args.get("kind") is not None else None,
            scope=str(call.args.get("scope")) if call.args.get("scope") is not None else None,
            view=normalize_recall_view(call.args.get("view")),
        )
        result = self.recall(query)
        payload = build_recall_structured_payload(
            provider_id=self.provider_id,
            query=query,
            result=result,
            view=query.view,
        )
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="memory recall result",
            structured=payload,
            llm_text=render_recall_result_for_llm(
                provider_id=self.provider_id,
                query=query,
                result=result,
                view=query.view,
            ),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="commit",
        action_name="write",
        description=(
            "Commit a durable memory record. "
            "title: short label for this memory. "
            "summary: concise summary for future LLM consumption (compressed). "
            "search_text: the original verbatim fact or statement — source of truth for retrieval indexing. "
            "Do NOT omit or leave empty — all three are required for correct indexing."
        ),
        metadata={"omit_family_in_canonical": True},
        args_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "fact or case"},
                "title": {"type": "string", "description": "Short label for this memory"},
                "summary": {"type": "string", "description": "Concise summary (compressed, for prompt display)"},
                "search_text": {"type": "string", "description": "Original verbatim fact — source of truth, used for FTS and vector embedding"},
                "scope": {"type": "string", "description": "system or task"},
                "task_id": {"type": "string"},
                "canonical_key": {"type": "string"},
                "payload": {"type": "object"},
                "topics": {"type": "array", "items": {"type": "string"}, "description": "Topic tags for filtering"},
                "situation_text": {"type": "string", "description": "For case kind: situation description"},
                "task_text": {"type": "string", "description": "For case kind: task description"},
                "action_text": {"type": "string", "description": "For case kind: action taken"},
                "result_text": {"type": "string", "description": "For case kind: result outcome"},
            },
            "required": ["kind", "title", "summary", "search_text"],
        },
    )
    def commit_write(self, call: IntrospectionCall) -> IntrospectionResult:
        kind = str(call.args.get("kind") or "").strip()
        title = str(call.args.get("title") or "").strip()
        summary = str(call.args.get("summary") or "").strip()
        search_text = str(call.args.get("search_text") or "").strip()
        if not kind:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="kind is required")
        if not title:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="title is required")
        if not summary:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="summary is required")
        if not search_text:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="search_text is required")
        result = self.commit(
            L3CommitRequest(
                kind=kind,
                title=title,
                summary=summary,
                search_text=search_text,
                scope=str(call.args.get("scope") or "system"),
                task_id=str(call.args.get("task_id")) if call.args.get("task_id") is not None else None,
                canonical_key=str(call.args.get("canonical_key")) if call.args.get("canonical_key") is not None else None,
                payload=dict(call.args.get("payload") or {}),
                topics=[str(value) for value in list(call.args.get("topics") or [])],
                situation_text=str(call.args.get("situation_text") or ""),
                task_text=str(call.args.get("task_text") or ""),
                action_text=str(call.args.get("action_text") or ""),
                result_text=str(call.args.get("result_text") or ""),
            )
        )
        payload = build_mutation_structured_payload(result)
        return IntrospectionResult(
            status=result.status,
            text="memory commit result",
            structured=payload,
            llm_text=render_mutation_result_for_llm("commit", result),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="correct",
        action_name="update",
        description=(
            "Update an existing durable memory record. "
            "Only provided fields will be updated. "
            "search_text: updated source of truth for retrieval indexing."
        ),
        metadata={"omit_family_in_canonical": True},
        args_schema={
            "type": "object",
            "properties": {
                "mem_ref": {
                    "type": "string",
                    "description": "Opaque memory ref returned by memory_recall, such as fact:fact_abc or case:case_abc.",
                },
                "title": {"type": "string", "description": "Updated short label"},
                "summary": {"type": "string", "description": "Updated concise summary"},
                "search_text": {"type": "string", "description": "Updated source of truth — original verbatim fact for retrieval"},
                "payload_patch": {"type": "object", "description": "Merge patch for existing payload fields"},
                "topics": {"type": "array", "items": {"type": "string"}, "description": "Replacement topic tags"},
                "situation_text": {"type": "string"},
                "task_text": {"type": "string"},
                "action_text": {"type": "string"},
                "result_text": {"type": "string"},
            },
            "required": ["mem_ref"],
        },
    )
    def correct_patch(self, call: IntrospectionCall) -> IntrospectionResult:
        mem_ref = _read_mem_ref(call.args)
        result = self.correct(
            L3CorrectRequest(
                document_id=mem_ref,
                title=str(call.args.get("title")) if call.args.get("title") is not None else None,
                summary=str(call.args.get("summary")) if call.args.get("summary") is not None else None,
                search_text=str(call.args.get("search_text")) if call.args.get("search_text") is not None else None,
                payload_patch=dict(call.args.get("payload_patch") or {}),
                topics=[str(value) for value in list(call.args.get("topics") or [])] if call.args.get("topics") is not None else None,
                situation_text=str(call.args.get("situation_text")) if call.args.get("situation_text") is not None else None,
                task_text=str(call.args.get("task_text")) if call.args.get("task_text") is not None else None,
                action_text=str(call.args.get("action_text")) if call.args.get("action_text") is not None else None,
                result_text=str(call.args.get("result_text")) if call.args.get("result_text") is not None else None,
            )
        )
        payload = build_mutation_structured_payload(result)
        return IntrospectionResult(
            status=result.status,
            text="memory update result",
            structured=payload,
            llm_text=render_mutation_result_for_llm("update", result),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="delete",
        action_name="delete",
        description=(
            "Delete one durable memory record by exact mem_ref. "
            "Use only when the user explicitly asks to forget/delete a specific memory or a clearly invalid record."
        ),
        metadata={"omit_family_in_canonical": True},
        args_schema={
            "type": "object",
            "properties": {
                "mem_ref": {
                    "type": "string",
                    "description": "Opaque memory ref returned by memory_recall, such as fact:fact_abc or case:case_abc.",
                },
                "reason": {"type": "string", "description": "Brief reason for deletion"},
            },
            "required": ["mem_ref"],
        },
    )
    def delete_memory(self, call: IntrospectionCall) -> IntrospectionResult:
        mem_ref = _read_mem_ref(call.args)
        result = self.delete(
            L3DeleteRequest(
                document_id=mem_ref,
                reason=str(call.args.get("reason") or ""),
            )
        )
        payload = build_mutation_structured_payload(result)
        return IntrospectionResult(
            status=result.status,
            text="memory delete result",
            structured=payload,
            llm_text=render_mutation_result_for_llm("delete", result),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="provider", family="lifecycle", action_name="attach", description="Attach memory provider")
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = True
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="memory provider attached",
            structured={"mounted": True},
            llm_text=render_titled_structured_for_llm("Memory provider attached", {"mounted": True}),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="provider", family="lifecycle", action_name="detach", description="Detach memory provider")
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        self.mounted = False
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="memory provider detached",
            structured={"mounted": False},
            llm_text=render_titled_structured_for_llm("Memory provider detached", {"mounted": False}),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="maintenance",
        action_name="refresh_indexes",
        description="Refresh provider indexes and embedding state",
        metadata={"omit_family_in_canonical": True},
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

    def _find_vector_duplicate(self, search_text: str) -> tuple[dict[str, Any], float] | None:
        if not search_text or self.embedding_provider is None:
            return None
        candidates = self._vector_candidates(search_text, limit=1)
        if not candidates:
            return None
        doc_id, score = next(iter(candidates.items()))
        if score < VECTOR_DEDUP_THRESHOLD:
            return None
        hit = self.repository.get_document(doc_id)
        if hit is None:
            return None
        return hit, score

    def _confirm_duplicate(self, candidate: dict[str, Any], request: L3CommitRequest) -> bool:
        candidate_key = str(candidate.get("canonical_key") or "").strip()
        if candidate_key and candidate_key == (request.canonical_key or ""):
            return True
        candidate_title = str(candidate.get("title") or "").strip().lower()
        candidate_topics = set(str(t).strip().lower() for t in (candidate.get("topics") or []))
        request_title = str(request.title or "").strip().lower()
        request_topics = set(str(t).strip().lower() for t in request.topics)
        if candidate_title and request_title and candidate_title == request_title:
            return True
        if candidate_topics and request_topics and len(candidate_topics & request_topics) >= 2:
            return True
        return False

    def _merge_from_commit(self, candidate: dict[str, Any], request: L3CommitRequest) -> L3MutationResult:
        document_id = str(candidate.get("document_id", ""))
        correct_request = L3CorrectRequest(
            document_id=document_id,
            title=request.title or None,
            summary=request.summary or None,
            search_text=request.search_text or None,
            topics=request.topics or None,
        )
        return self.correct(correct_request)

    def commit(self, request: L3CommitRequest) -> L3MutationResult:
        if not self.mounted:
            return L3MutationResult(status=RuntimeStatus.UNAVAILABLE, document_id="")
        search_text = request.search_text or request.summary or request.title or ""
        vector_match = self._find_vector_duplicate(search_text)
        if vector_match is not None:
            candidate, score = vector_match
            print(f"[memory] memory_vector_dedup_candidate new_title={request.title} candidate_id={candidate.get('document_id')} score={score:.3f}")
            if self._confirm_duplicate(candidate, request):
                print(f"[memory] memory_vector_dedup_confirmed merged_into={candidate.get('document_id')} score={score:.3f}")
                return self._merge_from_commit(candidate, request)
        now = utc_now()
        if request.kind == "fact":
            summary = request.summary or request.title
            dedupe_fingerprint = request.dedupe_fingerprint or _stable_fact_fingerprint(
                title=request.title or "",
                summary=summary,
                payload=dict(request.payload),
                canonical_key=request.canonical_key,
                scope=request.scope,
                task_id=request.task_id,
            )
            existing = None
            if request.canonical_key:
                existing = self.repository.find_fact_by_canonical_key(request.canonical_key)
            if existing is None and dedupe_fingerprint:
                existing = self.repository.find_fact_by_dedupe_fingerprint(dedupe_fingerprint)
            fact_id = existing.fact_id if existing is not None else f"fact_{uuid.uuid4().hex[:12]}"
            model = self.repository.upsert_fact(
                fact_id=fact_id,
                payload={
                    "scope": request.scope,
                    "task_id": request.task_id,
                    "title": request.title,
                    "summary": summary,
                    "search_text": request.search_text or summary,
                    "canonical_key": request.canonical_key,
                    "dedupe_fingerprint": dedupe_fingerprint,
                    "payload_blob": dict(request.payload),
                    "last_used_at": now,
                },
            )
            document_id = f"fact:{model.fact_id}"
        elif request.kind == "case":
            summary = request.summary or request.title or request.task_text or request.situation_text
            dedupe_fingerprint = request.dedupe_fingerprint or _stable_case_fingerprint(
                title=request.title or "",
                summary=summary,
                situation_text=request.situation_text,
                task_text=request.task_text,
                action_text=request.action_text,
                result_text=request.result_text,
                payload=dict(request.payload),
                scope=request.scope,
                task_id=request.task_id,
            )
            existing = self.repository.find_case_by_dedupe_fingerprint(dedupe_fingerprint)
            case_id = existing.case_id if existing is not None else f"case_{uuid.uuid4().hex[:12]}"
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
                    "search_text": request.search_text or summary,
                    "dedupe_fingerprint": dedupe_fingerprint,
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

    def retire_entries(self, entries: list[L2Entry]) -> L3RetireResult:
        if not self.mounted:
            return L3RetireResult(status=RuntimeStatus.UNAVAILABLE)
        document_ids: list[str] = []
        reused_document_ids: list[str] = []
        for entry in entries:
            if entry.kind not in {"fact", "case"}:
                continue
            if entry.kind == "fact":
                existing = None
                if entry.canonical_key:
                    existing = self.repository.find_fact_by_canonical_key(entry.canonical_key)
                if existing is None and entry.dedupe_fingerprint:
                    existing = self.repository.find_fact_by_dedupe_fingerprint(entry.dedupe_fingerprint)
                if existing is not None:
                    document_id = f"fact:{existing.fact_id}"
                    reused_document_ids.append(document_id)
                    self._mark_document_pending(document_id, stale=True)
                    document_ids.append(document_id)
                    continue
                model = self.repository.upsert_fact(
                    fact_id=f"fact_{uuid.uuid4().hex[:12]}",
                    payload={
                        "scope": entry.scope,
                        "task_id": entry.task_id,
                        "title": entry.title,
                        "summary": entry.summary,
                        "search_text": entry.search_text or entry.summary,
                        "canonical_key": entry.canonical_key,
                        "dedupe_fingerprint": entry.dedupe_fingerprint,
                        "payload_blob": dict(entry.payload),
                        "last_used_at": utc_now(),
                    },
                )
                document_id = f"fact:{model.fact_id}"
            else:
                existing = self.repository.find_case_by_dedupe_fingerprint(entry.dedupe_fingerprint or "")
                if existing is not None:
                    document_id = f"case:{existing.case_id}"
                    reused_document_ids.append(document_id)
                    self._mark_document_pending(document_id, stale=True)
                    document_ids.append(document_id)
                    continue
                payload = dict(entry.payload or {})
                model = self.repository.upsert_case(
                    case_id=f"case_{uuid.uuid4().hex[:12]}",
                    payload={
                        "scope": entry.scope,
                        "task_id": entry.task_id,
                        "title": entry.title,
                        "summary": entry.summary,
                        "situation_text": str(payload.get("situation") or payload.get("situation_text") or ""),
                        "task_text": str(payload.get("task") or payload.get("task_text") or ""),
                        "action_text": str(payload.get("action") or payload.get("action_text") or ""),
                        "result_text": str(payload.get("result") or payload.get("result_text") or ""),
                        "search_text": entry.search_text or entry.summary,
                        "dedupe_fingerprint": entry.dedupe_fingerprint,
                        "payload_blob": payload,
                        "last_used_at": utc_now(),
                    },
                )
                document_id = f"case:{model.case_id}"
            topics = _extract_entry_topics(entry)
            self.repository.sync_fts_row(document_id)
            self.repository.replace_topics(document_id, topics)
            self._mark_document_pending(document_id)
            document_ids.append(document_id)
        return L3RetireResult(
            status=RuntimeStatus.OK,
            document_ids=document_ids,
            reused_document_ids=reused_document_ids,
            metadata={"retired": len(document_ids), "reused": len(reused_document_ids)},
        )

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
            if request.search_text is not None:
                model.search_text = request.search_text
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
            if request.search_text is not None:
                model.search_text = request.search_text
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

    def delete(self, request: L3DeleteRequest) -> L3MutationResult:
        if not self.mounted:
            return L3MutationResult(status=RuntimeStatus.UNAVAILABLE, document_id=request.document_id)
        document_id = str(request.document_id or "").strip()
        if not document_id:
            return L3MutationResult(status=RuntimeStatus.INVALID, document_id="")
        deleted = self.repository.delete_document(document_id)
        if deleted is None:
            return L3MutationResult(status=RuntimeStatus.NOT_FOUND, document_id=document_id)
        remove_projected_entries = getattr(self.service, "remove_projected_entries", None)
        if callable(remove_projected_entries):
            remove_projected_entries([document_id])
        payload = {
            "document_id": document_id,
            "deleted": True,
            "deleted_document": deleted,
            "reason": str(request.reason or ""),
        }
        return L3MutationResult(
            status=RuntimeStatus.OK,
            document_id=document_id,
            hit=payload,
            metadata={"deleted": True},
        )

    def recall(self, query: MemoryQuery) -> L3RecallResult:
        if not self.mounted:
            return L3RecallResult()
        refreshed = self.refresh_indexes(limit=min(max(query.limit, 1), 8))
        candidate_limit = max(query.limit * 4, 16)
        search_text = _stable_document_search_text(*query.queries)
        lexical_candidates, lexical_source_counts = self.repository.collect_lexical_candidates(
            search_text,
            limit=candidate_limit,
        )
        topic_candidates = self.repository.list_topic_candidates(query.topic_scope, limit=candidate_limit)
        vector_query_text = _stable_document_search_text(search_text, " ".join(query.topic_scope) if query.topic_scope else "")
        embedding_health = self._embedding_health() if vector_query_text else {"healthy": True}
        vector_candidates = self._vector_candidates(vector_query_text, limit=candidate_limit)
        candidate_ids = set(lexical_candidates) | set(topic_candidates) | set(vector_candidates)
        hits = []
        scored_hits = []
        docs = {document_id: self.repository.get_document(document_id) for document_id in candidate_ids}
        docs = {document_id: doc for document_id, doc in docs.items() if doc is not None}
        normalized_lexical = self._normalize_scores(lexical_candidates)
        normalized_topic = self._normalize_scores(topic_candidates)
        normalized_vector = self._normalize_scores(vector_candidates)
        lexical_rank = self._rank_fusion_scores(lexical_candidates)
        topic_rank = self._rank_fusion_scores(topic_candidates)
        vector_rank = self._rank_fusion_scores(vector_candidates)
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
                self.fusion_weights["fts"] * lexical_rank.get(document_id, 0.0)
                + self.fusion_weights["topic"] * topic_rank.get(document_id, 0.0)
                + self.fusion_weights["vector"] * vector_rank.get(document_id, 0.0)
                + self.fusion_weights["use_count"] * use_counts.get(document_id, 0.0)
                + self.fusion_weights["recency"] * recency.get(document_id, 0.0)
            )
            source_count = sum(
                1
                for source_score in (
                    lexical_rank.get(document_id, 0.0),
                    topic_rank.get(document_id, 0.0),
                    vector_rank.get(document_id, 0.0),
                )
                if source_score > 0.0
            )
            merged = dict(hit)
            merged["scores"] = {
                "fts": normalized_lexical.get(document_id, 0.0),
                "topic": normalized_topic.get(document_id, 0.0),
                "vector": normalized_vector.get(document_id, 0.0),
                "fts_rank": lexical_rank.get(document_id, 0.0),
                "topic_rank": topic_rank.get(document_id, 0.0),
                "vector_rank": vector_rank.get(document_id, 0.0),
                "use_count": use_counts.get(document_id, 0.0),
                "recency": recency.get(document_id, 0.0),
                "source_count": float(source_count),
                "final": final_score,
            }
            scored_hits.append((final_score, document_id, merged))
        scored_hits.sort(key=lambda item: (-item[0], item[1]))
        limited = scored_hits[: max(query.limit, 1)]
        hits = [item[2] for item in limited]
        projected_entries = [self._project_entry(hit, source_kind="l3_recall", candidate_state="candidate") for hit in hits]
        self.repository.bump_usage([hit["document_id"] for hit in hits])
        hot_entries = [entry for entry, (_, doc_id, hit) in zip(projected_entries, limited) if hit.get("scores", {}).get("final", 0) >= RECALL_PROMOTION_THRESHOLD]
        cool_entries = [entry for entry in projected_entries if entry not in hot_entries]
        if hot_entries:
            self.service.project_l3_entries(hot_entries, touch=True, top_of_mind=True)
        if cool_entries:
            self.service.project_l3_entries(cool_entries, touch=True, top_of_mind=False)
        degraded_reason = str(self.last_embedding_error or embedding_health.get("last_error") or "").strip()
        degraded = bool(vector_query_text) and bool(degraded_reason)
        return L3RecallResult(
            hits=hits,
            projected_entries=projected_entries,
            metadata={
                "refreshed_embeddings": refreshed.get("refreshed", 0),
                "vector_available": refreshed.get("vector_available", False),
                "candidate_count": len(candidate_ids),
                "candidate_sources": {
                    "vector": len(vector_candidates),
                    "topic": len(topic_candidates),
                    "fts_jieba": lexical_source_counts.get("fts_jieba", 0),
                    "like": lexical_source_counts.get("like", 0),
                },
                "retrieval_mode": self._derive_retrieval_mode(
                    lexical_candidates=lexical_candidates,
                    topic_candidates=topic_candidates,
                    vector_candidates=vector_candidates,
                ),
                "fusion_strategy": "ranked-hybrid-v1",
                "degraded": degraded,
                "degraded_reason": degraded_reason if degraded else "",
                "embedding_healthy": bool(embedding_health.get("healthy", True)),
            },
        )

    def refresh_indexes(self, *, limit: int = 8, retry_failed: bool = False) -> dict[str, Any]:
        if self.embedding_provider is None:
            return {"refreshed": 0, "vector_available": False, "detail": "embedding-provider-unavailable"}
        active_provider_id = self._embedding_provider_id()
        active_model_name = self._embedding_model_name()
        retargeted = self.repository.retarget_embeddings(
            provider_id=active_provider_id,
            model_name=active_model_name,
        )
        refreshed = 0
        failed_retried = 0
        failed_again = 0
        pending: list[tuple[Any, dict[str, Any]]] = []
        source_texts: list[str] = []
        for metadata in self.repository.list_retryable_embeddings(limit=limit, retry_failed=retry_failed):
            document = self.repository.get_document(metadata.document_id)
            if document is None:
                continue
            if metadata.index_status == "failed":
                failed_retried += 1
            pending.append((metadata, document))
            source_texts.append(str(document.get("search_text") or ""))
        try:
            vectors = self.embedding_provider.embed_documents(source_texts) if source_texts else []
            if source_texts and len(vectors) != len(source_texts):
                raise RuntimeError("embedding provider returned mismatched batch size")
            self.last_embedding_error = ""
        except Exception as exc:
            self.last_embedding_error = str(exc)
            for metadata, _ in pending:
                self.repository.mark_embedding_failed(embedding_id=metadata.embedding_id, error_text=self.last_embedding_error)
                failed_again += 1
            sqlite_vec_status = ensure_sqlite_vec_loaded()
            return {
                "refreshed": 0,
                "vector_available": bool(sqlite_vec_status.available),
                "vector_backend_detail": sqlite_vec_status.detail,
                "retry_failed": retry_failed,
                "failed_retried": failed_retried,
                "failed_again": failed_again,
                "retargeted": retargeted,
                "embedding_provider_id": active_provider_id,
                "embedding_model": active_model_name,
                "last_embedding_error": self.last_embedding_error,
            }
        for (metadata, _), vector in zip(pending, vectors):
            self.repository.upsert_vector_blob(
                embedding_id=metadata.embedding_id,
                vector_blob=serialize_vector(vector),
                dimension=len(vector),
            )
            norm = math.sqrt(sum(value * value for value in vector))
            self.repository.mark_embedding_ready(embedding_id=metadata.embedding_id, norm=norm)
            refreshed += 1
        sqlite_vec_status = ensure_sqlite_vec_loaded()
        return {
            "refreshed": refreshed,
            "vector_available": bool(sqlite_vec_status.available),
            "vector_backend_detail": sqlite_vec_status.detail,
            "retry_failed": retry_failed,
            "failed_retried": failed_retried,
            "failed_again": failed_again,
            "retargeted": retargeted,
            "embedding_provider_id": active_provider_id,
            "embedding_model": active_model_name,
            "last_embedding_error": self.last_embedding_error,
        }

    def _mark_document_pending(self, document_id: str, *, stale: bool = False) -> None:
        document = self.repository.get_document(document_id)
        if document is None:
            return
        self.repository.queue_embedding(
            document_id=document_id,
            source_text=str(document.get("search_text") or ""),
            provider_id=self._embedding_provider_id(),
            model_name=self._embedding_model_name(),
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
            search_text=str(hit.get("search_text") or ""),
            canonical_key=str(hit.get("canonical_key")) if hit.get("canonical_key") is not None else None,
            dedupe_fingerprint=str(hit.get("dedupe_fingerprint")) if hit.get("dedupe_fingerprint") is not None else None,
            payload=dict(hit.get("payload") or {}),
        )

    def _vector_candidates(self, search_text: str, *, limit: int) -> dict[str, float]:
        if not search_text or self.embedding_provider is None:
            return {}
        try:
            query_vector = self.embedding_provider.embed_query(search_text)
            self.last_embedding_error = ""
        except Exception as exc:
            self.last_embedding_error = str(exc)
            return {}
        sqlite_vec_scores = self.repository.query_vector_candidates_sqlite_vec(
            provider_id=self._embedding_provider_id(),
            model_name=self._embedding_model_name(),
            query_vector=query_vector,
            limit=limit,
        )
        if sqlite_vec_scores is not None:
            return sqlite_vec_scores
        scores: dict[str, float] = {}
        for metadata, blob in self.repository.list_vector_rows(
            provider_id=self._embedding_provider_id(),
            model_name=self._embedding_model_name(),
        ):
            candidate = deserialize_vector(blob)
            similarity = cosine_similarity(query_vector, candidate)
            if similarity < self.min_vector_similarity:
                continue
            scores[metadata.document_id] = max(scores.get(metadata.document_id, 0.0), similarity)
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
        return dict(ordered)

    def _derive_retrieval_mode(
        self,
        *,
        lexical_candidates: dict[str, float],
        topic_candidates: dict[str, float],
        vector_candidates: dict[str, float],
    ) -> str:
        active_sources = [
            name
            for name, payload in (
                ("vector", vector_candidates),
                ("topic", topic_candidates),
                ("lexical", lexical_candidates),
            )
            if payload
        ]
        if not active_sources:
            return "none"
        if len(active_sources) == 1:
            return active_sources[0]
        return "+".join(active_sources)

    def _embedding_provider_id(self) -> str:
        return str(getattr(self.embedding_provider, "provider_id", "unavailable") or "unavailable")

    def _embedding_model_name(self) -> str:
        return str(getattr(self.embedding_provider, "model_name", "unavailable") or "unavailable")

    def _embedding_transport(self) -> str:
        return str(getattr(self.embedding_provider, "transport", "unknown") or "unknown")

    def _embedding_health(self) -> dict[str, Any]:
        provider = self.embedding_provider
        if provider is None:
            return {
                "healthy": False,
                "provider_id": "unavailable",
                "transport": "unknown",
                "model_name": "unavailable",
                "last_error": "embedding provider is not configured",
            }
        try:
            payload = provider.health()
        except Exception as exc:
            payload = {
                "healthy": False,
                "provider_id": self._embedding_provider_id(),
                "transport": self._embedding_transport(),
                "model_name": self._embedding_model_name(),
                "last_error": str(exc),
            }
        if not bool(payload.get("healthy")) and payload.get("last_error"):
            self.last_embedding_error = str(payload.get("last_error"))
        return payload

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

    def _rank_fusion_scores(self, scores: dict[str, float]) -> dict[str, float]:
        if not scores:
            return {}
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return {
            document_id: 1.0 / float(rank)
            for rank, (document_id, _) in enumerate(ordered, start=1)
        }
