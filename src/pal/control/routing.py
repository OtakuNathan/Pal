from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pal.control.contracts import ControlRoute
from pal.shared import ChannelEnvelope


_EXPLICIT_SCOPE_KEYS = ("control_scope_key", "scope_key")
_IDENTITY_KEYS = (
    "conversation_id",
    "session_id",
    "room_id",
    "channel_id",
    "user_id",
    "account_id",
)
_TRANSIENT_TARGET_KEYS = {
    "request_id",
    "message_id",
    "callback_id",
    "interaction_id",
    "event_id",
}


def derive_control_scope_key(
    *,
    endpoint_id: str,
    channel_kind: str,
    reply_target: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    _ = channel_kind
    target = dict(reply_target or {})
    data = dict(payload or {})
    explicit = _first_scope_value(target)
    if explicit:
        return explicit
    identity = _identity_scope_part(target, data)
    return f"channel:{_scope_piece(endpoint_id) or 'unknown_endpoint'}:{identity}"


def _first_scope_value(*mappings: dict[str, Any]) -> str:
    for mapping in mappings:
        for key in _EXPLICIT_SCOPE_KEYS:
            value = _scope_piece(mapping.get(key))
            if value:
                return value
    return ""


def _identity_scope_part(*mappings: dict[str, Any]) -> str:
    merged: dict[str, Any] = {}
    for mapping in mappings:
        merged.update(mapping)
    parts = []
    for key in _IDENTITY_KEYS:
        value = _scope_piece(merged.get(key))
        if value:
            parts.append(f"{key}={value}")
    if parts:
        return ",".join(parts)
    stable_target = {
        str(key): value
        for key, value in merged.items()
        if str(key) not in _TRANSIENT_TARGET_KEYS and isinstance(value, (str, int, float, bool))
    }
    if stable_target:
        raw = json.dumps(stable_target, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"target={digest}"
    return "default"


def _scope_piece(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"\s+", "_", text)[:160]


def route_from_channel_envelope(envelope: ChannelEnvelope) -> ControlRoute:
    payload = envelope.event.payload if isinstance(envelope.event.payload, dict) else {}
    return ControlRoute(
        endpoint_id=envelope.endpoint.endpoint_id,
        channel_kind=envelope.endpoint.channel_kind,
        reply_target=dict(envelope.response_handle.reply_target),
        control_scope_key=derive_control_scope_key(
            endpoint_id=envelope.endpoint.endpoint_id,
            channel_kind=envelope.endpoint.channel_kind,
            reply_target=envelope.response_handle.reply_target,
            payload=payload,
        ),
        correlation_id=envelope.event.correlation_id or envelope.event.event_id,
    )
