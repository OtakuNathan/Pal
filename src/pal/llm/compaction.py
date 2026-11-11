"""Opaque continuation contracts. These windows are history, never tool effects."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from pal.llm.ir import LLMMessageIR, ReplayEnvelope, WireShape
from pal.shared.json_values import thaw_json

NATIVE_WINDOW_KEY = "_pal_compaction"


def is_native_window(message: LLMMessageIR) -> bool:
    return message.replay is not None and NATIVE_WINDOW_KEY in message.replay.payload


def native_origin(endpoint: Any) -> str:
    identity = [endpoint.model_id, endpoint.wire_shape, endpoint.base_url.rstrip("/"),
                endpoint.auth_kind, endpoint.credential_ref,
                endpoint.capabilities_blob.get("access_profile", "")]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def validate_native_window(replay: ReplayEnvelope) -> None:
    marker = replay.payload.get(NATIVE_WINDOW_KEY)
    output = replay.payload.get("output")
    if (replay.wire_shape != WireShape.OPENAI_RESPONSE or not isinstance(marker, Mapping)
            or marker.get("version") != 1 or not marker.get("origin")
            or not isinstance(output, (list, tuple)) or not output
            or any(not isinstance(item, Mapping) for item in output)
            or not any(item.get("type") == "compaction" and item.get("encrypted_content") for item in output)):
        raise ValueError("invalid native compaction window")


def token_count_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    # /responses/input_tokens has its own schema; generation controls and
    # cache hints are not input-token parameters.
    fields = {"model", "input", "instructions", "tools", "tool_choice", "text",
              "reasoning", "parallel_tool_calls", "truncation"}
    return {key: thaw_json(value) for key, value in payload.items() if key in fields}
