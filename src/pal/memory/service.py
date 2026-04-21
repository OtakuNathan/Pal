from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any

from pal.foundation import utc_now
from pal.memory.contracts import (
    DEFAULT_GHOST_TTL,
    DEFAULT_HOT_TTL,
    L1Store,
    L1TranscriptMessage,
    L2Entry,
    L2HeatLevel,
    L2HeatState,
    L2Store,
    L3CommitRequest,
    L3CorrectRequest,
    L3MutationResult,
    L3RecallResult,
    L3RetireResult,
    MAX_RENEWAL_COUNT,
    MemoryCommitRequest,
    MemoryCommitResult,
    MemoryCompactRequest,
    MemoryCompactResult,
    MemoryPack,
    MemoryPackRequest,
    MemoryServicePort,
)
from pal.memory.repository import L3ProviderSelector
from pal.shared import RuntimeStatus

SUMMARY_ENTRY_ID = "memory_summary_current"
SUMMARY_TITLE = "Conversation Summary"
L2_WORKING_SET_CAPACITY = 128
TOP_OF_MIND_LIMIT = 8


@dataclass
class DetachedL3Provider:
    provider_id: str = "null_l3"
    module_id: str = "l3.null_l3"

    def inspect(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "mounted": True,
            "vector_backend": "none",
            "pending_embeddings": 0,
        }

    def recall(self, query) -> L3RecallResult:
        _ = query
        return L3RecallResult()

    def commit(self, request: L3CommitRequest) -> L3MutationResult:
        _ = request
        return L3MutationResult(status=RuntimeStatus.SKIPPED, document_id="")

    def correct(self, request: L3CorrectRequest) -> L3MutationResult:
        _ = request
        return L3MutationResult(status=RuntimeStatus.NOT_FOUND, document_id="")

    def retire_entries(self, entries: list[L2Entry]) -> L3RetireResult:
        _ = entries
        return L3RetireResult(status=RuntimeStatus.SKIPPED)

    def refresh_indexes(self, *, limit: int = 8, retry_failed: bool = False) -> dict[str, object]:
        _ = (limit, retry_failed)
        return {"refreshed": 0, "vector_available": False}


@dataclass
class InMemoryL1Store(L1Store):
    items: list[list[L1TranscriptMessage]] = field(default_factory=list)

    def append(self, item: list[L1TranscriptMessage] | str) -> None:
        normalized = _normalize_l1_transcript(item)
        if normalized:
            self.items.append(normalized)


