from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

from pal.memory.contracts import (
    L1Store,
    L1TranscriptMessage,
    L2Entry,
    L2Store,
    L3CommitRequest,
    L3CorrectRequest,
    L3MutationResult,
    L3RecallResult,
    MemoryCommitRequest,
    MemoryCommitResult,
    MemoryCompactRequest,
    MemoryCompactResult,
    MemoryPack,
    MemoryQuery,
    MemoryServicePort,
)
from pal.memory.repository import L3ProviderSelector
from pal.shared import RuntimeStatus
from pal.foundation import utc_now


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

    def recall(self, query: MemoryQuery) -> L3RecallResult:
        _ = query
        return L3RecallResult()

    def commit(self, request: L3CommitRequest) -> L3MutationResult:
        _ = request
        return L3MutationResult(status=RuntimeStatus.SKIPPED, document_id="")

    def correct(self, request: L3CorrectRequest) -> L3MutationResult:
        _ = request
        return L3MutationResult(status=RuntimeStatus.NOT_FOUND, document_id="")

    def refresh_indexes(self, *, limit: int = 8) -> dict[str, object]:
        _ = limit
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

    def list_entries(self) -> list[L2Entry]:
        return sorted(
            self.items.values(),
            key=lambda item: (item.touched_at, item.entry_id),
            reverse=True,
        )

    def upsert_entries(self, entries: list[L2Entry], *, touch: bool) -> None:
        for entry in entries:
            current = self.items.get(entry.entry_id)
            merged = entry if current is None else replace(current, **entry.__dict__)
            self.items[entry.entry_id] = merged
            if touch:
                self.touch(entry.entry_id)

    def touch(self, entry_id: str) -> None:
        entry = self.items.get(entry_id)
        if entry is None:
            return
        now = utc_now()
        self.items[entry_id] = replace(entry, touched_at=now)
        refs = [value for value in self.top_of_mind_refs if value != entry_id]
        refs.append(entry_id)
        self.top_of_mind_refs = refs[-8:]


@dataclass
class MemoryService(MemoryServicePort):
    l1_store: InMemoryL1Store = field(default_factory=InMemoryL1Store)
    l2_store: InMemoryL2Store = field(default_factory=InMemoryL2Store)
    l3_selector: L3ProviderSelector | None = None
    failed_commits: list[MemoryCommitRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.l3_selector is None:
            self.l3_selector = L3ProviderSelector(resolver=lambda provider_id: DetachedL3Provider(provider_id=provider_id))

    def compact(self, request: MemoryCompactRequest) -> MemoryCompactResult:
        flattened_turns: list[str] = []
        for transcript in self.l1_store.items:
            parts = [f"{message.role.capitalize()}: {message.content}" for message in transcript if message.content.strip()]
            if parts:
                flattened_turns.append(" | ".join(parts))
        raw = " || ".join(flattened_turns) or "No retained L1 memory."
        limit = max(request.target_input_budget // 8, 64)
        summary = str(request.metadata.get("semantic_summary") or "").strip() or raw[:limit]
        self.l1_store.items = [[L1TranscriptMessage(role="assistant", content=summary)]]
        if summary:
            self.l2_store.upsert_entries(
                [
                    L2Entry(
                        entry_id="memory_compaction_recent",
                        kind="fact",
                        scope="system",
                        title="Conversation Summary",
                        summary=summary,
                        rendered=summary,
                        source_kind="l1_compaction",
                        candidate_state="stable",
                        touched_at=utc_now(),
                    )
                ],
                touch=True,
            )
        return MemoryCompactResult(
            summary=summary,
            metadata={
                "target_input_budget": request.target_input_budget,
                "reserved_output_tokens": request.reserved_output_tokens,
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

    def build_pack(self, query: MemoryQuery) -> MemoryPack:
        assert self.l3_selector is not None
        recall = self.l3_selector.resolve().recall(query)
        self.l2_store.upsert_entries(recall.projected_entries, touch=True)
        l3_hits = recall.hits
        return MemoryPack(
            l1_items=[list(item) for item in self.l1_store.items],
            l2_items=self.l2_store.list_entries(),
            l3_hits=l3_hits,
        )

    async def abuild_pack(self, query: MemoryQuery) -> MemoryPack:
        return await asyncio.to_thread(self.build_pack, query)

    def project_l3_entries(self, entries: list[L2Entry], *, touch: bool) -> None:
        self.l2_store.upsert_entries(entries, touch=touch)

    def project_mutation(self, result: L3MutationResult) -> None:
        if result.projected_entry is None:
            return
        self.project_l3_entries([result.projected_entry], touch=True)


def _normalize_l1_transcript(item: list[L1TranscriptMessage] | list[dict[str, object]] | str) -> list[L1TranscriptMessage]:
    if isinstance(item, str):
        content = item.strip()
        return [L1TranscriptMessage(role="assistant", content=content)] if content else []
    normalized: list[L1TranscriptMessage] = []
    for entry in list(item or []):
        if isinstance(entry, L1TranscriptMessage):
            if entry.content.strip():
                normalized.append(L1TranscriptMessage(role=entry.role, content=entry.content.strip()))
            continue
        if isinstance(entry, dict):
            role = str(entry.get("role") or "").strip()
            content = str(entry.get("content") or "").strip()
            if role and content:
                normalized.append(L1TranscriptMessage(role=role, content=content))
    return normalized
