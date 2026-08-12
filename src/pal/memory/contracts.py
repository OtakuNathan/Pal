from __future__ import annotations

from pal.shared.tool_protocol import ToolResultIR

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

if False:  # pragma: no cover - typing-only names without runtime import cycles
    from pal.llm.ir import LLMMessageIR
    from pal.memory.turn_ir import L1TurnIR

from pal.foundation import (
    DEFAULT_GHOST_TTL,
    DEFAULT_HOT_TTL,
    MAX_RENEWAL_COUNT,
    HeatLevel,
    HeatState,
)


# -- L2 Heat State Machine --
L2HeatLevel = HeatLevel
L2HeatState = HeatState


class L3RecallView(StrEnum):
    SUMMARY = "summary"
    ORIGIN = "origin"


class L1MessageKind(StrEnum):
    USER_REQUEST = "user_request"
    ASSISTANT_REPLY = "assistant_reply"
    ASSISTANT_TOOL_CALL = "assistant_tool_call"
    TOOL_RESULT = "tool_result"
    TURN_INTERRUPTED = "turn_interrupted"
    TURN_ABORTED = "turn_aborted"
    RUNTIME_CONTEXT_MEMORY = "runtime_context_memory"
    RUNTIME_CONTEXT_BEHAVIOR = "runtime_context_behavior"
    RUNTIME_CONTEXT_SKILL = "runtime_context_skill"
    RUNTIME_CONTEXT_ARTIFACT = "runtime_context_artifact"
    RUNTIME_CONTEXT_SUMMARY = "runtime_context_summary"


VECTOR_DEDUP_THRESHOLD = 0.85
RECALL_PROMOTION_THRESHOLD = 0.3


@dataclass(frozen=True)
class L1TranscriptMessage:
    role: str
    content: str
    kind: L1MessageKind | str = ""
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryQuery:
    level: str = "warm"
    queries: list[str] = field(default_factory=list)
    task_id: str | None = None
    topic_scope: list[str] = field(default_factory=list)
    limit: int = 8
    kind: str | None = None
    scope: str | None = None
    view: L3RecallView = L3RecallView.SUMMARY


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
    heat_state: L2HeatState | None = None


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
class L3DeleteRequest:
    document_id: str
    reason: str = ""


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
    active_input_id: str | None = None


@dataclass(frozen=True)
class MemoryPack:
    l1_recent_context: list[L1TranscriptMessage] = field(default_factory=list)
    l1_turns: list["L1TurnIR"] = field(default_factory=list)
    current_summary: L2Entry | None = None
    l2_working_memory: list[L2Entry] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryCompactRequest:
    target_input_budget: int
    reserved_output_tokens: int
    summary_entry: L2Entry | None = None
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

    def delete(self, request: L3DeleteRequest) -> L3MutationResult:
        ...

    def retire_entries(self, entries: list[L2Entry]) -> L3RetireResult:
        ...

    def refresh_indexes(self, *, limit: int = 8, retry_failed: bool = False) -> dict[str, Any]:
        ...


L3ProviderResolver = Callable[[str], L3ProviderPort]


class MemoryServicePort(Protocol):
    def begin_l1_turn(
        self,
        turn_id: str,
        *,
        user_text: str = "",
        user_message: "LLMMessageIR | None" = None,
        metadata: dict[str, Any] | None = None,
    ) -> "L1TurnIR":
        ...

    def active_l1_turn(self, turn_id: str) -> "L1TurnIR | None":
        ...

    def append_l1_user(
        self,
        turn_id: str,
        message: "LLMMessageIR",
    ) -> "L1TurnIR":
        ...

    def contains_l1_message(self, turn_id: str, message_id: str) -> bool:
        ...

    def upsert_l1_assistant(self, turn_id: str, message: "LLMMessageIR") -> "L1TurnIR":
        ...

    def discard_l1_assistant(self, turn_id: str, message_id: str) -> "L1TurnIR":
        ...

    def append_l1_tool_result(self, turn_id: str, result: "ToolResultIR") -> "L1TurnIR":
        ...

    def rollback_l1_tool_result(
        self,
        turn_id: str,
        *,
        previous: "L1TurnIR",
        call_id: str,
    ) -> "L1TurnIR":
        ...

    def settle_l1_turn(
        self,
        turn_id: str,
        *,
        after_commit: Callable[[], None] | None = None,
    ) -> "L1TurnIR":
        ...

    def interrupt_l1_turn(
        self,
        turn_id: str,
        *,
        reason: str = "",
        after_commit: Callable[[], None] | None = None,
    ) -> "L1TurnIR":
        ...

    def abort_l1_turn(
        self,
        turn_id: str,
        *,
        reason: str = "",
        after_commit: Callable[[], None] | None = None,
    ) -> "L1TurnIR":
        ...

    def compact(self, request: MemoryCompactRequest) -> MemoryCompactResult:
        ...

    async def acompact(self, request: MemoryCompactRequest) -> MemoryCompactResult:
        ...

    def compact_transactionally(
        self,
        request: MemoryCompactRequest,
        *,
        after_commit: Callable[[], None],
    ) -> MemoryCompactResult:
        ...

    async def acompact_transactionally(
        self,
        request: MemoryCompactRequest,
        *,
        after_commit: Callable[[], None],
    ) -> MemoryCompactResult:
        ...

    def commit_l1(self, request: MemoryCommitRequest) -> MemoryCommitResult:
        ...

    async def acommit_l1(self, request: MemoryCommitRequest) -> MemoryCommitResult:
        ...

    def build_pack(self, request: MemoryPackRequest) -> MemoryPack:
        ...

    async def abuild_pack(self, request: MemoryPackRequest) -> MemoryPack:
        ...

    def project_l2_entries(self, entries: list[L2Entry], *, touch: bool, top_of_mind: bool = True) -> None:
        ...

    def project_l3_entries(self, entries: list[L2Entry], *, touch: bool, top_of_mind: bool = True) -> None:
        ...

    def soft_reset(self) -> None:
        ...

    async def asoft_reset(self) -> None:
        ...