@dataclass
class InMemoryL2Store(L2Store):
    items: dict[str, L2Entry] = field(default_factory=dict)
    top_of_mind_refs: list[str] = field(default_factory=list)
    capacity: int = L2_WORKING_SET_CAPACITY
    top_of_mind_limit: int = TOP_OF_MIND_LIMIT
    heat_registry: dict[str, L2HeatState] = field(default_factory=dict)

    def list_entries(self) -> list[L2Entry]:
        return sorted(
            self.items.values(),
            key=lambda item: (item.touched_at, item.entry_id),
            reverse=True,
        )

    def get_entry(self, entry_id: str) -> L2Entry | None:
        return self.items.get(entry_id)

    def list_top_of_mind_entries(self) -> list[L2Entry]:
        return self.list_hot_entries()

    def list_active_entries(self) -> list[L2Entry]:
        return []

    def list_hot_entries(self) -> list[L2Entry]:
        result: list[L2Entry] = []
        for entry_id, state in self.heat_registry.items():
            if state.heat_level == L2HeatLevel.HOT:
                entry = self.items.get(entry_id)
                if entry is not None and not _is_summary_entry(entry):
                    result.append(entry)
        result.sort(key=lambda e: e.touched_at, reverse=True)
        return result

    def get_heat_state(self, entry_id: str) -> L2HeatState | None:
        return self.heat_registry.get(entry_id)

    def promote_to_hot(self, entry_id: str, *, source: str = "") -> None:
        if _is_summary_entry_by_id(entry_id):
            return
        current = self.heat_registry.get(entry_id)
        if current is None or current.heat_level == L2HeatLevel.DORMANT:
            state = L2HeatState(entry_id=entry_id, heat_level=L2HeatLevel.HOT, hot_ttl=DEFAULT_HOT_TTL, ghost_ttl=0, renewal_count=0)
            self.heat_registry[entry_id] = state
            print(f"[memory] memory_hot_promoted entry_id={entry_id} source={source}")
        elif current.heat_level == L2HeatLevel.HOT:
            state = L2HeatState(entry_id=entry_id, heat_level=L2HeatLevel.HOT, hot_ttl=DEFAULT_HOT_TTL, ghost_ttl=0, renewal_count=current.renewal_count)
            self.heat_registry[entry_id] = state
            print(f"[memory] memory_hot_refreshed entry_id={entry_id} remaining_ttl={DEFAULT_HOT_TTL}")
        elif current.heat_level == L2HeatLevel.GHOST:
            if current.renewal_count >= MAX_RENEWAL_COUNT:
                print(f"[memory] memory_ghost_force_dormant entry_id={entry_id} renewal_count={current.renewal_count}")
                return
            new_count = current.renewal_count + 1
            state = L2HeatState(entry_id=entry_id, heat_level=L2HeatLevel.HOT, hot_ttl=DEFAULT_HOT_TTL, ghost_ttl=0, renewal_count=new_count)
            self.heat_registry[entry_id] = state
            print(f"[memory] memory_ghost_reactivated entry_id={entry_id} renewal_count={new_count}")

    def tick_heat(self) -> list[str]:
        expired: list[str] = []
        for entry_id in list(self.heat_registry):
            state = self.heat_registry[entry_id]
            if state.heat_level == L2HeatLevel.HOT:
                new_ttl = state.hot_ttl - 1
                if new_ttl <= 0:
                    if state.renewal_count >= MAX_RENEWAL_COUNT:
                        del self.heat_registry[entry_id]
                        expired.append(entry_id)
                        print(f"[memory] memory_ghost_force_dormant entry_id={entry_id} renewal_count={state.renewal_count}")
                    else:
                        self.heat_registry[entry_id] = L2HeatState(entry_id=entry_id, heat_level=L2HeatLevel.GHOST, hot_ttl=0, ghost_ttl=DEFAULT_GHOST_TTL, renewal_count=state.renewal_count)
                        print(f"[memory] memory_hot_to_ghost entry_id={entry_id} renewal_count={state.renewal_count}")
                else:
                    self.heat_registry[entry_id] = L2HeatState(entry_id=entry_id, heat_level=L2HeatLevel.HOT, hot_ttl=new_ttl, ghost_ttl=0, renewal_count=state.renewal_count)
            elif state.heat_level == L2HeatLevel.GHOST:
                new_ttl = state.ghost_ttl - 1
                if new_ttl <= 0:
                    del self.heat_registry[entry_id]
                    expired.append(entry_id)
                    print(f"[memory] memory_ghost_to_dormant entry_id={entry_id}")
                else:
                    self.heat_registry[entry_id] = L2HeatState(entry_id=entry_id, heat_level=L2HeatLevel.GHOST, hot_ttl=0, ghost_ttl=new_ttl, renewal_count=state.renewal_count)
        return expired

    def upsert_entries(self, entries: list[L2Entry], *, touch: bool, top_of_mind: bool = False) -> list[L2Entry]:
        for entry in entries:
            normalized = _normalize_l2_entry(entry)
            current = self.items.get(normalized.entry_id)
            merged = normalized if current is None else replace(current, **normalized.__dict__)
            self.items[normalized.entry_id] = merged
            if touch:
                self.touch(normalized.entry_id, mark_top_of_mind=top_of_mind)
            if top_of_mind:
                self.promote_to_hot(normalized.entry_id, source="upsert")
        return self._evict_overflow()

    def touch(self, entry_id: str, *, mark_top_of_mind: bool = True) -> None:
        entry = self.items.get(entry_id)
        if entry is None:
            return
        now = utc_now()
        touched = replace(entry, touched_at=now)
        self.items[entry_id] = touched

    def _evict_overflow(self) -> list[L2Entry]:
        evicted: list[L2Entry] = []
        candidates = self._capacity_candidates()
        while len(candidates) > self.capacity:
            victim = candidates[0]
            removed = self.items.pop(victim.entry_id, None)
            self.top_of_mind_refs = [value for value in self.top_of_mind_refs if value != victim.entry_id]
            self.heat_registry.pop(victim.entry_id, None)
            if removed is not None:
                evicted.append(removed)
            candidates = self._capacity_candidates()
        return evicted

    def _capacity_candidates(self) -> list[L2Entry]:
        return sorted(
            [entry for entry in self.items.values() if _counts_against_l2_capacity(entry)],
            key=lambda item: (item.touched_at, item.entry_id),
        )


