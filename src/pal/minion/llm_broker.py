from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR

import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pal.llm.contracts import (
    LLMGenerationResult,
    LLMPreflightAdvice,
    LLMPreflightRequest,
)
from pal.llm.ir import (
    LLMFinishReason,
    LLMMessageIR,
    LLMRequestIR,
    LLMResponseDeltaKind,
    LLMResponseItemKind,
    LLMResponseIR,
    LLMResponseUpdate,
    MessageRole,
    MessageState,
    ReasoningPartIR,
    TextPartIR,
)
from pal.llm.serde import (
    generation_result_from_payload,
    generation_result_to_payload,
    preflight_request_from_payload,
    preflight_request_to_payload,
    part_from_payload,
    part_to_payload,
    request_from_payload as llm_request_from_payload,
    request_to_payload as llm_request_to_payload,
    response_from_payload,
    response_to_payload,
)
from pal.minion.ipc import (
    ROLE_GATEWAY_TOKEN_ENV,
    MinionManagerClient,
    MinionRoleGatewayClient,
)


def preflight_advice_to_payload(advice: LLMPreflightAdvice) -> dict[str, Any]:
    return {
        "status": advice.status,
        "active_model": advice.active_model,
        "fallback_chain": list(advice.fallback_chain or []),
        "target_input_budget": int(advice.target_input_budget or 0),
        "reserved_output_tokens": int(advice.reserved_output_tokens or 0),
        "breakdown": dict(advice.breakdown or {}),
    }


def preflight_advice_from_payload(payload: dict[str, Any]) -> LLMPreflightAdvice:
    return LLMPreflightAdvice(
        status=str(payload.get("status") or "ready"),
        active_model=str(payload.get("active_model") or "") or None,
        fallback_chain=[str(item) for item in list(payload.get("fallback_chain") or []) if str(item or "").strip()],
        target_input_budget=int(payload.get("target_input_budget") or 0),
        reserved_output_tokens=int(payload.get("reserved_output_tokens") or 0),
        breakdown=dict(payload.get("breakdown") or {}),
    )


llm_outcome_to_payload = generation_result_to_payload
llm_outcome_from_payload = generation_result_from_payload


def stream_update_to_payload(update: LLMResponseUpdate) -> dict[str, Any]:
    """Encode a broker stream event without copying the accumulated prefix.

    Delta frames carry only their new semantic part. The terminal state frame
    carries the one authoritative complete response.
    """

    payload: dict[str, Any] = {
        "delta_kind": update.delta_kind.value,
        "message_id": update.response.message.message_id,
    }
    if update.delta_kind == LLMResponseDeltaKind.STATE:
        payload["response"] = response_to_payload(update.response)
        return payload
    if update.delta_kind in {
        LLMResponseDeltaKind.TEXT,
        LLMResponseDeltaKind.REASONING,
    }:
        payload["text_delta"] = update.text_delta
    if update.delta_kind == LLMResponseDeltaKind.REASONING:
        payload["redacted"] = _reasoning_delta_is_redacted(update)
    if update.delta_kind == LLMResponseDeltaKind.TOOL_CALL:
        if update.tool_call is None:
            raise ValueError("tool-call stream event has no tool call")
        payload["tool_call"] = part_to_payload(update.tool_call)
    if update.delta_kind == LLMResponseDeltaKind.ITEM_COMMITTED:
        payload["item_id"] = update.item_id
        payload["item_kind"] = (
            update.item_kind.value if update.item_kind is not None else ""
        )
        if update.tool_call is not None:
            payload["tool_call"] = part_to_payload(update.tool_call)
    return payload


