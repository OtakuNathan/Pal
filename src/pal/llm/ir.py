from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Mapping, TypeAlias
from uuid import uuid4

from pal.shared.enums import LLMFinishReason
from pal.shared.json_values import freeze_json_mapping
from pal.shared.tool_protocol import (
    ToolCallIR as _ToolCallIR,
    ToolDefinitionIR as _ToolDefinitionIR,
    ToolResultIR as _ToolResultIR,
)


class WireShape(StrEnum):
    OPENAI_COMPLETION = "openai_completion"
    OPENAI_RESPONSE = "openai_response"
    ANTHROPIC_MESSAGES = "anthropic_messages"


class ThinkingLevel(StrEnum):
    OFF = "off"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class MessageRole(StrEnum):
    SYSTEM = "system"
    DEVELOPER = "developer"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class MessageState(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class LLMResponseDeltaKind(StrEnum):
    TEXT = "text"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    ITEM_COMMITTED = "item_committed"
    STATE = "state"


class LLMResponseItemKind(StrEnum):
    MESSAGE = "message"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TextPartIR:
    text: str


@dataclass(frozen=True)
class ImagePartIR:
    source: str
    media_type: str | None = None


@dataclass(frozen=True)
class ArtifactRefPartIR:
    """Stable Pal-owned reference carried in L1 until prompt projection."""

    artifact_id: str
    kind: str = ""
    file_name: str = ""
    summary: str = ""
    status: str = ""
    available_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.artifact_id or "").strip():
            raise ValueError("artifact_id must be non-empty")
        object.__setattr__(self, "available_actions", tuple(self.available_actions))


@dataclass(frozen=True)
class ReasoningPartIR:
    text: str = ""
    redacted: bool = False


ContentPartIR: TypeAlias = (
    TextPartIR
    | ImagePartIR
    | ArtifactRefPartIR
    | ReasoningPartIR
    | _ToolCallIR
    | _ToolResultIR
)


@dataclass(frozen=True)
class ReplayEnvelope:
    wire_shape: WireShape
    endpoint_id: str
    model_id: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "wire_shape", WireShape(self.wire_shape))
        if not str(self.endpoint_id or "").strip():
            raise ValueError("replay endpoint_id must be non-empty")
        if not str(self.model_id or "").strip():
            raise ValueError("replay model_id must be non-empty")
        if not isinstance(self.payload, Mapping):
            raise TypeError("replay payload must be an object")
        object.__setattr__(self, "payload", freeze_json_mapping(self.payload))

    def matches(self, *, wire_shape: WireShape, endpoint_id: str, model_id: str) -> bool:
        return (
            self.wire_shape == wire_shape
            and self.endpoint_id == str(endpoint_id)
            and self.model_id == str(model_id)
        )


@dataclass(frozen=True)
class LLMMessageIR:
    role: MessageRole
    parts: tuple[ContentPartIR, ...] = ()
    message_id: str = field(default_factory=lambda: str(uuid4()))
    state: MessageState = MessageState.COMPLETE
    semantic_kind: str = ""
    replay: ReplayEnvelope | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", MessageRole(self.role))
        object.__setattr__(self, "state", MessageState(self.state))
        if not str(self.message_id or "").strip():
            raise ValueError("message_id must be non-empty")
        object.__setattr__(self, "parts", tuple(self.parts))
        object.__setattr__(self, "metadata", freeze_json_mapping(self.metadata))
        for part in self.parts:
            if isinstance(part, ReasoningPartIR) and self.role != MessageRole.ASSISTANT:
                raise ValueError("reasoning is only valid on assistant messages")
            if isinstance(part, _ToolCallIR) and self.role != MessageRole.ASSISTANT:
                raise ValueError("tool calls are only valid on assistant messages")
            if isinstance(part, _ToolResultIR) and self.role != MessageRole.TOOL:
                raise ValueError("tool results are only valid on tool messages")
            if isinstance(part, ArtifactRefPartIR) and self.role != MessageRole.USER:
                raise ValueError("artifact references are only valid on user messages")
        if self.replay is not None and self.role != MessageRole.ASSISTANT:
            raise ValueError("wire replay is only valid on assistant messages")

    @property
    def text(self) -> str:
        return "".join(
            part.text if isinstance(part, TextPartIR) else part.content
            for part in self.parts
            if isinstance(part, (TextPartIR, _ToolResultIR))
        )

    @property
    def reasoning_text(self) -> str:
        return "".join(part.text for part in self.parts if isinstance(part, ReasoningPartIR))

    @property
    def tool_calls(self) -> tuple[_ToolCallIR, ...]:
        return tuple(part for part in self.parts if isinstance(part, _ToolCallIR))

    def retire_reasoning(self) -> "LLMMessageIR":
        return replace(
            self,
            parts=tuple(part for part in self.parts if not isinstance(part, ReasoningPartIR)),
            replay=None,
            state=MessageState.COMPLETE,
        )


