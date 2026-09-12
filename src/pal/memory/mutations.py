"""Transactional memory mutations. Similarity never authorizes an overwrite."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from uuid import uuid4

from pal.foundation import utc_now
from pal.memory.contracts import L3CommitRequest, L3MutationResult


def content_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def mutation_id_from_call(call) -> str | None:
    explicit = str(call.meta.get("mutation_id") or "")
    tool_call = call.meta.get("tool_call")
    call_id = str(getattr(tool_call, "call_id", "") or "")
    if explicit:
        return explicit
    if call_id:
        return f"{call.meta.get('turn_id') or 'host'}:{call_id}:{call.name}"
    return None


def canonical_content(request: L3CommitRequest) -> dict:
    value = asdict(request)
    value.pop("mutation_id", None)
    value.pop("dedupe_fingerprint", None)
    value["topics"] = sorted(set(request.topics))
    value["search_text"] = request.search_text or request.summary or request.title
    return value


def _replayed(repo, request, operation):
    mutation_id = getattr(request, "mutation_id", None)
    if not mutation_id:
        return None
    digest = content_hash({"operation": operation, "request": asdict(request)})
    row = repo.database.execute_sql("SELECT request_hash, document_id FROM memory_mutations WHERE mutation_id=?", (mutation_id,)).fetchone()
    if row is None:
        return None
    if row[0] != digest:
        return L3MutationResult(status="conflict", document_id=row[1], metadata={"reason": "mutation_id reused with different arguments"})
    if repo.catalog is not None and repo.catalog.is_deleted(row[1]):
        return L3MutationResult(status="not_found", document_id=row[1], metadata={"replayed": True, "forgotten": True})
    return L3MutationResult(status="ok", document_id=row[1], hit=repo.get_document(row[1]) or {}, metadata={"replayed": True})


def _remember_result(repo, request, operation, result):
    if getattr(request, "mutation_id", None) and result.status == "ok":
        repo.database.execute_sql("INSERT INTO memory_mutations VALUES (?, ?, ?)", (
            request.mutation_id, content_hash({"operation": operation, "request": asdict(request)}), result.document_id,
        ))
    return result


def _insert(provider, request, *, revision=1):
    repo = provider.repository
    values = canonical_content(request)
    fingerprint = content_hash(values)
    record_id = f"{request.kind}_{uuid4().hex[:12]}"
    common = dict(scope=request.scope, task_id=request.task_id, title=request.title,
                  summary=request.summary, search_text=values["search_text"], canonical_key=request.canonical_key,
                  dedupe_fingerprint=fingerprint, payload_blob=dict(request.payload),
                  content_revision=revision, last_used_at=utc_now())
    if request.kind == "fact":
        repo.upsert_fact(fact_id=record_id, payload=common)
    else:
        repo.upsert_case(case_id=record_id, payload={**common, **{
            name: getattr(request, name) for name in ("situation_text", "task_text", "action_text", "result_text")
        }})
    document_id = f"{request.kind}:{record_id}"
    repo.replace_topics(document_id, request.topics)
    repo.sync_fts_row(document_id)
    provider._mark_document_pending(document_id)
    return _result(provider, document_id)


def _result(provider, document_id, **metadata):
    hit = provider.repository.get_document(document_id) or {}
    entry = provider._project_entry(hit, source_kind="explicit_commit", candidate_state="stable") if hit else None
    return L3MutationResult(status="ok", document_id=document_id, hit=hit, projected_entry=entry,
                            metadata={"index_status": "pending", **metadata})


def commit(provider, request):
    repo = provider.repository
    if not provider.mounted or provider.read_only or repo.frozen:
        return L3MutationResult(status="unavailable", document_id="")
    if request.kind not in {"fact", "case"} or request.scope not in {"system", "task"}:
        return L3MutationResult(status="invalid", document_id="")
    if (request.scope == "task") != bool(request.task_id):
        return L3MutationResult(status="invalid", document_id="", metadata={"reason": "task scope requires task_id"})
    with repo.write_transaction():
        replayed = _replayed(repo, request, "remember")
        if replayed is not None:
            return replayed
        model = repo.Fact if request.kind == "fact" else repo.Case
        query = model.select().where((model.scope == request.scope) & (model.task_id == request.task_id) & (model.lifecycle == "active"))
        fingerprint = content_hash(canonical_content(request))
        # Cases without a stable event identity must remain separate even if
        # their descriptions are identical. Retries use mutation_id instead.
        event = request.payload.get("event_id") or request.payload.get("source_event_id")
        exact_allowed = request.kind == "fact" or (isinstance(event, str) and bool(event.strip()))
        exact = query.where(model.dedupe_fingerprint == fingerprint).first() if exact_allowed else None
        existing = query.where(model.canonical_key == request.canonical_key).first() if request.canonical_key else None
        if exact is not None:
            result = _result(provider, f"{request.kind}:{exact.get_id()}", deduplicated=True)
        elif existing is not None:
            return L3MutationResult(status="conflict", document_id=f"{request.kind}:{existing.get_id()}",
                                    metadata={"reason": "canonical identity exists; use explicit update"})
        else:
            result = _insert(provider, request)
        _remember_result(repo, request, "remember", result)
    provider.service.project_mutation(result)
    return result


def document_request(document):
    payload = dict(document.get("payload") or {})
    star = {name: payload.pop(name, "") for name in ("situation_text", "task_text", "action_text", "result_text")}
    return L3CommitRequest(kind=document["document_kind"], scope=document["scope"], task_id=document.get("task_id"),
                           title=document["title"], summary=document["summary"], search_text=document["search_text"],
                           canonical_key=document.get("canonical_key"), topics=list(document.get("topics") or []), payload=payload, **star)


def retire_document(repo, document, successors, *, reason):
    import base64
    ref = document["document_id"]
    document = dict(document)
    document["original_index"] = []
    for metadata in repo.Embedding.select().where(repo.Embedding.document_id == ref):
        blob = repo.get_vector_blob(metadata.embedding_id)
        document["original_index"].append({"metadata": dict(metadata.__data__),
            "vector_base64": base64.b64encode(blob).decode("ascii") if blob is not None else None})
    kind, _, record_id = ref.partition(":")
    model = repo.Fact if kind == "fact" else repo.Case
    model.update(lifecycle="superseded").where(model._meta.primary_key == record_id).execute()
    repo.database.execute_sql("INSERT OR IGNORE INTO memory_revisions VALUES (?, ?, ?, ?, ?, 0)", (
        ref, document["content_revision"], json.dumps(document, ensure_ascii=False), json.dumps(successors), reason,
    ))
    repo.sync_fts_row(ref)
    # Retired documents cannot participate in vector search or exact dedup.
    repo.Embedding.update(index_status="stale").where(repo.Embedding.document_id == ref).execute()


def correct(provider, request):
    repo = provider.repository
    if not provider.mounted or provider.read_only or repo.frozen:
        return L3MutationResult(status="unavailable", document_id=request.document_id)
    with repo.write_transaction():
        replayed = _replayed(repo, request, "update")
        if replayed is not None:
            return replayed
        old = repo.get_document(request.document_id)
        if old is None:
            row = repo.database.execute_sql("SELECT successors_json FROM memory_revisions WHERE document_id=?", (request.document_id,)).fetchone()
            return L3MutationResult(status="conflict" if row else "not_found", document_id=request.document_id,
                                    metadata={"successors": json.loads(row[0]) if row else []})
        value = document_request(old)
        changes = {name: getattr(request, name) for name in (
            "title", "summary", "search_text", "topics", "situation_text", "task_text", "action_text", "result_text"
        ) if getattr(request, name) is not None}
        changes["payload"] = {**value.payload, **request.payload_patch}
        updated = replace(value, **changes)
        if canonical_content(value) == canonical_content(updated):
            result = _result(provider, request.document_id, no_changes=True)
        else:
            result = _insert(provider, updated, revision=old["content_revision"] + 1)
            retire_document(repo, old, [result.document_id], reason="explicit_update")
        _remember_result(repo, request, "update", result)
    if result.document_id != request.document_id:
        provider.service.remove_projected_entries([request.document_id])
    provider.service.project_mutation(result)
    return result
