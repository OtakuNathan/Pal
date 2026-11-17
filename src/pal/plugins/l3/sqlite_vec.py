from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_CONTROL,
    INDIRECT_LOCAL_WRITE,
)
from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import ToolGuidance, ToolAffordance, EffectReceipt, EffectOutcome

from pal.execution.generated_tool_models import (
    PluginsL3SqliteVecSQLiteVecL3PluginRefreshIndexesInput,
)

import math
import json
import threading
from functools import wraps
from dataclasses import InitVar, dataclass, field, replace
from typing import Any
from pal.memory.backend_contracts import SqliteGenerationPort
from pal.memory.storage import MemoryStorage

from pal.foundation import utc_now
from pal.foundation.diagnostics import exception_reason
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
from pal.memory.mutations import mutation_id_from_call
from pal.memory.candidates import memory_star_from_args, star_text_fields
from pal.memory.contracts import RECALL_PROMOTION_THRESHOLD, L3BatchCommitRequest, L3BatchCommitResult
from pal.memory.embedding import EmbeddingProviderPort, OllamaEmbeddingProvider
from pal.memory.embedding_config import embedding_configuration
from pal.memory.embedding_space import (
    EmbeddingAccess, EmbeddingSpace, accepts_vector, claim_empty_space,
    embedding_access, read_space, reset_empty_space,
)
from pal.memory.repository import (
    MemoryDurableRepository,
    cosine_similarity,
    deserialize_vector,
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
    SINGLETON_TARGET,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm


def _provider_recovery(handler):
    """Keep uncertain mutation reconciliation on the originally selected store."""
    @wraps(handler)
    def invoke(self, call):
        target = call.meta.get("resolved_target_id")
        provider_name = target if target and target != SINGLETON_TARGET else self.provider_id
        args = {"provider_id": provider_name}
        if call.args.get("mem_ref"):
            args["mem_ref"] = call.args["mem_ref"]
        else:
            args["queries"] = [str(call.args.get("search_text") or call.args.get("summary") or "")]
            if call.args.get("kind"):
                args["kind"] = call.args["kind"]
            if call.args.get("task_id"):
                args["task_id"] = call.args["task_id"]
        try:
            result = handler(self, call)
        except Exception as exc:
            reason = exception_reason(exc)
            result = CapabilityResult(
                status=RuntimeStatus.ERROR, text=reason,
                llm_text=reason,
                structured={"error": reason, "provider_id": provider_name},
                effect_receipt=EffectReceipt(outcome=EffectOutcome.UNKNOWN),
            )
        if isinstance(result, IntrospectionResult) and result.status == RuntimeStatus.INVALID:
            return CapabilityResult(status=result.status, text=result.text, llm_text=result.llm_text,
                structured=result.structured, effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_STARTED),
                recovery_hint="Correct the reported input fields before retrying; the mutation did not start.")
        if result.status != RuntimeStatus.OK and (result.structured or {}).get("mem_ref"):
            args.pop("queries", None)
            args["mem_ref"] = result.structured["mem_ref"]
        hint = (f"Reconcile with recall_memory using {args!r} before retrying. "
                "Keep this provider and record identity even if the active provider changes; do not blindly repeat the write.")
        if result.status != RuntimeStatus.OK:
            return CapabilityResult(status=result.status, text=result.text, llm_text=result.llm_text,
                structured=result.structured, effect_receipt=result.effect_receipt if isinstance(result, CapabilityResult) else None,
                recovery_hint=hint, affordances=(ToolAffordance(
                tool="recall_memory", arguments=args,
                reason="Inspect the original mutation target before retrying.",
            ),))
        return result
    return invoke


def _generation_operation(handler):
    @wraps(handler)
    def invoke(self, *args, **kwargs):
        with self._generation_lock:
            self._sync_embedding_access()
            return handler(self, *args, **kwargs)
    return invoke


