from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
from pal.shared.json_values import freeze_json_mapping, thaw_json

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Mapping

from pal.llm.ir import (
    LLMMessageIR,
    MessageRole,
    MessageState,
    TextPartIR,
)


class L1TurnState(StrEnum):
    ACTIVE = "active"
    SETTLED = "settled"
    INTERRUPTED = "interrupted"
    ABORTED = "aborted"


class L1TurnProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class L1TurnIR:
    turn_id: str
    messages: tuple[LLMMessageIR, ...]
    state: L1TurnState = L1TurnState.ACTIVE
    revision: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.turn_id or "").strip():
            raise ValueError("L1 turn_id must be non-empty")
        object.__setattr__(self, "state", L1TurnState(self.state))
        if int(self.revision) < 0:
            raise ValueError("L1 turn revision must be non-negative")
        object.__setattr__(self, "revision", int(self.revision))
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "metadata", freeze_json_mapping(self.metadata))
        _validate_protocol(self.messages, allow_pending=self.state == L1TurnState.ACTIVE)
        if self.state != L1TurnState.ACTIVE:
            for message in self.messages:
                if message.state == MessageState.IN_PROGRESS:
                    raise L1TurnProtocolError("settled L1 turn contains an in-progress message")
                if message.reasoning_text or message.replay is not None:
                    raise L1TurnProtocolError("settled L1 turn retains transient reasoning replay")

    @classmethod
    def begin(
        cls,
        turn_id: str,
        *,
        user_text: str = "",
        user_message: LLMMessageIR | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "L1TurnIR":
        if user_message is not None and str(user_text or ""):
            raise ValueError("provide user_message or user_text, not both")
        if user_message is not None and user_message.role != MessageRole.USER:
            raise ValueError("initial L1 message must have user role")
        messages: tuple[LLMMessageIR, ...] = ()
        if user_message is not None:
            messages = (user_message,)
        elif str(user_text or ""):
            messages = (
                LLMMessageIR(
                    role=MessageRole.USER,
                    parts=(TextPartIR(str(user_text)),),
                    semantic_kind="user_request",
                ),
            )
        return cls(turn_id=str(turn_id), messages=messages, metadata=dict(metadata or {}))

    @property
    def pending_call_ids(self) -> frozenset[str]:
        calls, results = _protocol_ids(self.messages)
        return frozenset(calls - results)

    @property
    def semantic_delta_seen(self) -> bool:
        return any(
            message.role == MessageRole.ASSISTANT
            and bool(message.parts)
            for message in self.messages
        )

    def append(self, message: LLMMessageIR) -> "L1TurnIR":
        self._require_active()
        return replace(self, messages=(*self.messages, message), revision=self.revision + 1)

    def append_user_once(self, message: LLMMessageIR) -> "L1TurnIR":
        """Idempotently append one user message by its stable message ID."""

        self._require_active()
        if message.role != MessageRole.USER:
            raise L1TurnProtocolError("only user messages can use append_user_once")
        matches = [item for item in self.messages if item.message_id == message.message_id]
        if len(matches) > 1:
            raise L1TurnProtocolError("user message id is duplicated in L1")
        if matches:
            if matches[0] != message:
                raise L1TurnProtocolError(
                    "user message id already belongs to different L1 content"
                )
            return self
        return replace(self, messages=(*self.messages, message), revision=self.revision + 1)

    def upsert_assistant(self, message: LLMMessageIR) -> "L1TurnIR":
        self._require_active()
        if message.role != MessageRole.ASSISTANT:
            raise L1TurnProtocolError("only assistant messages can be streamed into L1")
        messages = list(self.messages)
        matches = [index for index, item in enumerate(messages) if item.message_id == message.message_id]
        if len(matches) > 1:
            raise L1TurnProtocolError("assistant message id is duplicated in L1")
        if matches:
            index = matches[0]
            if messages[index].role != MessageRole.ASSISTANT:
                raise L1TurnProtocolError("message id belongs to a non-assistant message")
            messages[index] = message
        else:
            messages.append(message)
        return replace(self, messages=tuple(messages), revision=self.revision + 1)

    def discard_assistant(self, message_id: str) -> "L1TurnIR":
        """Discard one unconsumed assistant response from an active turn.

        This is used when a provider reports that the response was truncated.
        No tool result can have consumed the response yet; rejecting a response
        after any of its tool calls were consumed would break the L1 protocol.
        """

        self._require_active()
        normalized = str(message_id or "").strip()
        if not normalized:
            raise L1TurnProtocolError("assistant message id is required")
        matches = [
            message
            for message in self.messages
            if message.role == MessageRole.ASSISTANT
            and str(message.message_id or "") == normalized
        ]
        if len(matches) != 1:
            raise L1TurnProtocolError(
                f"assistant message id must identify exactly one L1 message: {normalized}"
            )
        discarded_calls = {
            part.call_id
            for part in matches[0].parts
            if isinstance(part, ToolCallIR)
        }
        _, result_ids = _protocol_ids(self.messages)
        consumed = discarded_calls & result_ids
        if consumed:
            raise L1TurnProtocolError(
                f"cannot discard assistant response with consumed tool calls: {sorted(consumed)}"
            )
        messages = tuple(
            message
            for message in self.messages
            if not (
                message.role == MessageRole.ASSISTANT
                and str(message.message_id or "") == normalized
            )
        )
        return replace(self, messages=messages, revision=self.revision + 1)

    def append_tool_result(self, result: ToolResultIR) -> "L1TurnIR":
        self._require_active()
        if result.call_id not in self.pending_call_ids:
            raise L1TurnProtocolError(
                f"late, duplicate, or unknown tool result rejected: {result.call_id}"
            )
        return self.append(
            LLMMessageIR(
                role=MessageRole.TOOL,
                parts=(result,),
                semantic_kind="tool_result",
            )
        )

    def settle(self) -> "L1TurnIR":
        self._require_active()
        if self.pending_call_ids:
            raise L1TurnProtocolError("cannot settle L1 turn with unresolved tool calls")
        messages = _ensure_assistant_closure(
            tuple(_close_message(message) for message in self.messages)
        )
        return replace(
            self,
            messages=messages,
            state=L1TurnState.SETTLED,
            revision=self.revision + 1,
        )

    def interrupt(self, *, reason: str = "") -> "L1TurnIR":
        return self._close_incomplete(L1TurnState.INTERRUPTED, reason=reason)

    def abort(self, *, reason: str = "") -> "L1TurnIR":
        return self._close_incomplete(L1TurnState.ABORTED, reason=reason)

    def _close_incomplete(self, state: L1TurnState, *, reason: str) -> "L1TurnIR":
        self._require_active()
        pending = self.pending_call_ids
        closed: list[LLMMessageIR] = []
        for message in self.messages:
            if message.role == MessageRole.ASSISTANT:
                parts = tuple(
                    part
                    for part in message.parts
                    if not isinstance(part, ToolCallIR) or part.call_id not in pending
                )
                if not parts:
                    continue
                message = replace(
                    message,
                    parts=parts,
                    semantic_kind=(
                        "assistant_reply"
                        if not any(isinstance(part, ToolCallIR) for part in parts)
                        else message.semantic_kind
                    ),
                )
            closed.append(_close_message(message))
        metadata = thaw_json(self.metadata)
        if reason:
            metadata["settlement_reason"] = str(reason)
        return replace(
            self,
            messages=_ensure_assistant_closure(tuple(closed)),
            state=state,
            revision=self.revision + 1,
            metadata=metadata,
        )

    def _require_active(self) -> None:
        if self.state != L1TurnState.ACTIVE:
            raise L1TurnProtocolError(
                f"L1 turn {self.turn_id} is already {self.state.value}; late update rejected"
            )


@dataclass
class L1TurnStore:
    turns: list[L1TurnIR] = field(default_factory=list)

    def begin(
        self,
        turn_id: str,
        *,
        user_text: str = "",
        user_message: LLMMessageIR | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> L1TurnIR:
        normalized = str(turn_id or "").strip()
        if any(turn.turn_id == normalized for turn in self.turns):
            raise L1TurnProtocolError(f"L1 turn already exists: {normalized}")
        turn = L1TurnIR.begin(
            normalized,
            user_text=user_text,
            user_message=user_message,
            metadata=metadata,
        )
        self.turns.append(turn)
        return turn

    def get(self, turn_id: str) -> L1TurnIR | None:
        normalized = str(turn_id or "").strip()
        return next((turn for turn in reversed(self.turns) if turn.turn_id == normalized), None)

    def require_active(self, turn_id: str) -> L1TurnIR:
        turn = self.get(turn_id)
        if turn is None:
            raise L1TurnProtocolError(f"unknown L1 turn: {turn_id}")
        turn._require_active()
        return turn

    def replace(self, turn: L1TurnIR) -> None:
        for index, current in enumerate(self.turns):
            if current.turn_id == turn.turn_id:
                if turn.revision <= current.revision:
                    raise L1TurnProtocolError("L1 replacement did not advance revision")
                self.turns[index] = turn
                return
        raise L1TurnProtocolError(f"unknown L1 turn: {turn.turn_id}")

    def clear(self) -> None:
        self.turns.clear()


def _close_message(message: LLMMessageIR) -> LLMMessageIR:
    if message.role == MessageRole.ASSISTANT:
        return message.retire_reasoning()
    return replace(
        message,
        state=MessageState.COMPLETE,
        replay=None,
    )


def _ensure_assistant_closure(
    messages: tuple[LLMMessageIR, ...],
) -> tuple[LLMMessageIR, ...]:
    """Keep closed tool protocol legal for providers requiring role alternation."""

    if not messages or messages[-1].role != MessageRole.TOOL:
        return messages
    return (
        *messages,
        LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(
                TextPartIR(
                    "Tool interaction closed without a further assistant reply."
                ),
            ),
            semantic_kind="runtime_generated_closure",
            metadata={"pal_authored": True},
        ),
    )


def _protocol_ids(messages: tuple[LLMMessageIR, ...]) -> tuple[set[str], set[str]]:
    calls: set[str] = set()
    results: set[str] = set()
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallIR):
                if part.call_id in calls:
                    raise L1TurnProtocolError(f"duplicate tool call id: {part.call_id}")
                calls.add(part.call_id)
            elif isinstance(part, ToolResultIR):
                if part.call_id in results:
                    raise L1TurnProtocolError(f"duplicate tool result id: {part.call_id}")
                results.add(part.call_id)
    return calls, results


def _validate_protocol(messages: tuple[LLMMessageIR, ...], *, allow_pending: bool) -> None:
    calls, results = _protocol_ids(messages)
    unknown = results - calls
    if unknown:
        raise L1TurnProtocolError(f"orphan tool results in L1: {sorted(unknown)}")
    if not allow_pending and calls != results:
        raise L1TurnProtocolError("settled L1 tool protocol is not closed")