@dataclass
class MemoryService(MemoryServicePort):
    l1_store: InMemoryL1Store = field(default_factory=InMemoryL1Store)
    l2_store: InMemoryL2Store = field(default_factory=InMemoryL2Store)
    l3_selector: L3ProviderSelector | None = None
    failed_commits: list[MemoryCommitRequest] = field(default_factory=list)
    failed_retirements: list[L2Entry] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.l3_selector is None:
            self.l3_selector = L3ProviderSelector(resolver=lambda provider_id: DetachedL3Provider(provider_id=provider_id))

    def compact(self, request: MemoryCompactRequest) -> MemoryCompactResult:
        payload = _coerce_structured_compaction_payload(
            request.metadata.get("structured_compaction"),
            fallback_summary=str(request.metadata.get("semantic_summary") or "").strip(),
            existing_summary=self.l2_store.get_entry(SUMMARY_ENTRY_ID),
        )
        summary_entry = payload["summary_entry"]
        projected_entries = [summary_entry, *payload["stable_entries"]]
        self.l1_store.items = [[L1TranscriptMessage(role="assistant", content=summary_entry.summary)]]
        evicted = self.l2_store.upsert_entries(projected_entries, touch=True, top_of_mind=False)
        retired = self._retire_entries(evicted)
        return MemoryCompactResult(
            summary=summary_entry.summary,
            projected_entries=projected_entries,
            metadata={
                "target_input_budget": request.target_input_budget,
                "reserved_output_tokens": request.reserved_output_tokens,
                "projected_entry_count": len(projected_entries),
                "retired_count": retired,
            },
        )

    async def acompact(self, request: MemoryCompactRequest) -> MemoryCompactResult:
        return await asyncio.to_thread(self.compact, request)

    def commit_l1(self, request: MemoryCommitRequest) -> MemoryCommitResult:
        committed_transcript = _normalize_l1_transcript(request.transcript)
        if not committed_transcript:
            return MemoryCommitResult(status=RuntimeStatus.SKIPPED, committed_transcript=[])
        try:
            self.l1_store.append(committed_transcript)
        except Exception:
            self.failed_commits.append(request)
            return MemoryCommitResult(
                status=RuntimeStatus.RETRY,
                committed_transcript=[],
                metadata={"turn_id": request.turn_id},
            )
        return MemoryCommitResult(
            status=RuntimeStatus.OK,
            committed_transcript=committed_transcript,
            metadata={"turn_id": request.turn_id},
        )

    async def acommit_l1(self, request: MemoryCommitRequest) -> MemoryCommitResult:
        return await asyncio.to_thread(self.commit_l1, request)

    def build_pack(self, request: MemoryPackRequest) -> MemoryPack:
        if request.turn_kind == "service_trigger":
            return MemoryPack(metadata={"turn_kind": request.turn_kind})
        current_summary = self.l2_store.get_entry(SUMMARY_ENTRY_ID)
        hot_entries = self.l2_store.list_hot_entries()
        return MemoryPack(
            l1_recent_context=_flatten_recent_l1_context(self.l1_store.items),
            current_summary=current_summary,
            l2_working_memory=hot_entries,
            metadata={
                "turn_kind": request.turn_kind,
                "task_id": request.task_id,
                "work_order_id": request.work_order_id,
            },
        )

    async def abuild_pack(self, request: MemoryPackRequest) -> MemoryPack:
        return await asyncio.to_thread(self.build_pack, request)

    def build_compaction_source_text(self, *, target_input_budget: int) -> str:
        current_summary = self.l2_store.get_entry(SUMMARY_ENTRY_ID)
        recent_l1 = _flatten_recent_l1_context(self.l1_store.items)
        rendered_turns = _render_l1_recent_context(recent_l1)
        parts: list[str] = []
        if current_summary is not None and current_summary.summary.strip():
            parts.append(f"[Current Summary]\n{current_summary.summary.strip()}")
        if rendered_turns:
            parts.append(f"[Recent L1]\n{rendered_turns}")
        raw = "\n\n".join(parts).strip()
        if not raw:
            return ""
        limit = max(256, target_input_budget or 0)
        return raw[:limit]

    def project_l3_entries(self, entries: list[L2Entry], *, touch: bool, top_of_mind: bool = True) -> None:
        evicted = self.l2_store.upsert_entries(entries, touch=touch, top_of_mind=top_of_mind)
        self._retire_entries(evicted)

    def project_mutation(self, result: L3MutationResult) -> None:
        if result.projected_entry is None:
            return
        self.project_l3_entries([result.projected_entry], touch=True, top_of_mind=True)

    def _retire_entries(self, entries: list[L2Entry]) -> int:
        retireable = [entry for entry in entries if _should_retire_entry(entry)]
        if not retireable:
            return 0
        provider = self._resolve_l3_provider()
        if provider is None:
            self.failed_retirements.extend(retireable)
            return 0
        try:
            result = provider.retire_entries(retireable)
        except Exception:
            self.failed_retirements.extend(retireable)
            return 0
        if result.status != RuntimeStatus.OK:
            self.failed_retirements.extend(retireable)
            return 0
        return len(retireable)

    def _resolve_l3_provider(self):
        if self.l3_selector is None:
            return None
        try:
            return self.l3_selector.resolve()
        except Exception:
            return None


