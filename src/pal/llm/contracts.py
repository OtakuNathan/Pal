from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR as _ToolCallIR

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from pal.llm.ir import (
    LLMFinishReason,
    LLMRequestIR,
    LLMResponseIR,
    LLMResponseUpdate,
    LLMMessageIR,
    LLMUsageIR,
    MessageRole,
    ReasoningPartIR,
    TextPartIR,
)
from pal.llm.conversions import request_ir_from_prompt


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
class LLMGenerationResult:
    response: LLMResponseIR
    response_mode: str | None = None
    target_input_budget: int = 0
    reserved_output_tokens: int = 0
    preferred_endpoint_id: str | None = None
    preferred_model_id: str | None = None

    @property
    def text(self) -> str:
        return self.response.text

    @property
    def reasoning_text(self) -> str:
        return self.response.reasoning_text

    @property
    def tool_calls(self) -> tuple[_ToolCallIR, ...]:
        return self.response.tool_calls

    @property
    def finish_reason(self) -> LLMFinishReason:
        return self.response.finish_reason

    @property
    def input_tokens(self) -> int:
        return self.response.usage.input_tokens

    @property
    def uncached_input_tokens(self) -> int:
        return self.response.usage.uncached_input_tokens

    @property
    def cached_input_tokens(self) -> int:
        return self.response.usage.cached_input_tokens

    @property
    def cache_write_input_tokens(self) -> int:
        return self.response.usage.cache_write_input_tokens

    @property
    def output_tokens(self) -> int:
        return self.response.usage.output_tokens

    @property
    def reasoning_tokens(self) -> int:
        return self.response.usage.reasoning_tokens

    @property
    def reasoning_tokens_reported(self) -> bool:
        return self.response.usage.reasoning_tokens_reported

    @property
    def cost(self) -> float:
        return self.response.usage.cost

    @property
    def usage_reported(self) -> bool:
        return self.response.usage.reported

    @property
    def provider_response_count(self) -> int:
        return self.response.provider_response_count


@dataclass(frozen=True)
class LLMPreflightRequest:
    request: LLMRequestIR


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

    def generate(self, request: LLMRequestIR) -> LLMGenerationResult:
        ...

    async def agenerate(self, request: LLMRequestIR) -> LLMGenerationResult:
        ...

    def astream(self, request: LLMRequestIR) -> AsyncIterator[LLMResponseUpdate]:
        ...

def generation_result_from_values(
    text: str = "",
    *,
    reasoning_text: str = "",
    tool_calls: list[_ToolCallIR] | tuple[_ToolCallIR, ...] = (),
    finish_reason: LLMFinishReason | str = LLMFinishReason.STOP,
    input_tokens: int = 0,
    uncached_input_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_write_input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    reasoning_tokens_reported: bool | None = None,
    cost: float = 0.0,
    usage_reported: bool = False,
    provider_response_count: int = 1,
    response_mode: str | None = None,
    target_input_budget: int = 0,
    reserved_output_tokens: int = 0,
    preferred_endpoint_id: str | None = None,
    preferred_model_id: str | None = None,
) -> LLMGenerationResult:
    reason = _normalize_finish_reason(finish_reason)
    parts: list[Any] = []
    if reasoning_text:
        parts.append(ReasoningPartIR(str(reasoning_text)))
    if text:
        parts.append(TextPartIR(str(text)))
    parts.extend(tool_calls)
    return LLMGenerationResult(
        response=LLMResponseIR(
            message=LLMMessageIR(role=MessageRole.ASSISTANT, parts=tuple(parts)),
            finish_reason=reason,
            usage=LLMUsageIR(
                input_tokens=input_tokens,
                uncached_input_tokens=uncached_input_tokens,
                cached_input_tokens=cached_input_tokens,
                cache_write_input_tokens=cache_write_input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                reasoning_tokens_reported=(
                    bool(reasoning_tokens) and bool(usage_reported)
                    if reasoning_tokens_reported is None
                    else bool(reasoning_tokens_reported)
                ),
                cost=cost,
                reported=usage_reported,
            ),
            provider_response_count=provider_response_count,
        ),
        response_mode=response_mode,
        target_input_budget=target_input_budget,
        reserved_output_tokens=reserved_output_tokens,
        preferred_endpoint_id=preferred_endpoint_id,
        preferred_model_id=preferred_model_id,
    )


def _normalize_finish_reason(value: LLMFinishReason | str) -> LLMFinishReason:
    if isinstance(value, LLMFinishReason):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"length", "max_tokens", "max_output_tokens", "token_limit", "output_truncated"}:
        return LLMFinishReason.LENGTH
    if normalized in {"tool_calls", "tool_use", "function_call"}:
        return LLMFinishReason.TOOL_CALLS
    if normalized in {"error", "failed", "cancelled", "canceled"}:
        return LLMFinishReason.ERROR
    return LLMFinishReason(normalized or LLMFinishReason.STOP.value)
