from __future__ import annotations

from pal.execution.generated_tool_models import (
    PluginsL3StubsL3ProviderCapabilityMixinDeleteInput,
    PluginsL3StubsL3ProviderCapabilityMixinRecallInput,
    PluginsL3StubsL3ProviderCapabilityMixinRefreshIndexesInput,
    PluginsL3StubsL3ProviderCapabilityMixinUpdateInput,
    PluginsL3StubsL3ProviderCapabilityMixinWriteInput,
)

from dataclasses import dataclass, field

from pal.memory.contracts import (
    L2Entry,
    L3CommitRequest,
    L3CorrectRequest,
    L3DeleteRequest,
    L3MutationResult,
    L3RecallResult,
    L3RetireResult,
    MemoryQuery,
)
from pal.memory.candidates import memory_star_from_args, star_text_fields
from pal.memory.rendering import (
    build_mutation_structured_payload,
    build_recall_structured_payload,
    normalize_recall_view,
    render_mutation_result_for_llm,
    render_recall_result_for_llm,
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


def _read_mem_ref(args: dict[str, object]) -> str:
    return str(args.get("mem_ref") or args.get("document_id") or "").strip()


def _read_task_id(args: dict[str, object]) -> str | None:
    task_id = str(args.get("task_id") or "").strip()
    return task_id or None


def _recall_scope_for_task_id(task_id: str | None) -> str | None:
    return "task" if task_id else None


MEMORY_STAR_SCHEMA = {
    "type": "object",
    "description": "Required when kind='case'; omit for fact memories.",
    "properties": {
        "situation": {"type": "string", "description": "Situation or failure context."},
        "task": {"type": "string", "description": "Task or objective in that situation."},
        "action": {"type": "string", "description": "Action, repair, or decision that mattered."},
        "result": {"type": "string", "description": "Outcome or reusable lesson."},
    },
    "required": ["situation", "task", "action", "result"],
}


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
        description="Show memory provider runtime state",
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = self.inspect()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="memory provider",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Memory provider", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="provider",
        action_name="inventory",
        description="Inspect memory provider inventory and index status",
    )
    def inventory(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = self.inspect()
        snapshot.setdefault("provider_id", self.provider_id)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="memory provider inventory",
            structured=snapshot,
            llm_text=render_titled_structured_for_llm("Memory provider inventory", snapshot),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="provider",
        family="recall",
        action_name="recall",
        description=(
            "Recall durable memory records. When an error, regression, failed repair, repeated pitfall, or unfamiliar "
            "debugging situation appears, prefer kind='case' with concrete error/symptom/fix terms to check prior failures "
            "and fixes before improvising."
        ),
        metadata={"omit_family_in_canonical": True},
        InputModel=PluginsL3StubsL3ProviderCapabilityMixinRecallInput,
    )
    def recall_query(self, call: IntrospectionCall) -> IntrospectionResult:
        task_id = _read_task_id(call.args)
        query = MemoryQuery(
            level=str(call.args.get("level") or "warm"),
            queries=[str(value) for value in list(call.args.get("queries") or [])],
            topic_scope=[str(value) for value in list(call.args.get("topic_scope") or [])],
            task_id=task_id,
            limit=int(call.args.get("limit") or 8),
            scope=_recall_scope_for_task_id(task_id),
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
        description="Commit durable memory",
        metadata={"omit_family_in_canonical": True},
        InputModel=PluginsL3StubsL3ProviderCapabilityMixinWriteInput,
    )
    def commit_write(self, call: IntrospectionCall) -> IntrospectionResult:
        kind = str(call.args.get("kind") or "").strip()
        summary = str(call.args.get("summary") or "").strip()
        search_text = str(call.args.get("search_text") or summary).strip()
        if kind not in {"fact", "case"}:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="kind must be fact or case", llm_text="kind must be fact or case")
        if not summary:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="summary is required", llm_text="summary is required")
        star, star_error = memory_star_from_args(call.args)
        if star_error:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text=star_error, llm_text=star_error)
        if kind == "case" and not star:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="kind=case requires star with situation, task, action, and result",
                llm_text="kind=case requires star with situation, task, action, and result",
            )
        if kind != "case" and star:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="star is only valid when kind=case", llm_text="star is only valid when kind=case")
        task_id = _read_task_id(call.args)
        payload = dict(call.args.get("payload") or {})
        if star:
            payload.update(star)
        star_fields = star_text_fields(star) if star else {}
        result = self.commit(
            L3CommitRequest(
                kind=kind,
                scope="task" if task_id else str(call.args.get("scope") or "system"),
                task_id=task_id,
                title=str(call.args.get("title")) if call.args.get("title") is not None else None,
                summary=summary,
                search_text=search_text,
                canonical_key=str(call.args.get("canonical_key")) if call.args.get("canonical_key") is not None else None,
                payload=payload,
                topics=[str(value) for value in list(call.args.get("topics") or [])],
                situation_text=star_fields.get("situation_text", ""),
                task_text=star_fields.get("task_text", ""),
                action_text=star_fields.get("action_text", ""),
                result_text=star_fields.get("result_text", ""),
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
        description="Update durable memory",
        metadata={"omit_family_in_canonical": True},
        InputModel=PluginsL3StubsL3ProviderCapabilityMixinUpdateInput,
    )
    def correct_patch(self, call: IntrospectionCall) -> IntrospectionResult:
        mem_ref = _read_mem_ref(call.args)
        if not mem_ref:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="mem_ref is required", llm_text="mem_ref is required")
        star, star_error = memory_star_from_args(call.args)
        if star_error:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text=star_error, llm_text=star_error)
        if star and mem_ref.startswith("fact:"):
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="star is only valid for case memories", llm_text="star is only valid for case memories")
        payload_patch = dict(call.args.get("payload_patch") or {})
        if star:
            payload_patch.update(star)
        star_fields = star_text_fields(star) if star else {}
        result = self.correct(
            L3CorrectRequest(
                document_id=mem_ref,
                title=str(call.args.get("title")) if call.args.get("title") is not None else None,
                summary=str(call.args.get("summary")) if call.args.get("summary") is not None else None,
                search_text=str(call.args.get("search_text")) if call.args.get("search_text") is not None else None,
                payload_patch=payload_patch,
                topics=[str(value) for value in list(call.args.get("topics") or [])] if call.args.get("topics") is not None else None,
                situation_text=star_fields.get("situation_text") if star else None,
                task_text=star_fields.get("task_text") if star else None,
                action_text=star_fields.get("action_text") if star else None,
                result_text=star_fields.get("result_text") if star else None,
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
        InputModel=PluginsL3StubsL3ProviderCapabilityMixinDeleteInput,
    )
    def delete_memory(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.delete(
            L3DeleteRequest(
                document_id=_read_mem_ref(call.args),
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
        InputModel=PluginsL3StubsL3ProviderCapabilityMixinRefreshIndexesInput,
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

    def delete(self, request: L3DeleteRequest) -> L3MutationResult:
        return L3MutationResult(status=RuntimeStatus.NOT_FOUND, document_id=request.document_id)

    def retire_entries(self, entries: list[L2Entry]) -> L3RetireResult:
        _ = entries
        return L3RetireResult(status=RuntimeStatus.SKIPPED)

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
                search_text=str(record.get("search_text", "")),
                canonical_key=str(record.get("canonical_key")) if record.get("canonical_key") is not None else None,
                dedupe_fingerprint=str(record.get("dedupe_fingerprint")) if record.get("dedupe_fingerprint") is not None else None,
                payload=dict(record),
            )
            for record in self.records
        ]
        service = getattr(self, "service", None)
        if service is not None and hasattr(service, "project_l3_entries"):
            service.project_l3_entries(entries, touch=True)
        return L3RecallResult(hits=list(self.records), projected_entries=entries)

    def commit(self, request: L3CommitRequest) -> L3MutationResult:
        existing = None
        if request.kind == "fact" and request.canonical_key:
            existing = next(
                (
                    record
                    for record in self.records
                    if str(record.get("document_kind")) == "fact"
                    and str(record.get("canonical_key") or "").strip() == str(request.canonical_key or "").strip()
                ),
                None,
            )
        if existing is None and request.dedupe_fingerprint:
            existing = next(
                (
                    record
                    for record in self.records
                    if str(record.get("document_kind")) == request.kind
                    and str(record.get("dedupe_fingerprint") or "").strip() == str(request.dedupe_fingerprint or "").strip()
                ),
                None,
            )
        document_id = str(existing.get("document_id")) if existing is not None else f"{request.kind}:{len(self.records) + 1}"
        hit = {
            "document_id": document_id,
            "document_kind": request.kind,
            "scope": request.scope,
            "title": request.title or "",
            "summary": request.summary,
            "search_text": request.summary or request.title or "",
            "canonical_key": request.canonical_key,
            "dedupe_fingerprint": request.dedupe_fingerprint,
            "topics": list(request.topics),
            "payload": dict(request.payload),
        }
        if existing is None:
            self.records.append(hit)
        else:
            existing.update(hit)
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
            search_text=str(hit.get("search_text") or ""),
            canonical_key=request.canonical_key,
            dedupe_fingerprint=request.dedupe_fingerprint,
            payload=dict(hit),
        )
        result = L3MutationResult(status=RuntimeStatus.OK, document_id=document_id, hit=hit, projected_entry=entry)
        service = getattr(self, "service", None)
        if service is not None and hasattr(service, "project_mutation"):
            service.project_mutation(result)
        return result

    def retire_entries(self, entries: list[L2Entry]) -> L3RetireResult:
        document_ids: list[str] = []
        reused_document_ids: list[str] = []
        for entry in entries:
            if entry.kind not in {"fact", "case"}:
                continue
            existing = None
            if entry.kind == "fact" and entry.canonical_key:
                existing = next(
                    (
                        record
                        for record in self.records
                        if str(record.get("document_kind")) == "fact"
                        and str(record.get("canonical_key") or "").strip() == str(entry.canonical_key or "").strip()
                    ),
                    None,
                )
            if existing is None and entry.dedupe_fingerprint:
                existing = next(
                    (
                        record
                        for record in self.records
                        if str(record.get("document_kind")) == entry.kind
                        and str(record.get("dedupe_fingerprint") or "").strip() == str(entry.dedupe_fingerprint or "").strip()
                    ),
                    None,
                )
            if existing is not None:
                document_id = str(existing.get("document_id") or "")
                reused_document_ids.append(document_id)
                document_ids.append(document_id)
                continue
            document_id = f"{entry.kind}:{len(self.records) + 1}"
            self.records.append(
                {
                    "document_id": document_id,
                    "document_kind": entry.kind,
                    "scope": entry.scope,
                    "task_id": entry.task_id,
                    "title": entry.title,
                    "summary": entry.summary,
                    "rendered": entry.rendered,
                    "search_text": entry.search_text,
                    "canonical_key": entry.canonical_key,
                    "dedupe_fingerprint": entry.dedupe_fingerprint,
                    "payload": dict(entry.payload),
                }
            )
            document_ids.append(document_id)
        return L3RetireResult(
            status=RuntimeStatus.OK,
            document_ids=document_ids,
            reused_document_ids=reused_document_ids,
            metadata={"retired": len(document_ids), "reused": len(reused_document_ids)},
        )

    def correct(self, request: L3CorrectRequest) -> L3MutationResult:
        for record in self.records:
            if str(record.get("document_id")) != request.document_id:
                continue
            if request.title is not None:
                record["title"] = request.title
            if request.summary is not None:
                record["summary"] = request.summary
            if request.search_text is not None:
                record["search_text"] = request.search_text
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
                search_text=str(record.get("search_text", "")),
                canonical_key=str(record.get("canonical_key")) if record.get("canonical_key") is not None else None,
                dedupe_fingerprint=str(record.get("dedupe_fingerprint")) if record.get("dedupe_fingerprint") is not None else None,
                payload=dict(record),
            )
            result = L3MutationResult(status=RuntimeStatus.OK, document_id=request.document_id, hit=dict(record), projected_entry=entry)
            service = getattr(self, "service", None)
            if service is not None and hasattr(service, "project_mutation"):
                service.project_mutation(result)
            return result
        return L3MutationResult(status=RuntimeStatus.NOT_FOUND, document_id=request.document_id)

    def delete(self, request: L3DeleteRequest) -> L3MutationResult:
        mem_ref = str(request.document_id or "").strip()
        for index, record in enumerate(list(self.records)):
            if str(record.get("document_id") or "").strip() != mem_ref:
                continue
            deleted = dict(record)
            del self.records[index]
            service = getattr(self, "service", None)
            remove_projected_entries = getattr(service, "remove_projected_entries", None)
            if callable(remove_projected_entries):
                remove_projected_entries([mem_ref])
            return L3MutationResult(
                status=RuntimeStatus.OK,
                document_id=mem_ref,
                hit={"mem_ref": mem_ref, "deleted": True, "deleted_memory": deleted, "reason": request.reason},
                metadata={"deleted": True},
            )
        return L3MutationResult(status=RuntimeStatus.NOT_FOUND, document_id=mem_ref)

    def refresh_indexes(self, *, limit: int = 8, retry_failed: bool = False) -> dict[str, object]:
        _ = (limit, retry_failed)
        return {"refreshed": 0, "vector_available": False}


@dataclass
class SQLiteFTSL3Plugin(MockL3Plugin):
    provider_id: str = "sqlite_fts_l3"