def _normalize_l1_transcript(item: list[L1TranscriptMessage] | list[dict[str, object]] | str) -> list[L1TranscriptMessage]:
    if isinstance(item, str):
        content = item.strip()
        return [L1TranscriptMessage(role="assistant", content=content)] if content else []
    normalized: list[L1TranscriptMessage] = []
    for entry in list(item or []):
        if isinstance(entry, L1TranscriptMessage):
            if entry.content.strip():
                normalized.append(L1TranscriptMessage(
                    role=entry.role,
                    content=entry.content.strip(),
                    tool_trace=entry.tool_trace,
                    tool_calls=entry.tool_calls,
                    tool_call_id=entry.tool_call_id,
                ))
            continue
        if isinstance(entry, dict):
            role = str(entry.get("role") or "").strip()
            content = str(entry.get("content") or "").strip()
            if role and content:
                normalized.append(L1TranscriptMessage(
                    role=role,
                    content=content,
                    tool_calls=entry.get("tool_calls"),
                    tool_call_id=entry.get("tool_call_id"),
                ))
    return normalized


def _flatten_recent_l1_context(items: list[list[L1TranscriptMessage]]) -> list[L1TranscriptMessage]:
    flattened: list[L1TranscriptMessage] = []
    for transcript in items:
        flattened.extend(_normalize_l1_transcript(transcript))
    return flattened


def _render_l1_recent_context(messages: list[L1TranscriptMessage]) -> str:
    lines: list[str] = []
    for message in messages:
        role = str(message.role or "").strip()
        content = str(message.content or "").strip()
        if role and content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines).strip()