def _provider_mutation_result(operation: str, result: L3MutationResult) -> CapabilityResult:
    try:
        return CapabilityResult(
            status=result.status, text=f"memory {operation} result",
            structured=build_mutation_structured_payload(result),
            llm_text=render_mutation_result_for_llm(operation, result),
        )
    except Exception as exc:
        # Backend completion is known even when presentation subsequently fails.
        reason = exception_reason(exc)
        return CapabilityResult(status=RuntimeStatus.ERROR, text=reason,
            llm_text=reason, structured={"error": reason,
                "mem_ref": result.document_id, "backend_status": result.status},
            effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED
                if result.status == RuntimeStatus.OK else EffectOutcome.UNKNOWN))


def _stable_document_search_text(*parts: str) -> str:
    return "\n".join(part.strip() for part in parts if str(part or "").strip())


def _read_mem_ref(args: dict[str, Any]) -> str:
    return str(args.get("mem_ref") or args.get("document_id") or "").strip()


def _read_task_id(args: dict[str, Any]) -> str | None:
    task_id = str(args.get("task_id") or "").strip()
    return task_id or None


def _recall_scope_for_task_id(task_id: str | None) -> str | None:
    return "task" if task_id else None


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
    read_only: bool = False
    use_default_embedding: bool = field(default=True, kw_only=True)
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
    _generation_lock: Any = field(default_factory=threading.RLock, init=False, repr=False)
    _indexing: bool = field(default=False, init=False, repr=False)
    _vector_reads: bool = field(default=True, init=False, repr=False)
    _binding_read_only: bool = field(default=False, init=False, repr=False)
    _bound_space: EmbeddingSpace | None = field(default=None, init=False, repr=False)
    _catalog_space_matches: bool = field(default=True, init=False, repr=False)
    _retired_repositories: list[MemoryDurableRepository] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self, embedder: EmbeddingProviderPort | None) -> None:
        if self.embedding_provider is None and embedder is not None:
            self.embedding_provider = embedder
        if self.embedding_provider is None and self.use_default_embedding:
            self.embedding_provider = OllamaEmbeddingProvider()
        storage = self.repository.catalog
        self._binding_read_only = self.embedding_provider is None or self.read_only or bool(storage is not None
            and storage.is_published(self.repository.generation_id)
            and storage.provider_for_generation(self.repository.generation_id) != self.provider_id)
        self.repository.read_only = self._binding_read_only
        if not self.repository.read_only:
            self.repository.ensure_schema()
        if storage is not None:
            self._indexing = storage.indexing_pending(self.repository.generation_id)
        self._load_embedding_space(claim=not self._binding_read_only)

    def _load_embedding_space(self, *, claim: bool = False) -> None:
        repo = self.repository
        if claim and repo.writable and self.embedding_provider is not None:
            with repo.write_transaction():
                previous_space = read_space(repo.database.connection())
                self._bound_space = claim_empty_space(repo.database.connection(), self.migration_identity)
                if self._bound_space is not None and self._bound_space.profile == self.migration_identity:
                    configuration = embedding_configuration(self.embedding_provider)
                    if configuration is not None:
                        repo.database.execute_sql("INSERT OR REPLACE INTO memory_embedding_configuration VALUES (1,?)",
                            (json.dumps(configuration, sort_keys=True),))
                if previous_space is None and self._bound_space is not None:
                    for row in repo.list_projection_rows():
                        repo.queue_embedding(document_id=row["document_id"], source_text=str(row.get("search_text") or ""),
                            space_profile=self.migration_identity, **self.profile)
        else:
            self._bound_space = read_space(repo.database.connection())
        self._catalog_space_matches = True
        storage = repo.catalog
        if storage is not None and storage.is_published(repo.generation_id):
            profile = storage.generation_profile(repo.generation_id)
            if (claim and self._bound_space is not None and self._bound_space.profile == self.migration_identity
                    and not profile):
                profile = storage.claim_generation_profile(repo.generation_id, self.provider_id, self.migration_identity)
            self._catalog_space_matches = bool(self._bound_space is not None
                and storage.provider_for_generation(repo.generation_id) == self.provider_id
                and profile == self._bound_space.profile)
        self._sync_embedding_access()

    def _sync_embedding_access(self) -> None:
        access = embedding_access(self.embedding_provider is not None
            and self._bound_space is not None and self._catalog_space_matches,
            self._bound_space is not None and self._bound_space.profile == self.migration_identity,
            not self._binding_read_only)
        self.read_only = access is not EmbeddingAccess.WRITE
        self._vector_reads = access is not EmbeddingAccess.BLOCKED

    @property
    def indexes_writable(self) -> bool:
        return self._vector_reads and not self.read_only and self.repository.writable

    @property
    def module_id(self) -> str:
        return f"l3.{self.provider_id}"

    @property
    def generation_port(self) -> SqliteGenerationPort | None:
        return self if self.repository.catalog is not None else None

    @property
    def storage(self) -> MemoryStorage:
        if self.repository.catalog is None:
            raise RuntimeError("Backend has no managed generation catalog")
        return self.repository.catalog

    @property
    def profile(self) -> dict[str, str | None]:
        return {"provider_id": self._embedding_provider_id(), "model_name": self._embedding_model_name(),
                **self._embedding_profile()}

    @property
    def migration_identity(self) -> dict[str, str | None]:
        return {**self.profile, "configuration_fingerprint":
                self.embedding_provider.configuration_fingerprint if self.embedding_provider is not None else "unavailable"}

    def prepare_generation(self, repository: MemoryDurableRepository, documents=None) -> None:
        if repository.catalog is not None and repository.catalog.is_published(repository.generation_id):
            raise RuntimeError("Embedding preparation requires an unpublished generation")
        with repository.write_transaction():
            if documents is not None:
                space = read_space(repository.database.connection())
                if space is None or space.profile != self.migration_identity:
                    raise RuntimeError("Candidate embedding identity changed during migration")
            if documents is None:
                repository.Vector.delete().execute()
                repository.Embedding.delete().execute()
                # Vector indexes belong to one embedding space and generation.
                indexes = repository.database.execute_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name GLOB 'memory_vec_idx_*' AND sql LIKE '%USING vec0%'").fetchall()
                if indexes:
                    status = ensure_sqlite_vec_loaded(repository.database)
                    if not status.available:
                        raise RuntimeError(f"Cannot discard source vector indexes: {status.detail}")
                # Dropping the virtual table also removes its shadow tables.
                for (name,) in indexes:
                    if name.replace("_", "").isalnum():
                        repository.database.execute_sql(f'DROP TABLE "{name}"')
                reset_empty_space(repository.database.connection(), self.migration_identity)
                repository.database.execute_sql("DELETE FROM memory_embedding_configuration")
                configuration = embedding_configuration(self.embedding_provider)
                if configuration is not None:
                    repository.database.execute_sql("INSERT INTO memory_embedding_configuration VALUES (1,?)",
                        (json.dumps(configuration, sort_keys=True),))
                repository.rebuild_fts_indexes()
                documents = [row["document_id"] for row in repository.list_projection_rows()]
            for document_id in documents:
                repository.sync_fts_row(document_id)
                document = repository.get_document(document_id)
                if document is not None:
                    repository.queue_embedding(document_id=document_id,
                        source_text=str(document.get("search_text") or ""), space_profile=self.migration_identity, **self.profile)
                else:
                    ids = [row.embedding_id for row in repository.Embedding.select().where(
                        repository.Embedding.document_id == document_id)]
                    repository.Vector.delete().where(repository.Vector.embedding_id.in_(ids)).execute()
                    repository.Embedding.delete().where(repository.Embedding.document_id == document_id).execute()
        if repository is self.repository:
            self._load_embedding_space()

    def bind_generation(self, generation_id: str) -> None:
        with self._generation_lock:
            storage = self.repository.catalog
            if storage is None or storage.current() != generation_id or storage.active_provider_id() != self.provider_id:
                raise RuntimeError("Backend cannot bind an uncommitted memory generation")
            if storage.generation_profile(generation_id) != self.migration_identity:
                raise RuntimeError("Backend embedding identity differs from the committed generation")
            if not self.generation_is_bound(generation_id):
                self._replace_repository(storage.open(generation_id), read_only=False)
            self._close_retired_repositories()

    def generation_is_bound(self, generation_id: str) -> bool:
        with self._generation_lock:
            return (self.repository.generation_id == generation_id and not self.read_only
                    and not self._binding_read_only and self._catalog_space_matches
                    and self._bound_space is not None
                    and self._bound_space.profile == self.migration_identity)

    def _replace_repository(self, repository: MemoryDurableRepository, *, read_only: bool) -> None:
        # Prepare every binding-dependent field privately. A load/validation
        # failure cannot publish a partially initialized repository to callers.
        try:
            prepared = replace(self, repository=repository, read_only=read_only)
            if not read_only and prepared.read_only:
                raise RuntimeError("Published database embedding identity differs from its catalog")
        except BaseException:
            self._retired_repositories.append(repository)
            self._close_retired_repositories()
            raise
        self._retired_repositories.append(self.repository)
        self.repository = prepared.repository
        self.read_only = prepared.read_only
        self._binding_read_only = prepared._binding_read_only
        self._indexing = prepared._indexing
        self._vector_reads = prepared._vector_reads
        self._bound_space = prepared._bound_space
        self._catalog_space_matches = prepared._catalog_space_matches

    def _close_retired_repositories(self) -> None:
        failures = []
        for repository in tuple(self._retired_repositories):
            try:
                repository.close()
            except Exception as exc:
                failures.append(exc)
            else:
                self._retired_repositories.remove(repository)
        if failures:
            raise ExceptionGroup("Retired memory repositories could not be closed", failures)

    def close(self) -> None:
        with self._generation_lock:
            self.mounted = False
            if self.repository not in self._retired_repositories:
                self._retired_repositories.append(self.repository)
            self._close_retired_repositories()

    def retire_generation(self, generation_id: str) -> None:
        with self._generation_lock:
            if self.repository.generation_id == generation_id:
                self._binding_read_only = True
                self.read_only = True
                self.repository.read_only = True

    def index_generation(self, generation_id: str, *, limit: int) -> dict[str, object]:
        with self._generation_lock:
            if self.repository.generation_id != generation_id or self.repository.catalog.current() != generation_id:
                raise RuntimeError("Memory indexing generation was superseded")
            result = self.refresh_indexes(limit=limit, retry_failed=True)
            return {**result, "pending": self.repository.inventory()["retryable_embeddings"]}

    def finish_indexing(self, generation_id: str) -> None:
        with self._generation_lock:
            if self.repository.generation_id == generation_id:
                self._indexing = False

    def iter_providers(self) -> list["SQLiteVecL3Plugin"]:
        return [self]

    def resolve_provider_id(self, provider: "SQLiteVecL3Plugin") -> str:
        return provider.provider_id

    def resolve_provider_label(self, provider: "SQLiteVecL3Plugin") -> str:
        return provider.provider_id

    @_generation_operation
    def inspect_summary(self) -> dict[str, Any]:
        """Passive hub facts: never probe embedding transport for a summary."""
        return {**self.repository.inventory(), "mounted": self.mounted,
                "read_only": self.read_only, "embedding_space_matches": self._vector_reads,
                "embedding_configured": self.embedding_provider is not None,
                "last_embedding_error": self.last_embedding_error}

    @_generation_operation
    def inspect(self) -> dict[str, Any]:
        sqlite_vec_status = ensure_sqlite_vec_loaded(self.repository.database)
        inventory = self.repository.inventory()
        provider_health = (self._embedding_health() if self._vector_reads else
                           {"healthy": False, "detail": "Embedding identity differs or is unknown; migrate this generation"})
        inventory.update(
            {
                "provider_id": self.provider_id,
                "mounted": self.mounted,
                "read_only": self.read_only,
                "embedding_space_matches": self._vector_reads,
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

    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = self.inspect()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="sqlite vec memory provider",
            structured=payload,
            llm_text=render_titled_structured_for_llm("SQLite vec memory provider", payload),
        )

    def inventory(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        from pal.shared.inspection import inspection_snapshot
        payload = inspection_snapshot({"provider_id": self.provider_id, "mounted": self.mounted},
                                      sections={"inventory": self.inspect}, source="memory_provider")
        if not payload["diagnostics"]["partial"]:
            payload.update(payload.pop("inventory"))
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="sqlite vec memory inventory",
            structured=payload,
            llm_text=render_titled_structured_for_llm("SQLite vec memory inventory", payload),
        )

    # Internal backend adapter; model calls use the memory facade.
    def recall_query(self, call: IntrospectionCall) -> IntrospectionResult:
        task_id = _read_task_id(call.args)
        query = MemoryQuery(
            mem_ref=str(call.args.get("mem_ref") or "").strip() or None,
            level=str(call.args.get("level") or "warm"),
            queries=[str(value) for value in list(call.args.get("queries") or [])],
            topic_scope=[str(value) for value in list(call.args.get("topic_scope") or [])],
            task_id=task_id,
            limit=int(call.args.get("limit") or 8),
            kind=str(call.args.get("kind")) if call.args.get("kind") is not None else None,
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

    # Backend test/maintenance adapter; normal model writes use remember_memory.
    @_provider_recovery
    def commit_write(self, call: IntrospectionCall) -> IntrospectionResult | CapabilityResult:
        kind = str(call.args.get("kind") or "").strip()
        title = str(call.args.get("title") or call.args.get("summary") or "").strip()
        summary = str(call.args.get("summary") or "").strip()
        search_text = str(call.args.get("search_text") or "").strip()
        if kind not in {"fact", "case"}:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="kind must be fact or case", llm_text="kind must be fact or case")
        if not summary:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="summary is required", llm_text="summary is required")
        if not search_text:
            return IntrospectionResult(status=RuntimeStatus.INVALID, text="search_text is required", llm_text="search_text is required")
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
                mutation_id=mutation_id_from_call(call),
                kind=kind,
                title=title,
                summary=summary,
                search_text=search_text,
                scope="task" if task_id else str(call.args.get("scope") or "system"),
                task_id=task_id,
                canonical_key=str(call.args.get("canonical_key")) if call.args.get("canonical_key") is not None else None,
                payload=payload,
                topics=[str(value) for value in list(call.args.get("topics") or [])],
                situation_text=star_fields.get("situation_text", ""),
                task_text=star_fields.get("task_text", ""),
                action_text=star_fields.get("action_text", ""),
                result_text=star_fields.get("result_text", ""),
            )
        )
        return _provider_mutation_result("commit", result)

    # Internal backend adapter; model calls use the memory facade.
    @_provider_recovery
    def correct_patch(self, call: IntrospectionCall) -> IntrospectionResult | CapabilityResult:
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
                mutation_id=mutation_id_from_call(call),
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
        return _provider_mutation_result("update", result)

    # Internal backend adapter; model calls use the memory facade.
    @_provider_recovery
    def delete_memory(self, call: IntrospectionCall) -> IntrospectionResult | CapabilityResult:
        mem_ref = _read_mem_ref(call.args)
        result = self.delete(
            L3DeleteRequest(
                document_id=mem_ref,
                reason=str(call.args.get("reason") or ""),
            )
        )
        return _provider_mutation_result("delete", result)

    @_generation_operation
    def attach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        if not self.mounted and self.repository.catalog is not None:
            self._replace_repository(self.repository.catalog.open(
                self.repository.generation_id if self._binding_read_only else None,
                read_only=self._binding_read_only), read_only=self._binding_read_only)
        self._close_retired_repositories()
        self.mounted = True
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="memory provider attached",
            structured={"mounted": True},
            llm_text=render_titled_structured_for_llm("Memory provider attached", {"mounted": True}),
        )

    @_generation_operation
    def detach(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        if self.repository.frozen:
            return IntrospectionResult(status="unavailable", text="memory maintenance owns the connection", llm_text="memory maintenance owns the connection")
        self.close()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="memory provider detached",
            structured={"mounted": False},
            llm_text=render_titled_structured_for_llm("Memory provider detached", {"mounted": False}),
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

    @_generation_operation
    def commit_batch(self, request: L3BatchCommitRequest) -> L3BatchCommitResult:
        from pal.memory.mutations import commit_batch
        return commit_batch(self, request)

    @_generation_operation
    def commit(self, request: L3CommitRequest) -> L3MutationResult:
        from pal.memory.mutations import commit
        return commit(self, request)

    @_generation_operation
    def retire_entries(self, entries: list[L2Entry]) -> L3RetireResult:
        document_ids, reused = [], []
        for entry in entries:
            payload = dict(entry.payload or {})
            result = self.commit(L3CommitRequest(
                kind=entry.kind, scope=entry.scope, task_id=entry.task_id,
                title=entry.title, summary=entry.summary, search_text=entry.search_text,
                canonical_key=entry.canonical_key, payload=payload, topics=_extract_entry_topics(entry),
                mutation_id=f"retire:{entry.entry_id}",
                situation_text=str(payload.get("situation") or payload.get("situation_text") or ""),
                task_text=str(payload.get("task") or payload.get("task_text") or ""),
                action_text=str(payload.get("action") or payload.get("action_text") or ""),
                result_text=str(payload.get("result") or payload.get("result_text") or ""),
            ))
            if result.status != "ok":
                return L3RetireResult(status=result.status, document_ids=document_ids)
            document_ids.append(result.document_id)
            if result.metadata.get("replayed") or result.metadata.get("deduplicated"):
                reused.append(result.document_id)
        return L3RetireResult(status="ok", document_ids=document_ids, reused_document_ids=reused,
                              metadata={"retired": len(document_ids), "reused": len(reused)})

    @_generation_operation
    def correct(self, request: L3CorrectRequest) -> L3MutationResult:
        from pal.memory.mutations import correct
        return correct(self, request)

    @_generation_operation
    def delete(self, request: L3DeleteRequest) -> L3MutationResult:
        if not self.mounted or self.read_only or not self.repository.writable:
            return L3MutationResult(status="unavailable", document_id=request.document_id)
        document_id = str(request.document_id or "").strip()
        if not document_id:
            return L3MutationResult(status="invalid", document_id="")
        with self.repository.write_lock:
            if self.repository.frozen:
                return L3MutationResult(status="unavailable", document_id=document_id)
            if self.repository.catalog is not None:
                refs = self.repository.catalog.forget(self.repository, document_id)
                self.repository.catalog.purge_forgotten(self.repository, refs)
            else:
                with self.repository.write_transaction():
                    deleted = self.repository.delete_document(document_id)
                if deleted is None:
                    return L3MutationResult(status="not_found", document_id=document_id)
                refs = {document_id}
            self.service.remove_projected_entries(list(refs))
        return L3MutationResult(status="ok", document_id=document_id,
            hit={"document_id": document_id, "deleted": True}, metadata={"deleted": True})

    def _recall_exact(self, query: MemoryQuery) -> L3RecallResult:
        ref = query.mem_ref
        metadata = {"retrieval_mode": "exact", "mem_ref": ref, "lookup_status": "not_found"}
        if not self.mounted:
            return L3RecallResult(metadata={**metadata, "lookup_status": "unavailable"})
        repo = self.repository
        if ref in repo.excluded_refs:
            return L3RecallResult(metadata=metadata)
        if repo.catalog is not None and repo.catalog.is_deleted(ref):
            return L3RecallResult(metadata={**metadata, "lookup_status": "deleted"})
        hit = repo.get_document(ref)
        if hit is not None:
            if ((query.kind and hit.get("document_kind") != query.kind)
                    or (query.scope and hit.get("scope") != query.scope)
                    or (query.task_id and hit.get("task_id") not in (None, query.task_id))):
                return L3RecallResult(metadata={**metadata, "lookup_status": "not_in_scope"})
            entry = self._project_entry(hit, source_kind="l3_recall", candidate_state="candidate")
            self.service.project_l3_entries([entry], touch=True, top_of_mind=False)
            return L3RecallResult(hits=[hit], projected_entries=[entry],
                metadata={**metadata, "lookup_status": "found"})
        # Revisions are durable evidence of replacement, not current facts.
        import json
        row = repo.database.execute_sql(
            "SELECT successors_json FROM memory_revisions WHERE document_id = ?", (ref,)
        ).fetchone()
        successors = json.loads(row[0]) if row else []
        retired = row is not None
        if not retired and repo.catalog is not None:
            for item in repo.catalog.history(ref, repo=repo):
                if item.get("document", {}).get("document_id") == ref:
                    successors = item.get("successors", [])
                    retired = True
                    break
        if retired:
            metadata.update(lookup_status="superseded" if successors else "retired", successors=successors)
        return L3RecallResult(metadata=metadata)

    @_generation_operation
    def recall(self, query: MemoryQuery) -> L3RecallResult:
        if query.mem_ref:
            return self._recall_exact(query)
        if not self.mounted:
            return L3RecallResult(metadata={"degraded": True, "degraded_reason": "memory provider is not mounted"})
        refreshed = (
            {
                "refreshed": 0,
                "vector_available": self._vector_reads and not self._indexing,
                "read_only": True,
            }
            if not self.indexes_writable or self._indexing
            else self.refresh_indexes(limit=min(max(query.limit, 1), 8))
        )
        candidate_limit = max(query.limit * 4, 16)
        search_text = _stable_document_search_text(*query.queries)
        lexical_candidates, lexical_source_counts = self.repository.collect_lexical_candidates(
            search_text,
            limit=candidate_limit,
        )
        topic_candidates = self.repository.list_topic_candidates(query.topic_scope, limit=candidate_limit)
        vector_query_text = _stable_document_search_text(search_text, " ".join(query.topic_scope) if query.topic_scope else "")
        embedding_health = ({"healthy": False, "detail": "generation index is rebuilding or its embedding identity differs"}
                            if self._indexing or not self._vector_reads
                            else self._embedding_health() if vector_query_text else {"healthy": True})
        vector_candidates = {} if self._indexing or not self._vector_reads else self._vector_candidates(vector_query_text, limit=candidate_limit)
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
        if not self.read_only:
            self.repository.bump_usage([hit["document_id"] for hit in hits])
        hot_entries = [entry for entry, (_, doc_id, hit) in zip(projected_entries, limited) if hit.get("scores", {}).get("final", 0) >= RECALL_PROMOTION_THRESHOLD]
        cool_entries = [entry for entry in projected_entries if entry not in hot_entries]
        if hot_entries:
            self.service.project_l3_entries(hot_entries, touch=True, top_of_mind=True)
        if cool_entries:
            self.service.project_l3_entries(cool_entries, touch=True, top_of_mind=False)
        degraded_reason = str(self.last_embedding_error or embedding_health.get("last_error")
                              or refreshed.get("last_embedding_error") or "").strip()
        degraded = bool(vector_query_text) and bool(degraded_reason)
        return L3RecallResult(
            hits=hits,
            projected_entries=projected_entries,
            metadata={
                "index_refresh": refreshed,
                "embedding_health": embedding_health,
                "refreshed_embeddings": refreshed.get("refreshed", 0),
                "generation_id": self.repository.generation_id,
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

    @_generation_operation
    def refresh_indexes(self, *, limit: int = 8, retry_failed: bool = False) -> dict[str, Any]:
        if not self.indexes_writable or not self.mounted:
            return {"refreshed": 0, "read_only": True, "vector_available": self._vector_reads,
                    "detail": "embedding-space-mismatch; migrate this generation" if not self._vector_reads else "read-only"}
        if self.embedding_provider is None:
            return {"refreshed": 0, "vector_available": False, "detail": "embedding-provider-unavailable"}
        active_provider_id = self._embedding_provider_id()
        active_model_name = self._embedding_model_name()
        identity = self.migration_identity
        provider = self.embedding_provider
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
            vectors = provider.embed_documents(source_texts) if source_texts else []
            if source_texts and len(vectors) != len(source_texts):
                raise RuntimeError("embedding provider returned mismatched batch size")
            space = read_space(self.repository.database.connection())
            dimension = space.dimension if space is not None else None
            for vector in vectors:
                if (space is None or not accepts_vector(space.profile == identity == self.migration_identity, dimension, len(vector))
                        or not all(math.isfinite(value) for value in vector)):
                    raise RuntimeError("Embedding response changed identity or vector dimension; migrate embedding space first")
                dimension = len(vector)
            self.last_embedding_error = ""
        except Exception as exc:
            self.last_embedding_error = exception_reason(exc)
            self._sync_embedding_access()
            with self.repository.write_lock:
                if self.indexes_writable:
                    with self.repository.write_transaction():
                        for metadata, document in pending:
                            current = self.repository.get_embedding(metadata.embedding_id)
                            fields = ("provider_id", "model_name", "model_revision", "text_processing_version", "source_text_hash", "updated_at", "index_status")
                            if current is not None and all(getattr(current, field) == getattr(metadata, field) for field in fields):
                                self.repository.mark_embedding_failed(embedding_id=metadata.embedding_id, error_text=self.last_embedding_error)
                                failed_again += 1
            sqlite_vec_status = ensure_sqlite_vec_loaded(self.repository.database)
            return {
                "refreshed": 0,
                "vector_available": bool(sqlite_vec_status.available),
                "vector_backend_detail": sqlite_vec_status.detail,
                "retry_failed": retry_failed,
                "failed_retried": failed_retried,
                "failed_again": failed_again,
                "embedding_provider_id": active_provider_id,
                "embedding_model": active_model_name,
                "last_embedding_error": self.last_embedding_error,
            }
        for (metadata, document), vector in zip(pending, vectors):
            with self.repository.write_lock:
                if self.repository.frozen:
                    break
                current = self.repository.get_embedding(metadata.embedding_id)
                current_document = self.repository.get_document(metadata.document_id)
                if current is None or current_document is None:
                    continue
                expected = (metadata.provider_id, metadata.model_name, metadata.source_text_hash)
                profile = self._embedding_profile()
                if any(getattr(current, key) != value or getattr(metadata, key) != value for key, value in profile.items()):
                    continue
                if (current.provider_id, current.model_name, current.source_text_hash) != expected:
                    continue
                if current_document.get("content_revision") != document.get("content_revision"):
                    continue
                if self._embedding_provider_id() != metadata.provider_id or self._embedding_model_name() != metadata.model_name:
                    continue
                if self.migration_identity != identity:
                    raise RuntimeError("Embedding identity changed while indexing; response was discarded")
                with self.repository.write_transaction():
                    self.repository.upsert_vector_blob(embedding_id=metadata.embedding_id,
                        vector_blob=serialize_vector(vector), dimension=len(vector), profile=identity)
                    self.repository.mark_embedding_ready(embedding_id=metadata.embedding_id,
                        norm=math.sqrt(sum(value * value for value in vector)))
                refreshed += 1
        sqlite_vec_status = ensure_sqlite_vec_loaded(self.repository.database)
        return {
            "refreshed": refreshed,
            "vector_available": bool(sqlite_vec_status.available),
            "vector_backend_detail": sqlite_vec_status.detail,
            "retry_failed": retry_failed,
            "failed_retried": failed_retried,
            "failed_again": failed_again,
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
            space_profile=self.migration_identity,
            provider_id=self._embedding_provider_id(),
            model_name=self._embedding_model_name(),
            index_status="stale" if stale else "pending",
            **self._embedding_profile(),
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
        self._sync_embedding_access()
        if not search_text or self.embedding_provider is None or not self._vector_reads:
            return {}
        identity = self.migration_identity
        try:
            query_vector = self.embedding_provider.embed_query(search_text)
            space = read_space(self.repository.database.connection())
            if (space is None or not accepts_vector(space.profile == identity == self.migration_identity,
                    space.dimension, len(query_vector)) or not all(math.isfinite(value) for value in query_vector)):
                raise RuntimeError("Query vector differs from this generation's embedding space")
            self.last_embedding_error = ""
        except Exception as exc:
            self.last_embedding_error = exception_reason(exc)
            return {}
        sqlite_vec_scores = self.repository.query_vector_candidates_sqlite_vec(
            provider_id=self._embedding_provider_id(),
            model_name=self._embedding_model_name(),
            **self._embedding_profile(),
            query_vector=query_vector,
            limit=limit,
        )
        if sqlite_vec_scores is not None:
            return sqlite_vec_scores
        scores: dict[str, float] = {}
        for metadata, blob in self.repository.list_vector_rows(
            provider_id=self._embedding_provider_id(),
            model_name=self._embedding_model_name(),
            **self._embedding_profile(),
        ):
            candidate = deserialize_vector(blob)
            if len(candidate) != len(query_vector):
                continue
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

    def _embedding_profile(self) -> dict[str, Any]:
        return {"model_revision": getattr(self.embedding_provider, "model_revision", None),
                "text_processing_version": str(getattr(self.embedding_provider, "text_processing_version", "search_text_v1"))}

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
                "last_error": exception_reason(exc),
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
