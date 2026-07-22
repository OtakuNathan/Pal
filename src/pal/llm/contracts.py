from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pal.stream_events import NormalizedLLMStreamEvent


@dataclass(frozen=True)
class ToolResultHandle:
    result_ref: str
    turn_id: str = ""
    backing_path: str = ""
    page_size: int = 0
    original_size: int = 0
    page_count: int = 0
    created_user_turn_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_ref": self.result_ref,
            "turn_id": self.turn_id,
            "backing_path": self.backing_path,
            "page_size": self.page_size,
            "original_size": self.original_size,
            "page_count": self.page_count,
            "created_user_turn_index": self.created_user_turn_index,
        }


@dataclass(frozen=True)
class CanonicalLLMRequest:
    messages: list[dict[str, Any]]
    max_output_tokens: int
    model_hint: str | None = None
    temperature: float | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    thinking_budget_tokens: int | None = None


@dataclass(frozen=True)
class CanonicalToolCall:
    name: str
    args: dict[str, Any]
    call_id: str | None = None


@dataclass(frozen=True)
class CanonicalToolResult:
    name: str
    ok: bool
    llm_text: str
    text: str = ""
    structured: dict[str, Any] | None = None
    call_id: str | None = None
    status: str = ""
    result_handle: ToolResultHandle | None = None
    invocation_result: Any | None = None

    def __post_init__(self) -> None:
        if not str(self.llm_text or "").strip():
            raise ValueError("CanonicalToolResult.llm_text must be non-empty")
        if not str(self.status or "").strip():
            object.__setattr__(self, "status", "ok" if self.ok else "error")


@dataclass(frozen=True)
class CanonicalLLMOutcome:
    text: str = ""
    reasoning_text: str = ""
    tool_calls: list[CanonicalToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    response_mode: str | None = None
    target_input_budget: int = 0
    reserved_output_tokens: int = 0
    preferred_endpoint_id: str | None = None
    preferred_model_id: str | None = None
    provider_specific_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMPreflightRequest:
    messages: list[dict[str, Any]]
    max_output_tokens: int
    model_hint: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMPreflightAdvice:
    status: str
    active_model: str | None = None
    fallback_chain: list[str] = field(default_factory=list)
    target_input_budget: int = 0
    reserved_output_tokens: int = 0
    breakdown: dict[str, int | bool] = field(default_factory=dict)


class LLMRuntimePort(Protocol):
    def preflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        ...

    async def apreflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        ...

    def generate(self, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        ...

    async def agenerate(self, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        ...

    def generate_stream(self, request: CanonicalLLMRequest) -> list[NormalizedLLMStreamEvent]:
        ...

    async def agenerate_stream(self, request: CanonicalLLMRequest) -> list[NormalizedLLMStreamEvent]:
        ...
