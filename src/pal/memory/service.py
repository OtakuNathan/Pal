from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from pal.foundation import HeatPolicy, HeatStateMachine, utc_now
from pal.memory.contracts import (
    DEFAULT_GHOST_TTL,
    DEFAULT_HOT_TTL,
    L1Store,
    L1MessageKind,
    L1TranscriptMessage,
    L2Entry,
    L2HeatLevel,
    L2HeatState,
    L2Store,
    L3CommitRequest,
    L3CorrectRequest,
    L3DeleteRequest,
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
from pal.memory.compact import (
    SUMMARY_ENTRY_ID,
    current_summary_from_l1,
    flatten_l1_context,
    normalize_l1_transcript,
)
from pal.memory.repository import L3ProviderSelector
from pal.memory.tool_protocol import l1_tool_protocol_validation_error
from pal.shared import RuntimeStatus

L2_WORKING_SET_CAPACITY = 128
TOP_OF_MIND_LIMIT = 8
LOGGER = logging.getLogger(__name__)


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

    def delete(self, request: L3DeleteRequest) -> L3MutationResult:
        return L3MutationResult(status=RuntimeStatus.NOT_FOUND, document_id=request.document_id)

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
        normalized = normalize_l1_transcript(item)
        protocol_error = l1_tool_protocol_validation_error(normalized)
        if protocol_error:
            raise ValueError(f"invalid L1 tool protocol: {protocol_error}")
        if normalized:
            self.items.append(normalized)


@dataclass
class InMemoryL2Store(L2Store):
    items: dict[str, L2Entry] = field(default_factory=dict)
    top_of_mind_refs: list[str] = field(default_factory=list)
    capacity: int = L2_WORKING_SET_CAPACITY
    top_of_mind_limit: int = TOP_OF_MIND_LIMIT
    heat_registry: dict[str, L2HeatState] = field(default_factory=dict)
    heat_machine: HeatStateMachine = field(
        default_factory=lambda: HeatStateMachine(
            HeatPolicy(
                hot_ttl=DEFAULT_HOT_TTL,
                ghost_ttl=DEFAULT_GHOST_TTL,
                max_renewal_count=MAX_RENEWAL_COUNT,
            )
        )
    )

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
        transition = self.heat_machine.promote_to_hot(entry_id, current)
        if transition.state is not None:
            self.heat_registry[entry_id] = transition.state
        else:
            self.heat_registry.pop(entry_id, None)
        if transition.event == "hot_promoted":
            LOGGER.debug("memory_hot_promoted entry_id=%s source=%s", entry_id, source)
        elif transition.event == "hot_refreshed":
            LOGGER.debug("memory_hot_refreshed entry_id=%s remaining_ttl=%s", entry_id, DEFAULT_HOT_TTL)
        elif transition.event == "ghost_force_dormant":
            LOGGER.debug("memory_ghost_force_dormant entry_id=%s renewal_count=%s", entry_id, current.renewal_count if current else 0)
        elif transition.event == "ghost_reactivated":
            LOGGER.debug(
                "memory_ghost_reactivated entry_id=%s renewal_count=%s",
                entry_id,
                transition.state.renewal_count if transition.state else 0,
            )

    def tick_heat(self) -> list[str]:
        expired: list[str] = []
        for entry_id in list(self.heat_registry):
            current = self.heat_registry[entry_id]
            transition = self.heat_machine.tick(current)
            if transition.state is None:
                del self.heat_registry[entry_id]
            else:
                self.heat_registry[entry_id] = transition.state
            if transition.expired:
                expired.append(entry_id)
            if transition.event == "ghost_force_dormant":
                LOGGER.debug("memory_ghost_force_dormant entry_id=%s renewal_count=%s", entry_id, current.renewal_count)
            elif transition.event == "hot_to_ghost":
                LOGGER.debug(
                    "memory_hot_to_ghost entry_id=%s renewal_count=%s",
                    entry_id,
                    transition.state.renewal_count if transition.state else 0,
                )
            elif transition.event == "ghost_to_dormant":
                LOGGER.debug("memory_ghost_to_dormant entry_id=%s", entry_id)
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
        summary_entry = request.summary_entry
        if not isinstance(summary_entry, L2Entry):
            raise ValueError(
                "memory compact requires a validated and rendered summary_entry"
            )
        if not str(summary_entry.summary or "").strip():
            raise ValueError("memory compact summary_entry is empty")
        if not str(summary_entry.rendered or "").strip():
            raise ValueError("memory compact summary_entry is not rendered")
        projected_entries = [summary_entry]
        previous_l1_items = list(self.l1_store.items)
        previous_l2_items = dict(self.l2_store.items)
        previous_top_of_mind_refs = list(self.l2_store.top_of_mind_refs)
        previous_heat_registry = dict(self.l2_store.heat_registry)
        try:
            self.l1_store.items = [[
                L1TranscriptMessage(
                    role="assistant",
                    content=summary_entry.rendered or summary_entry.summary,
                    kind=L1MessageKind.RUNTIME_CONTEXT_SUMMARY,
                    payload=dict(summary_entry.payload or {}),
                )
            ]]
            self.remove_projected_entries([SUMMARY_ENTRY_ID])
        except Exception:
            self.l1_store.items = previous_l1_items
            self.l2_store.items = previous_l2_items
            self.l2_store.top_of_mind_refs = previous_top_of_mind_refs
            self.l2_store.heat_registry = previous_heat_registry
            raise
        return MemoryCompactResult(
            summary=summary_entry.summary,
            projected_entries=projected_entries,
            metadata={
                "target_input_budget": request.target_input_budget,
                "reserved_output_tokens": request.reserved_output_tokens,
                "projected_entry_count": 0,
                "compact_summary_count": 1,
                "retired_count": 0,
            },
        )

    def compact_transactionally(
        self,
        request: MemoryCompactRequest,
        *,
        after_commit: Callable[[], None],
    ) -> MemoryCompactResult:
        """Commit memory and its dependent projection as one rollback boundary."""

        previous_l1_items = list(self.l1_store.items)
        previous_l2_items = dict(self.l2_store.items)
        previous_top_of_mind_refs = list(self.l2_store.top_of_mind_refs)
        previous_heat_registry = dict(self.l2_store.heat_registry)
        try:
            result = self.compact(request)
            after_commit()
            return result
        except Exception:
            self.l1_store.items = previous_l1_items
            self.l2_store.items = previous_l2_items
            self.l2_store.top_of_mind_refs = previous_top_of_mind_refs
            self.l2_store.heat_registry = previous_heat_registry
            raise

    async def acompact_transactionally(
        self,
        request: MemoryCompactRequest,
        *,
        after_commit: Callable[[], None],
    ) -> MemoryCompactResult:
        return self.compact_transactionally(
            request,
            after_commit=after_commit,
        )

    async def acompact(self, request: MemoryCompactRequest) -> MemoryCompactResult:
        return self.compact(request)

    def commit_l1(self, request: MemoryCommitRequest) -> MemoryCommitResult:
        committed_transcript = normalize_l1_transcript(request.transcript)
        if not committed_transcript:
            return MemoryCommitResult(status=RuntimeStatus.SKIPPED, committed_transcript=[])
        protocol_error = l1_tool_protocol_validation_error(
            committed_transcript
        )
        if protocol_error:
            return MemoryCommitResult(
                status=RuntimeStatus.INVALID,
                committed_transcript=[],
                metadata={
                    "turn_id": request.turn_id,
                    "error": protocol_error,
                },
            )
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
        return self.commit_l1(request)

    def build_pack(self, request: MemoryPackRequest) -> MemoryPack:
        if request.turn_kind == "proactive_trigger":
            return MemoryPack(metadata={"turn_kind": request.turn_kind})
        current_summary = current_summary_from_l1(self.l1_store.items)
        hot_entries = self.l2_store.list_hot_entries()
        active_input_id = str(request.active_input_id or "").strip()
        valid_l1_items = [
            transcript
            for transcript in self.l1_store.items
            if not l1_tool_protocol_validation_error(transcript)
        ]
        l1_recent_context = [
            message
            for message in flatten_l1_context(valid_l1_items)
            if not (
                active_input_id
                and str(
                    dict(getattr(message, "payload", {}) or {}).get(
                        "_pal_input_id"
                    )
                    or ""
                ).strip()
                == active_input_id
            )
        ]
        return MemoryPack(
            l1_recent_context=l1_recent_context,
            current_summary=current_summary,
            l2_working_memory=hot_entries,
            metadata={
                "turn_kind": request.turn_kind,
                "task_id": request.task_id,
                "work_order_id": request.work_order_id,
                "active_input_id": active_input_id,
            },
        )

    async def abuild_pack(self, request: MemoryPackRequest) -> MemoryPack:
        return self.build_pack(request)

    def project_l3_entries(self, entries: list[L2Entry], *, touch: bool, top_of_mind: bool = True) -> None:
        self.project_l2_entries(entries, touch=touch, top_of_mind=top_of_mind)

    def project_l2_entries(self, entries: list[L2Entry], *, touch: bool, top_of_mind: bool = True) -> None:
        memory_entries = [entry for entry in entries if _is_memory_projection_entry(entry)]
        evicted = self.l2_store.upsert_entries(memory_entries, touch=touch, top_of_mind=top_of_mind)
        self._retire_entries(evicted)

    def project_mutation(self, result: L3MutationResult) -> None:
        if result.projected_entry is None:
            return
        self.project_l3_entries([result.projected_entry], touch=True, top_of_mind=True)

    def remove_projected_entries(self, entry_ids: list[str]) -> None:
        for entry_id in entry_ids:
            self.l2_store.items.pop(entry_id, None)
            self.l2_store.top_of_mind_refs = [value for value in self.l2_store.top_of_mind_refs if value != entry_id]
            self.l2_store.heat_registry.pop(entry_id, None)

    def soft_reset(self) -> None:
        self.l1_store.items = []
        self.l2_store.items = {}
        self.l2_store.top_of_mind_refs = []
        self.l2_store.heat_registry = {}

    async def asoft_reset(self) -> None:
        self.soft_reset()

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


def _is_memory_projection_entry(entry: L2Entry) -> bool:
    kind = str(getattr(entry, "kind", "") or "").strip()
    scope = str(getattr(entry, "scope", "") or "").strip()
    source_kind = str(getattr(entry, "source_kind", "") or "").strip()
    return scope != "behavior" and kind != "behavior_rule" and source_kind != "behavior_advice"