def _normalize_l2_entry(entry: L2Entry) -> L2Entry:
    payload = dict(entry.payload or {})
    kind = str(entry.kind or "").strip().lower() or "fact"
    scope = str(entry.scope or "system").strip() or "system"
    title = str(entry.title or "").strip()
    summary = str(entry.summary or "").strip()
    rendered = str(entry.rendered or "").strip()
    search_text = str(entry.search_text or "").strip()
    source_kind = str(entry.source_kind or "l3_recall").strip() or "l3_recall"
    candidate_state = str(entry.candidate_state or "candidate").strip() or "candidate"
    source_ref = str(entry.source_ref or "").strip()
    touched_at = str(entry.touched_at or "").strip() or utc_now()
    canonical_key = str(entry.canonical_key or "").strip() or None
    dedupe_fingerprint = str(entry.dedupe_fingerprint or "").strip() or None

    if kind == "summary":
        title = title or SUMMARY_TITLE
        summary = summary or rendered
        rendered = rendered or summary
        search_text = search_text or summary
        return L2Entry(
            entry_id=SUMMARY_ENTRY_ID,
            kind="summary",
            scope=scope,
            task_id=entry.task_id,
            title=title,
            summary=summary,
            source_kind=source_kind,
            source_ref=source_ref,
            candidate_state="stable",
            touched_at=touched_at,
            rendered=rendered,
            search_text=search_text,
            canonical_key=canonical_key,
            dedupe_fingerprint=dedupe_fingerprint,
            payload=payload,
        )

    if kind not in {"fact", "case"}:
        kind = "fact"
    rendered = rendered or _render_entry(kind=kind, title=title, summary=summary, payload=payload)
    search_text = search_text or _search_text_from_entry(kind=kind, title=title, summary=summary, payload=payload)
    dedupe_fingerprint = dedupe_fingerprint or _stable_entry_fingerprint(
        kind=kind,
        scope=scope,
        task_id=entry.task_id,
        title=title,
        summary=summary,
        payload=payload,
        canonical_key=canonical_key,
    )
    entry_id = str(entry.entry_id or "").strip() or f"l2_{kind}_{dedupe_fingerprint[:12]}"
    return L2Entry(
        entry_id=entry_id,
        kind=kind,
        scope=scope,
        task_id=entry.task_id,
        title=title,
        summary=summary,
        source_kind=source_kind,
        source_ref=source_ref,
        candidate_state=candidate_state,
        touched_at=touched_at,
        rendered=rendered,
        search_text=search_text,
        canonical_key=canonical_key,
        dedupe_fingerprint=dedupe_fingerprint,
        payload=payload,
    )


