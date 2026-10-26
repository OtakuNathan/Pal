"""Bind provider continuation to the contribution accepted by the harness."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from pal.llm.ir import LLMMessageIR, ReplayEnvelope, WireShape
from pal.llm.continuation_policy import NativeCandidate, validate_candidate
from pal.llm.decode_contracts import ShapeDecodeError
from pal.llm.response_blocks import json_object
from pal.llm.response_hook_contracts import ProviderResponseHookError
from pal.shared.json_values import thaw_json


def has_opaque_continuation(message: LLMMessageIR) -> bool:
    if message.reasoning_text:
        return True
    def contains(value: Any) -> bool:
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
    native = message.replay
    if native is None:
        return
    payload = thaw_json(native.payload)
    if native.wire_shape == WireShape.OPENAI_COMPLETION and "messages" in payload:
        payloads = [{"message": item} for item in payload["messages"]]
    else:
        payloads = [payload]
    all_ids: list[str] = []
    all_calls: list[tuple[str, str, dict[str, Any]]] = []
    for value in payloads:
        if native.wire_shape == WireShape.OPENAI_COMPLETION:
            calls = value.get("message", {}).get("tool_calls") or ()
            ids = tuple(str(call.get("id") or "") for call in calls)
        elif native.wire_shape == WireShape.OPENAI_RESPONSE:
            calls = tuple(item for item in value.get("output", ()) if item.get("type") == "function_call")
            ids = tuple(str(item.get("call_id") or "") for item in calls)
        else:
            calls = tuple(item for item in value.get("content", ()) if item.get("type") == "tool_use")
            ids = tuple(str(item.get("id") or "") for item in calls)
        all_ids.extend(ids)
        decision = validate_candidate(NativeCandidate(
            native.wire_shape, native.endpoint_id, native.model_id,
            json.dumps(value, ensure_ascii=False), ids,
        ))
        if not decision.ok:
            raise ProviderResponseHookError("Native continuation cannot be replayed: " + "; ".join(
                issue.detail for issue in decision.issues))
        for call_id, call in zip(ids, calls):
            function = call.get("function", {}) if native.wire_shape == WireShape.OPENAI_COMPLETION else call
            arguments = function.get("input" if native.wire_shape == WireShape.ANTHROPIC_MESSAGES else "arguments")
            try:
                arguments = json_object(arguments, label="accepted replay tool arguments")
            except ShapeDecodeError as exc:
                raise ProviderResponseHookError("Native continuation tool arguments are invalid") from exc
            all_calls.append((call_id, str(function.get("name") or ""), arguments))
    if tuple(all_ids) != tuple(call.call_id for call in message.tool_calls):
        raise ProviderResponseHookError("native continuation does not match accepted tool inventory")
    accepted_calls = [(call.call_id, call.name, thaw_json(call.arguments)) for call in message.tool_calls]
    if all_calls != accepted_calls:
        raise ProviderResponseHookError("native continuation tool payload differs from accepted message")


def native_candidate_from_accepted_message(message: LLMMessageIR) -> NativeCandidate | None:
    """Derive continuation only from the accepted message's replay authority.

    Raw captures and replay.source_payload are diagnostic evidence. A merged
    Completion response carries multiple messages and uses the codec replay
    path instead of pretending to be one native response candidate.
    """
    native = message.replay
    if native is None:
        return None
    validate_native_for_send(message)
    if native.wire_shape == WireShape.OPENAI_COMPLETION and "messages" in native.payload:
        return None
    return NativeCandidate(
        native.wire_shape, native.endpoint_id, native.model_id,
        json.dumps(thaw_json(native.payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        tuple(call.call_id for call in message.tool_calls),
    )


def bind_accepted_replay(
    message: LLMMessageIR, native: ReplayEnvelope | None, *, provider_id: str,
) -> LLMMessageIR:
    if native is None:
        return message
    raw = thaw_json(native.payload)
    if native.wire_shape == WireShape.OPENAI_COMPLETION:
        wire = raw.get("message", {})
        calls = wire.get("tool_calls") or []
        identities = tuple(str(call.get("id") or "") for call in calls)
    else:
        key, kind, id_key = (("output", "function_call", "call_id")
                            if native.wire_shape == WireShape.OPENAI_RESPONSE
                            else ("content", "tool_use", "id"))
        calls = [item for item in raw.get(key, ()) if item.get("type") == kind]
        identities = tuple(str(call.get(id_key) or "") for call in calls)
    accepted = tuple(call.call_id for call in message.tool_calls)
    if identities == accepted:
        return replace(message, replay=native)
    # Only an explicit protocol-adapter receipt authorizes a changed inventory.
    # Generic replay binding has no knowledge of provider protocol grammars.
    if not message.metadata.get("protocol_tool_inventory_adapted") or calls:
        raise ProviderResponseHookError("accepted tool inventory differs from native replay")
    original = native.source_payload if native.source_payload is not None else native.payload
    if native.wire_shape == WireShape.OPENAI_COMPLETION:
        wire["content"] = message.text
        wire["tool_calls"] = [
            {"id": call.call_id, "type": "function", "function": {
                "name": call.name,
                "arguments": json.dumps(thaw_json(call.arguments), ensure_ascii=False),
            }} for call in message.tool_calls
        ]
    elif native.wire_shape == WireShape.ANTHROPIC_MESSAGES:
        raw["content"] = [item for item in raw.get("content", ()) if item.get("type") != "text"]
        if message.text:
            raw["content"].append({"type": "text", "text": message.text})
        raw["content"].extend({"type": "tool_use", "id": call.call_id, "name": call.name,
                               "input": thaw_json(call.arguments)} for call in message.tool_calls)
    else:
        raw["output"] = [item for item in raw.get("output", ()) if item.get("type") != "message"]
        if message.text:
            raw["output"].append({"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": message.text}]})
        raw["output"].extend({"type": "function_call", "call_id": call.call_id, "name": call.name,
                              "arguments": json.dumps(thaw_json(call.arguments), ensure_ascii=False)}
                             for call in message.tool_calls)
    return replace(message, replay=replace(native, payload=raw, source_payload=original))
