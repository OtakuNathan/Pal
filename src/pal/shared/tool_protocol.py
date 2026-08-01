from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any, Generic, Literal, Mapping, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from pal.shared.json_values import freeze_json_mapping, thaw_json
from pal.shared.result_rendering import render_structured_for_llm


@dataclass(frozen=True)
class ToolDefinitionIR:
    """Provider-neutral definition exposed to an LLM or another tool caller."""

    name: str
    description: str
    input_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise ValueError("tool name must be non-empty")
        if not isinstance(self.input_schema, Mapping):
            raise TypeError("tool input schema must be an object")
        object.__setattr__(self, "input_schema", freeze_json_mapping(self.input_schema))


@dataclass(frozen=True, init=False)
class ToolCallIR:
    """A well-formed tool invocation. Received calls must already have an id."""

    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __init__(
        self,
        call_id: str,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        args: Mapping[str, Any] | None = None,
    ) -> None:
        if arguments is not None and args is not None:
            raise TypeError("provide tool arguments once, using arguments or args")
        if not str(call_id or "").strip():
            raise ValueError("tool call id must be non-empty")
        if not str(name or "").strip():
            raise ValueError("tool call name must be non-empty")
        payload = arguments if arguments is not None else args
        if payload is not None and not isinstance(payload, Mapping):
            raise TypeError("tool call arguments must be an object")
        object.__setattr__(self, "call_id", str(call_id))
        object.__setattr__(self, "name", str(name))
        object.__setattr__(self, "arguments", freeze_json_mapping(payload or {}))

    @property
    def args(self) -> dict[str, Any]:
        return thaw_json(self.arguments)


def new_tool_call(
    call_id: str | None = None,
    name: str = "",
    arguments: Mapping[str, Any] | None = None,
    *,
    args: Mapping[str, Any] | None = None,
) -> ToolCallIR:
    """Create a Pal-originated call, generating an id only at this explicit boundary."""

    return ToolCallIR(
        call_id=str(call_id or f"call_{uuid4().hex}"),
        name=name,
        arguments=arguments,
        args=args,
    )


@dataclass(frozen=True)
class ToolResultIR:
    """Conversation/IPC projection of one completed tool call."""

    call_id: str
    name: str
    content: str
    ok: bool = True
    status: str = "ok"
    structured: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not str(self.call_id or "").strip():
            raise ValueError("tool result call_id must be non-empty")
        if not str(self.name or "").strip():
            raise ValueError("tool result name must be non-empty")
        if self.structured is not None:
            object.__setattr__(self, "structured", freeze_json_mapping(self.structured))


class EffectOutcome(str, Enum):
    NONE = "none"
    NOT_STARTED = "not_started"
    NOT_APPLIED = "not_applied"
    APPLIED = "applied"
    UNKNOWN = "unknown"


class RetryDirective(str, Enum):
    CORRECT_INPUT = "correct_input"
    SAFE = "safe"
    RECONCILE_FIRST = "reconcile_first"
    DO_NOT_RETRY = "do_not_retry"


class _StrictProtocolModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ToolAffordance(_StrictProtocolModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str


T = TypeVar("T")


class CompleteResult(_StrictProtocolModel, Generic[T]):
    kind: Literal["complete"] = "complete"
    output: T
    effect: EffectOutcome
    llm_text: str
    affordances: list[ToolAffordance] = Field(default_factory=list)
    context_delivery: dict[str, Any] | None = Field(default=None, exclude=True)


class PagedResult(_StrictProtocolModel):
    kind: Literal["paged"] = "paged"
    result_handle: dict[str, Any]
    page_text: str
    effect: EffectOutcome
    llm_text: str
    affordances: list[ToolAffordance]
    context_delivery: dict[str, Any] | None = Field(default=None, exclude=True)


class RejectedResult(_StrictProtocolModel):
    kind: Literal["rejected"] = "rejected"
    error_code: str
    error: str
    effect: Literal[EffectOutcome.NOT_STARTED] = EffectOutcome.NOT_STARTED
    retry: RetryDirective
    llm_text: str
    affordances: list[ToolAffordance] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class FailedResult(_StrictProtocolModel):
    kind: Literal["failed"] = "failed"
    error_code: str
    error: str
    effect: EffectOutcome
    retry: RetryDirective
    llm_text: str
    affordances: list[ToolAffordance] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


ToolInvocationResult = Annotated[
    CompleteResult[Any] | PagedResult | RejectedResult | FailedResult,
    Field(discriminator="kind"),
]
TOOL_INVOCATION_RESULT_ADAPTER = TypeAdapter(ToolInvocationResult)


@dataclass(frozen=True)
class ToolExecutionResult:
    """Cross-module result returned by execution providers to agent runtimes."""

    name: str
    ok: bool
    llm_text: str
    text: str = ""
    structured: dict[str, Any] | None = None
    call_id: str | None = None
    status: str = ""
    invocation_result: ToolInvocationResult | None = None
    context_delivery: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not str(self.llm_text or "").strip():
            raise ValueError("ToolExecutionResult.llm_text must be non-empty")
        if not str(self.status or "").strip():
            object.__setattr__(self, "status", "ok" if self.ok else "error")


def default_tool_result_text(
    result: Any,
    *,
    fallback_ok: str = "ok",
    fallback_error: str = "error",
) -> str:
    llm_text = str(getattr(result, "llm_text", "") or "").strip()
    if llm_text:
        return llm_text
    text = str(getattr(result, "text", "") or "").strip()
    if text:
        return text
    structured = getattr(result, "structured", None)
    if structured:
        return render_structured_for_llm(structured)
    return fallback_ok if bool(getattr(result, "ok", False)) else fallback_error


__all__ = [
    "CompleteResult",
    "EffectOutcome",
    "FailedResult",
    "PagedResult",
    "RejectedResult",
    "RetryDirective",
    "TOOL_INVOCATION_RESULT_ADAPTER",
    "ToolAffordance",
    "ToolCallIR",
    "ToolDefinitionIR",
    "ToolExecutionResult",
    "ToolInvocationResult",
    "ToolResultIR",
    "default_tool_result_text",
    "new_tool_call",
]
