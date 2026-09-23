"""Bind provider continuation to the contribution accepted by the harness."""
from __future__ import annotations

import json
from dataclasses import replace

from pal.llm.ir import LLMMessageIR, ReplayEnvelope, WireShape
from pal.llm.response_hooks import ProviderResponseHookError
from pal.shared.json_values import thaw_json


def has_opaque_continuation(message: LLMMessageIR) -> bool:
    if message.reasoning_text:
        return True
    def contains(value):
        if isinstance(value, dict):
            return (value.get("type") in {"reasoning", "thinking", "redacted_thinking"}
                    or any(key in value for key in (
                        "encrypted_content", "reasoning", "reasoning_content", "reasoning_details"))
                    or any(contains(item) for item in value.values()))
        if isinstance(value, list):
            return any(contains(item) for item in value)
        return False
    return message.replay is not None and contains(thaw_json(message.replay.payload))


def repair_replay_calls(native: ReplayEnvelope | None, call_ids: set[str]) -> ReplayEnvelope | None:
    """Remove only revoked call records; retain reasoning and original source."""
    if native is None:
        return None
    payload = thaw_json(native.payload)
    if native.wire_shape == WireShape.OPENAI_COMPLETION:
        messages = payload.get("messages", [payload.get("message", {})])
        for message in messages:
            calls = message.get("tool_calls")
            if isinstance(calls, list):
                kept = [call for call in calls if str(call.get("id") or "") in call_ids]
                if kept:
                    message["tool_calls"] = kept
                else:
                    message.pop("tool_calls", None)
    else:
        key, kind, id_key = (("output", "function_call", "call_id")
                            if native.wire_shape == WireShape.OPENAI_RESPONSE
                            else ("content", "tool_use", "id"))
        if key in payload:
            payload[key] = [item for item in payload[key]
                            if item.get("type") != kind or str(item.get(id_key) or "") in call_ids]
    if payload == thaw_json(native.payload):
        return native
    return replace(native, payload=payload, source_payload=(
        native.source_payload if native.source_payload is not None else native.payload
    ))


def validate_native_for_send(message: LLMMessageIR) -> None:
    from pal.llm.continuation_policy import NativeCandidate, validate_candidate

    native = message.replay
    if native is None:
        return
    payload = thaw_json(native.payload)
    if native.wire_shape == WireShape.OPENAI_COMPLETION and "messages" in payload:
        payloads = [{"message": item} for item in payload["messages"]]
    else:
        payloads = [payload]
    all_ids = []
    for value in payloads:
        if native.wire_shape == WireShape.OPENAI_COMPLETION:
            ids = tuple(str(call.get("id") or "") for call in value.get("message", {}).get("tool_calls") or ())
        elif native.wire_shape == WireShape.OPENAI_RESPONSE:
            ids = tuple(str(item.get("call_id") or "") for item in value.get("output", ()) if item.get("type") == "function_call")
        else:
            ids = tuple(str(item.get("id") or "") for item in value.get("content", ()) if item.get("type") == "tool_use")
        all_ids.extend(ids)
        decision = validate_candidate(NativeCandidate(
            native.wire_shape, native.endpoint_id, native.model_id,
            json.dumps(value, ensure_ascii=False), ids,
        ))
        if not decision.ok:
            raise ProviderResponseHookError("Native continuation cannot be replayed: " + "; ".join(
                issue.detail for issue in decision.issues))
    if tuple(all_ids) != tuple(call.call_id for call in message.tool_calls):
        raise ProviderResponseHookError("native continuation does not match accepted tool inventory")


def bind_accepted_replay(
    message: LLMMessageIR, native: ReplayEnvelope | None, *, provider_id: str,
) -> LLMMessageIR:
    if native is None:
        return message
    if native.wire_shape != WireShape.OPENAI_COMPLETION:
        return replace(message, replay=native)
    raw = thaw_json(native.payload)
    wire = raw.get("message", {})
    calls = wire.get("tool_calls") or []
    identities = tuple(str(call.get("id") or "") for call in calls)
    accepted = tuple(call.call_id for call in message.tool_calls)
    if identities == accepted:
        return replace(message, replay=native)
    # Only the provider's explicit textual-tool normalization owns this
    # adaptation. Never silently bless a generic inventory mismatch.
    if provider_id.lower() != "deepseek" or calls:
        raise ProviderResponseHookError("accepted tool inventory differs from native replay")
    original = native.source_payload if native.source_payload is not None else native.payload
    wire["content"] = message.text
    wire["tool_calls"] = [
        {"id": call.call_id, "type": "function", "function": {
            "name": call.name,
            "arguments": json.dumps(thaw_json(call.arguments), ensure_ascii=False),
        }} for call in message.tool_calls
    ]
    return replace(message, replay=replace(native, payload=raw, source_payload=original))