class BrokerStreamDecoder:
    """Rebuild body-equivalent response snapshots from delta-only IPC frames."""

    def __init__(self) -> None:
        self.message_id = ""
        self.parts: list[Any] = []
        self.committed_items: dict[str, LLMResponseItemKind] = {}
        self.committed_tool_calls: dict[str, ToolCallIR] = {}
        self.terminal_seen = False

    def feed(self, payload: dict[str, Any]) -> LLMResponseUpdate:
        if self.terminal_seen:
            raise ValueError("broker stream emitted data after its terminal response")
        kind = LLMResponseDeltaKind(str(payload.get("delta_kind") or ""))
        message_id = str(payload.get("message_id") or "").strip()
        if not message_id:
            raise ValueError("broker stream event has no message_id")
        if self.message_id and message_id != self.message_id:
            raise ValueError("broker stream changed message_id")

        if kind == LLMResponseDeltaKind.STATE:
            response_payload = payload.get("response")
            if not isinstance(response_payload, dict):
                raise ValueError("terminal broker stream event has no response")
            response = response_from_payload(response_payload)
            if response.message.message_id != message_id:
                raise ValueError("terminal broker response changed message_id")
            if response.finish_reason not in {
                LLMFinishReason.ERROR,
                LLMFinishReason.CONTENT_FILTER,
            }:
                self._validate_committed_items(response)
            self.message_id = message_id
            self.terminal_seen = True
            return LLMResponseUpdate(response, delta_kind=kind)

        text_delta = str(payload.get("text_delta") or "")
        tool_call: ToolCallIR | None = None
        item_id = ""
        item_kind: LLMResponseItemKind | None = None
        if kind == LLMResponseDeltaKind.TEXT:
            if not text_delta:
                raise ValueError("text broker stream event has an empty delta")
            self._append_text(text_delta)
        elif kind == LLMResponseDeltaKind.REASONING:
            redacted = bool(payload.get("redacted"))
            if not text_delta and not redacted:
                raise ValueError("reasoning broker stream event has an empty delta")
            self._append_reasoning(text_delta, redacted=redacted)
        elif kind == LLMResponseDeltaKind.TOOL_CALL:
            call_payload = payload.get("tool_call")
            if not isinstance(call_payload, dict):
                raise ValueError("tool-call broker stream event has no tool call")
            parsed = part_from_payload(call_payload)
            if not isinstance(parsed, ToolCallIR):
                raise ValueError("broker stream tool_call is not a tool call")
            tool_call = parsed
            self.parts.append(tool_call)
        elif kind == LLMResponseDeltaKind.ITEM_COMMITTED:
            item_id = str(payload.get("item_id") or "").strip()
            if not item_id:
                raise ValueError("committed broker item has no item_id")
            raw_item_kind = str(payload.get("item_kind") or "").strip()
            if not raw_item_kind:
                raise ValueError("committed broker item has no item_kind")
            item_kind = LLMResponseItemKind(raw_item_kind)
            previous = self.committed_items.get(item_id)
            if previous is not None and previous != item_kind:
                raise ValueError("committed broker item changed kind")
            call_payload = payload.get("tool_call")
            if item_kind == LLMResponseItemKind.TOOL_CALL:
                if not isinstance(call_payload, dict):
                    raise ValueError("committed broker tool item has no tool call")
                parsed = part_from_payload(call_payload)
                if not isinstance(parsed, ToolCallIR):
                    raise ValueError("committed broker item tool_call is not a tool call")
                tool_call = parsed
                previous_call = self.committed_tool_calls.get(item_id)
                if previous_call is not None and previous_call != tool_call:
                    raise ValueError("committed broker tool item changed value")
                self.committed_items[item_id] = item_kind
                self.committed_tool_calls[item_id] = tool_call
                if not any(
                    isinstance(part, ToolCallIR)
                    and part.call_id == tool_call.call_id
                    for part in self.parts
                ):
                    self.parts.append(tool_call)
            else:
                self.committed_items[item_id] = item_kind

        response = LLMResponseIR(
            message=LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=tuple(self.parts),
                message_id=message_id,
                state=MessageState.IN_PROGRESS,
                metadata=(
                    {
                        "committed_items": [
                            {
                                "item_id": committed_id,
                                "item_kind": committed_kind.value,
                            }
                            for committed_id, committed_kind in self.committed_items.items()
                        ]
                    }
                    if self.committed_items
                    else {}
                ),
            ),
            finish_reason=(
                LLMFinishReason.TOOL_CALLS
                if any(isinstance(part, ToolCallIR) for part in self.parts)
                else LLMFinishReason.STOP
            ),
        )
        update = LLMResponseUpdate(
            response,
            delta_kind=kind,
            text_delta=text_delta,
            tool_call=tool_call,
            item_id=item_id,
            item_kind=item_kind,
        )
        self.message_id = message_id
        return update

    def _validate_committed_items(self, response: LLMResponseIR) -> None:
        raw_items = response.message.metadata.get("committed_items", ())
        if not isinstance(raw_items, (list, tuple)):
            raise ValueError("terminal broker committed_items is not a sequence")
        terminal_items: dict[str, LLMResponseItemKind] = {}
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                raise ValueError("terminal broker committed item is not an object")
            item_id = str(raw_item.get("item_id") or "").strip()
            raw_kind = str(raw_item.get("item_kind") or "").strip()
            if not item_id or not raw_kind:
                raise ValueError("terminal broker committed item is incomplete")
            item_kind = LLMResponseItemKind(raw_kind)
            previous = terminal_items.get(item_id)
            if previous is not None and previous != item_kind:
                raise ValueError("terminal broker committed item changed kind")
            terminal_items[item_id] = item_kind
        for item_id, item_kind in self.committed_items.items():
            if terminal_items.get(item_id) != item_kind:
                raise ValueError("terminal broker response revoked a committed item")
        terminal_calls = {call.call_id: call for call in response.tool_calls}
        for committed_call in self.committed_tool_calls.values():
            if terminal_calls.get(committed_call.call_id) != committed_call:
                raise ValueError("terminal broker response revoked a committed tool call")

    def _append_text(self, value: str) -> None:
        if self.parts and isinstance(self.parts[-1], TextPartIR):
            self.parts[-1] = TextPartIR(self.parts[-1].text + value)
        else:
            self.parts.append(TextPartIR(value))

    def _append_reasoning(self, value: str, *, redacted: bool) -> None:
        if (
            self.parts
            and isinstance(self.parts[-1], ReasoningPartIR)
            and self.parts[-1].redacted == redacted
        ):
            previous = self.parts[-1]
            self.parts[-1] = ReasoningPartIR(previous.text + value, redacted=redacted)
        else:
            self.parts.append(ReasoningPartIR(value, redacted=redacted))