@dataclass(frozen=True)
class GenerationPolicyIR:
    max_output_tokens: int
    temperature: float | None = None
    thinking_level: ThinkingLevel | None = None
    thinking_budget_tokens: int | None = None
    tool_choice: str = "auto"

    def __post_init__(self) -> None:
        if int(self.max_output_tokens) <= 0:
            raise ValueError("max_output_tokens must be positive")
        object.__setattr__(self, "max_output_tokens", int(self.max_output_tokens))
        if self.thinking_level is not None:
            object.__setattr__(
                self,
                "thinking_level",
                ThinkingLevel(self.thinking_level),
            )


@dataclass(frozen=True)
class LLMRequestIR:
    messages: tuple[LLMMessageIR, ...]
    tools: tuple[_ToolDefinitionIR, ...]
    policy: GenerationPolicyIR
    model_hint: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "metadata", freeze_json_mapping(self.metadata))
        if any(
            isinstance(part, ArtifactRefPartIR)
            for message in self.messages
            for part in message.parts
        ):
            raise ValueError("unresolved artifact reference reached LLM request boundary")


@dataclass(frozen=True)
class LLMUsageIR:
    input_tokens: int = 0
    uncached_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    reported: bool = False


@dataclass(frozen=True)
class LLMResponseIR:
    message: LLMMessageIR
    finish_reason: LLMFinishReason
    usage: LLMUsageIR = field(default_factory=LLMUsageIR)
    provider_response_count: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "finish_reason",
            LLMFinishReason(self.finish_reason),
        )
        if self.message.role != MessageRole.ASSISTANT:
            raise ValueError("LLM response message must have the assistant role")

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def reasoning_text(self) -> str:
        return self.message.reasoning_text

    @property
    def tool_calls(self) -> tuple[_ToolCallIR, ...]:
        return self.message.tool_calls


@dataclass(frozen=True)
class LLMResponseUpdate:
    response: LLMResponseIR
    delta_kind: LLMResponseDeltaKind
    text_delta: str = ""
    tool_call: _ToolCallIR | None = None
    item_id: str = ""
    item_kind: LLMResponseItemKind | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "delta_kind",
            LLMResponseDeltaKind(self.delta_kind),
        )
        if self.item_kind is not None:
            object.__setattr__(
                self,
                "item_kind",
                LLMResponseItemKind(self.item_kind),
            )
        if self.delta_kind == LLMResponseDeltaKind.ITEM_COMMITTED:
            if not str(self.item_id or "").strip():
                raise ValueError("committed LLM item must have a stable item_id")
            if self.item_kind is None:
                raise ValueError("committed LLM item must declare item_kind")
            if (
                self.item_kind == LLMResponseItemKind.TOOL_CALL
                and self.tool_call is None
            ):
                raise ValueError("committed LLM tool item must carry its tool call")
