from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pal.stream_events import NormalizedLLMStreamEvent


@dataclass(frozen=True)
class ThinkingChoice:
    choice_id: str
    label: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        choice_id = _normalize_thinking_name(self.choice_id)
        if not choice_id:
            raise ValueError("thinking choice_id must be non-empty")
        label = str(self.label or "").strip()
        if not label:
            raise ValueError("thinking choice label must be non-empty")
        aliases = tuple(
            alias
            for alias in (_normalize_thinking_name(item) for item in self.aliases)
            if alias and alias != choice_id
        )
        object.__setattr__(self, "choice_id", choice_id)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "aliases", tuple(dict.fromkeys(aliases)))


@dataclass(frozen=True)
class ThinkingContract:
    choices: tuple[ThinkingChoice, ...]
    default_choice_id: str

    def __post_init__(self) -> None:
        choices = tuple(self.choices)
        if not choices:
            raise ValueError("thinking contract must declare at least one choice")
        lookup: dict[str, str] = {}
        choice_ids: set[str] = set()
        for choice in choices:
            if choice.choice_id in choice_ids:
                raise ValueError(f"thinking choice_id is duplicated: {choice.choice_id}")
            choice_ids.add(choice.choice_id)
            for name in (choice.choice_id, *choice.aliases):
                previous = lookup.get(name)
                if previous is not None and previous != choice.choice_id:
                    raise ValueError(f"thinking choice name is ambiguous: {name}")
                lookup[name] = choice.choice_id
        default_choice_id = _normalize_thinking_name(self.default_choice_id)
        canonical_default = lookup.get(default_choice_id)
        if canonical_default is None or canonical_default != default_choice_id:
            raise ValueError("thinking default_choice_id must name a declared canonical choice")
        object.__setattr__(self, "choices", choices)
        object.__setattr__(self, "default_choice_id", default_choice_id)

    def resolve(self, value: object) -> str | None:
        normalized = _normalize_thinking_name(value)
        if not normalized:
            return None
        for choice in self.choices:
            if normalized == choice.choice_id or normalized in choice.aliases:
                return choice.choice_id
        return None

    def choice(self, choice_id: object) -> ThinkingChoice | None:
        resolved = self.resolve(choice_id)
        if resolved is None:
            return None
        return next((choice for choice in self.choices if choice.choice_id == resolved), None)


def _normalize_thinking_name(value: object) -> str:
    return str(value or "").strip().lower()


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
    invocation_result: Any | None = None
    context_delivery: dict[str, Any] | None = None

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
    input_tokens: int = 0
    uncached_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    usage_reported: bool = False
    provider_response_count: int = 0
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
