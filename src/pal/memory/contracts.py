from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class L1TranscriptMessage:
    role: str
    content: str
    tool_trace: str | None = None


@dataclass(frozen=True)
class MemoryQuery:
    level: str = "warm"
    queries: list[str] = field(default_factory=list)
    task_id: str | None = None
    topic_scope: list[str] = field(default_factory=list)
    limit: int = 8
    kind: str | None = None
    scope: str | None = None


@dataclass(frozen=True)
class L2Entry:
    entry_id: str
    kind: str
    scope: str
    title: str
    summary: str
    task_id: str | None = None
    source_kind: str = "l3_recall"
    source_ref: str = ""
    candidate_state: str = "candidate"
    touched_at: str = ""
    rendered: str = ""
    search_text: str = ""
    canonical_key: str | None = None
    dedupe_fingerprint: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class L3RecallResult:
    hits: list[dict[str, Any]] = field(default_factory=list)
    projected_entries: list[L2Entry] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class L3CommitRequest:
    kind: str
    title: str = ""
    summary: str = ""
    search_text: str = ""
    scope: str = "system"
    task_id: str | None = None
    canonical_key: str | None = None
    dedupe_fingerprint: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)
    situation_text: str = ""
    task_text: str = ""
    action_text: str = ""
    result_text: str = ""


@dataclass(frozen=True)
class L3CorrectRequest:
    document_id: str
    title: str | None = None
    summary: str | None = None
    search_text: str | None = None
    payload_patch: dict[str, Any] = field(default_factory=dict)
    topics: list[str] | None = None
    situation_text: str | None = None
    task_text: str | None = None
    action_text: str | None = None
    result_text: str | None = None


@dataclass(frozen=True)
class L3MutationResult:
    status: str
    document_id: str
    hit: dict[str, Any] = field(default_factory=dict)
    projected_entry: L2Entry | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class L3RetireResult:
    status: str
    document_ids: list[str] = field(default_factory=list)
    reused_document_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryPackRequest:
    turn_kind: str = "chat"
    task_id: str | None = None
    work_order_id: str | None = None


@dataclass(frozen=True)
class MemoryPack:
    l1_recent_context: list[L1TranscriptMessage] = field(default_factory=list)
    current_summary: L2Entry | None = None
    l2_top_of_mind: list[L2Entry] = field(default_factory=list)
    l2_active_entries: list[L2Entry] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryCompactRequest:
    target_input_budget: int
    reserved_output_tokens: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryCompactResult:
    summary: str
    projected_entries: list[L2Entry] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryCommitRequest:
    turn_id: str
    transcript: list[L1TranscriptMessage] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryCommitResult:
    status: str
    committed_transcript: list[L1TranscriptMessage] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class L1Store(Protocol):
    def append(self, item: list[L1TranscriptMessage]) -> None:
        ...


class L2Store(Protocol):
    def list_entries(self) -> list[L2Entry]:
        ...


class L3ProviderPort(Protocol):
    provider_id: str

    def inspect(self) -> dict[str, Any]:
        ...

    def recall(self, query: MemoryQuery) -> L3RecallResult:
        ...

    def commit(self, request: L3CommitRequest) -> L3MutationResult:
        ...

    def correct(self, request: L3CorrectRequest) -> L3MutationResult:
        ...

    def retire_entries(self, entries: list[L2Entry]) -> L3RetireResult:
        ...

    def refresh_indexes(self, *, limit: int = 8, retry_failed: bool = False) -> dict[str, Any]:
        ...


L3ProviderResolver = Callable[[str], L3ProviderPort]


class MemoryServicePort(Protocol):
    def compact(self, request: MemoryCompactRequest) -> MemoryCompactResult:
        ...

    async def acompact(self, request: MemoryCompactRequest) -> MemoryCompactResult:
        ...

    def commit_l1(self, request: MemoryCommitRequest) -> MemoryCommitResult:
        ...

    async def acommit_l1(self, request: MemoryCommitRequest) -> MemoryCommitResult:
        ...

    def build_pack(self, request: MemoryPackRequest) -> MemoryPack:
        ...

    async def abuild_pack(self, request: MemoryPackRequest) -> MemoryPack:
        ...

    def build_compaction_source_text(self, *, target_input_budget: int) -> str:
        ...