def _reasoning_delta_is_redacted(update: LLMResponseUpdate) -> bool:
    return bool(
        update.response.message.parts
        and isinstance(update.response.message.parts[-1], ReasoningPartIR)
        and update.response.message.parts[-1].redacted
    )


@dataclass
class MinionBrokerLLMRuntime:
    runtime_root: Path
    run_id: str
    request_timeout_seconds: float = 3900.0
    supports_streaming: bool = True

    @property
    def _client(self) -> MinionManagerClient | MinionRoleGatewayClient:
        access_token = str(os.environ.get(ROLE_GATEWAY_TOKEN_ENV) or "").strip()
        if access_token:
            return MinionRoleGatewayClient(
                runtime_root=Path(self.runtime_root),
                access_token=access_token,
                request_timeout_seconds=self.request_timeout_seconds,
            )
        if os.environ.get("PAL_MINION_SANDBOXED") == "1":
            raise RuntimeError(
                "sandboxed minion has no assignment-scoped role gateway token"
            )
        return MinionManagerClient(runtime_root=Path(self.runtime_root), request_timeout_seconds=self.request_timeout_seconds)

    def resolve_max_output_tokens(
        self,
        *,
        preferred_endpoint_id: str | None = None,
        preferred_endpoint_source: str | None = None,
    ) -> int | None:
        result = self._client.request_sync(
            "llm_resolve_max_output_tokens",
            {
                "run_id": self.run_id,
                "preferred_endpoint_id": preferred_endpoint_id or "",
                "preferred_endpoint_source": preferred_endpoint_source or "",
            },
        )
        value = result.get("max_output_tokens")
        return int(value) if value is not None else None

    def resolve_endpoint_facts(self, *args, **kwargs) -> dict[str, Any]:
        preferred_endpoint_id = str(kwargs.get("preferred_endpoint_id") or "").strip()
        preferred_endpoint_source = str(kwargs.get("preferred_endpoint_source") or "").strip()
        if args and not preferred_endpoint_id:
            preferred_endpoint_id = str(args[0] or "").strip()
        return self._client.request_sync(
            "llm_resolve_endpoint_facts",
            {
                "run_id": self.run_id,
                "preferred_endpoint_id": preferred_endpoint_id,
                "preferred_endpoint_source": preferred_endpoint_source,
            },
        )

    def preflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        result = self._client.request_sync(
            "llm_preflight",
            {"run_id": self.run_id, "request": preflight_request_to_payload(request)},
        )
        return preflight_advice_from_payload(dict(result.get("advice") or {}))

    async def apreflight(self, request: LLMPreflightRequest) -> LLMPreflightAdvice:
        result = await self._client.request(
            "llm_preflight",
            {"run_id": self.run_id, "request": preflight_request_to_payload(request)},
        )
        return preflight_advice_from_payload(dict(result.get("advice") or {}))

    def generate(self, request: LLMRequestIR) -> LLMGenerationResult:
        result = self._client.request_sync(
            "llm_generate",
            {"run_id": self.run_id, "request": llm_request_to_payload(request)},
        )
        return llm_outcome_from_payload(dict(result.get("outcome") or {}))

    async def agenerate(self, request: LLMRequestIR) -> LLMGenerationResult:
        result = await self._client.request(
            "llm_generate",
            {"run_id": self.run_id, "request": llm_request_to_payload(request)},
        )
        return llm_outcome_from_payload(dict(result.get("outcome") or {}))

    async def astream(self, request: LLMRequestIR) -> AsyncIterator[LLMResponseUpdate]:
        decoder = BrokerStreamDecoder()
        async for item in self._client.stream(
            "llm_generate_stream",
            {"run_id": self.run_id, "request": llm_request_to_payload(request)},
        ):
            update = item.get("update")
            if not isinstance(update, dict):
                raise ValueError("LLM broker stream frame has no update")
            yield decoder.feed(update)
        if not decoder.terminal_seen:
            raise RuntimeError("LLM broker stream ended without a terminal response")
