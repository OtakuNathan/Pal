from __future__ import annotations

from dataclasses import dataclass, field

from pal.memory.contracts import (
    L2Entry,
    L3CommitRequest,
    L3CorrectRequest,
    L3MutationResult,
    L3RecallResult,
    MemoryQuery,
)
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm


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
class _L3ProviderCapabilityMixin:
    provider_id: str
    mounted: bool

    @property
    def module_id(self) -> str:
        return f"l3.{self.provider_id}"

    def iter_providers(self) -> list["_L3ProviderCapabilityMixin"]:
        return [self]

    def resolve_provider_id(self, provider: "_L3ProviderCapabilityMixin") -> str:
        return provider.provider_id

    def resolve_provider_label(self, provider: "_L3ProviderCapabilityMixin") -> str:
        return provider.provider_id

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="provider",
        action_name="show",
        description="Show l3 provider runtime state",
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = self.inspect()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="l3 provider",
            structured=payload,
            llm_text=render_titled_structured_for_llm("L3 provider", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="provider",
        action_name="inventory",
        description="Inspect l3 provider inventory and index status",
    )
    def inventory(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = self.inspect()
        snapshot.setdefault("provider_id", self.provider_id)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="l3 provider inventory",
            structured=snapshot,
            llm_text=render_titled_structured_for_llm("L3 provider inventory", snapshot),
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
            )
        )
        payload = {"hits": result.hits, "projected_entries": [entry.__dict__ for entry in result.projected_entries]}
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
                "topics": {"type": "array", "items": {"type": "string"}},
                "payload": {"type": "object"},
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
        payload = result.hit or {"document_id": result.document_id}
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
                "topics": {"type": "array", "items": {"type": "string"}},
                "payload_patch": {"type": "object"},
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
        payload = result.hit or {"document_id": result.document_id}
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


@dataclass
class NullL3Plugin(_L3ProviderCapabilityMixin):
    provider_id: str = "null_l3"
    mounted: bool = True

    def inspect(self) -> dict[str, object]:
        return {"provider_id": self.provider_id, "mounted": self.mounted, "vector_backend": "none", "record_count": 0}

    def recall(self, query: MemoryQuery) -> L3RecallResult:
        _ = query
        if not self.mounted:
            return L3RecallResult()
        return L3RecallResult()

    def commit(self, request: L3CommitRequest) -> L3MutationResult:
        _ = request
        return L3MutationResult(status=RuntimeStatus.SKIPPED, document_id="")

    def correct(self, request: L3CorrectRequest) -> L3MutationResult:
        _ = request
        return L3MutationResult(status=RuntimeStatus.NOT_FOUND, document_id="")

    def refresh_indexes(self, *, limit: int = 8, retry_failed: bool = False) -> dict[str, object]:
        _ = (limit, retry_failed)
        return {"refreshed": 0, "vector_available": False}


@dataclass
class MockL3Plugin(_L3ProviderCapabilityMixin):
    provider_id: str = "mock_l3"
    mounted: bool = True
    records: list[dict[str, object]] = field(default_factory=list)

    def inspect(self) -> dict[str, object]:
        return {"provider_id": self.provider_id, "mounted": self.mounted, "record_count": len(self.records), "vector_backend": "mock"}

    def recall(self, query: MemoryQuery) -> L3RecallResult:
        _ = query
        if not self.mounted:
            return L3RecallResult()
        entries = [
            L2Entry(
                entry_id=str(record.get("document_id")),
                kind=str(record.get("document_kind", "fact")),
                scope=str(record.get("scope", "system")),
                title=str(record.get("title", "")),
                summary=str(record.get("summary", record.get("title", ""))),
                source_ref=str(record.get("document_id", "")),
                rendered=str(record.get("rendered", record.get("summary", ""))),
                payload=dict(record),
            )
            for record in self.records
        ]
        return L3RecallResult(hits=list(self.records), projected_entries=entries)

    def commit(self, request: L3CommitRequest) -> L3MutationResult:
        document_id = f"{request.kind}:{len(self.records) + 1}"
        hit = {
            "document_id": document_id,
            "document_kind": request.kind,
            "scope": request.scope,
            "title": request.title or "",
            "summary": request.summary,
            "topics": list(request.topics),
            "payload": dict(request.payload),
        }
        self.records.append(hit)
        entry = L2Entry(
            entry_id=document_id,
            kind=request.kind,
            scope=request.scope,
            task_id=request.task_id,
            title=request.title or "",
            summary=request.summary,
            source_kind="explicit_commit",
            source_ref=document_id,
            candidate_state="stable",
            rendered=request.summary,
            payload=dict(hit),
        )
        return L3MutationResult(status=RuntimeStatus.OK, document_id=document_id, hit=hit, projected_entry=entry)

    def correct(self, request: L3CorrectRequest) -> L3MutationResult:
        for record in self.records:
            if str(record.get("document_id")) != request.document_id:
                continue
            if request.title is not None:
                record["title"] = request.title
            if request.summary is not None:
                record["summary"] = request.summary
            if request.topics is not None:
                record["topics"] = list(request.topics)
            if request.payload_patch:
                payload = dict(record.get("payload") or {})
                payload.update(request.payload_patch)
                record["payload"] = payload
            entry = L2Entry(
                entry_id=request.document_id,
                kind=str(record.get("document_kind", "fact")),
                scope=str(record.get("scope", "system")),
                title=str(record.get("title", "")),
                summary=str(record.get("summary", "")),
                source_kind="correction",
                source_ref=request.document_id,
                candidate_state="stable",
                rendered=str(record.get("summary", "")),
                payload=dict(record),
            )
            return L3MutationResult(status=RuntimeStatus.OK, document_id=request.document_id, hit=dict(record), projected_entry=entry)
        return L3MutationResult(status=RuntimeStatus.NOT_FOUND, document_id=request.document_id)

    def refresh_indexes(self, *, limit: int = 8, retry_failed: bool = False) -> dict[str, object]:
        _ = (limit, retry_failed)
        return {"refreshed": 0, "vector_available": False}


@dataclass
class SQLiteFTSL3Plugin(MockL3Plugin):
    provider_id: str = "sqlite_fts_l3"