def _coerce_structured_compaction_payload(
    raw: Any,
    *,
    fallback_summary: str,
    existing_summary: L2Entry | None,
) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    summary_payload = payload.get("summary") if isinstance(payload, dict) else None
    entries_payload = payload.get("entries") if isinstance(payload, dict) else None

    summary_text = ""
    summary_title = SUMMARY_TITLE
    summary_rendered = ""
    summary_search_text = ""
    summary_payload_blob: dict[str, Any] = {}

    if isinstance(summary_payload, dict):
        summary_text = str(summary_payload.get("summary") or "").strip()
        summary_title = str(summary_payload.get("title") or "").strip() or SUMMARY_TITLE
        summary_rendered = str(summary_payload.get("rendered") or "").strip()
        summary_search_text = str(summary_payload.get("search_text") or "").strip()
        summary_payload_blob = dict(summary_payload.get("payload") or {})
    elif isinstance(summary_payload, str):
        summary_text = summary_payload.strip()

    if not summary_text and fallback_summary:
        summary_text = fallback_summary
    if not summary_text and existing_summary is not None:
        summary_text = existing_summary.summary
        summary_title = existing_summary.title or summary_title
        summary_rendered = existing_summary.rendered or summary_rendered
        summary_search_text = existing_summary.search_text or summary_search_text
        summary_payload_blob = dict(existing_summary.payload or {})
    if not summary_text:
        summary_text = "No retained L1 memory."

    summary_entry = _normalize_l2_entry(
        L2Entry(
            entry_id=SUMMARY_ENTRY_ID,
            kind="summary",
            scope="system",
            title=summary_title,
            summary=summary_text,
            source_kind="l1_compaction",
            candidate_state="stable",
            touched_at=utc_now(),
            rendered=summary_rendered or summary_text,
            search_text=summary_search_text or summary_text,
            payload=summary_payload_blob,
        )
    )

    stable_entries: list[L2Entry] = []
    for item in list(entries_payload or []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in {"fact", "case"}:
            continue
        stable_entries.append(
            _normalize_l2_entry(
                L2Entry(
                    entry_id=str(item.get("entry_id") or ""),
                    kind=kind,
                    scope=str(item.get("scope") or "system"),
                    task_id=str(item.get("task_id")) if item.get("task_id") is not None else None,
                    title=str(item.get("title") or ""),
                    summary=str(item.get("summary") or ""),
                    source_kind="l1_compaction",
                    source_ref="",
                    candidate_state="stable",
                    touched_at=utc_now(),
                    rendered=str(item.get("rendered") or ""),
                    search_text=str(item.get("search_text") or ""),
                    canonical_key=str(item.get("canonical_key")) if item.get("canonical_key") is not None else None,
                    dedupe_fingerprint=str(item.get("dedupe_fingerprint")) if item.get("dedupe_fingerprint") is not None else None,
                    payload=dict(item.get("payload") or {}),
                )
            )
        )
    return {"summary_entry": summary_entry, "stable_entries": stable_entries}


def _render_entry(*, kind: str, title: str, summary: str, payload: dict[str, Any]) -> str:
    if kind == "case":
        situation = str(payload.get("situation") or payload.get("situation_text") or "").strip()
        task_text = str(payload.get("task") or payload.get("task_text") or "").strip()
        action = str(payload.get("action") or payload.get("action_text") or "").strip()
        result = str(payload.get("result") or payload.get("result_text") or "").strip()
        parts = [
            summary,
            f"Situation: {situation}" if situation else "",
            f"Task: {task_text}" if task_text else "",
            f"Action: {action}" if action else "",
            f"Result: {result}" if result else "",
        ]
        rendered = "\n".join(part for part in parts if part)
        return rendered.strip() or title
    return (summary or title).strip()


def _search_text_from_entry(*, kind: str, title: str, summary: str, payload: dict[str, Any]) -> str:
    if kind == "case":
        parts = [
            title,
            summary,
            str(payload.get("situation") or payload.get("situation_text") or "").strip(),
            str(payload.get("task") or payload.get("task_text") or "").strip(),
            str(payload.get("action") or payload.get("action_text") or "").strip(),
            str(payload.get("result") or payload.get("result_text") or "").strip(),
        ]
    else:
        parts = [title, summary]
        fact_text = str(payload.get("fact_text") or "").strip()
        if fact_text:
            parts.append(fact_text)
    return "\n".join(part for part in parts if part).strip()


def _stable_entry_fingerprint(
    *,
    kind: str,
    scope: str,
    task_id: str | None,
    title: str,
    summary: str,
    payload: dict[str, Any],
    canonical_key: str | None,
) -> str:
    normalized = {
        "canonical_key": canonical_key or "",
        "kind": kind,
        "payload": payload,
        "scope": scope,
        "summary": summary,
        "task_id": task_id or "",
        "title": title,
    }
    blob = json.dumps(normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _counts_against_l2_capacity(entry: L2Entry) -> bool:
    return entry.kind in {"fact", "case"}


def _is_summary_entry(entry: L2Entry) -> bool:
    return entry.kind == "summary" or entry.entry_id == SUMMARY_ENTRY_ID


def _is_summary_entry_by_id(entry_id: str) -> bool:
    return entry_id == SUMMARY_ENTRY_ID


def _should_retire_entry(entry: L2Entry) -> bool:
    if entry.kind not in {"fact", "case"}:
        return False
    if entry.candidate_state != "stable":
        return False
    return entry.source_kind != "l3_recall"
