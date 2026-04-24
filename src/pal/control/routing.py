from __future__ import annotations

from typing import Any

from pal.channel.contracts import ChannelEnvelope
from pal.control.contracts import ControlRoute


def derive_control_scope_key(
    *,
    endpoint_id: str,
    channel_kind: str,
    reply_target: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    target = dict(reply_target or {})
    data = dict(payload or {})
    if channel_kind == "telegram":
        chat_id = str(target.get("chat_id") or data.get("chat_id") or "").strip() or "unknown"
        thread_id = str(target.get("thread_id") or data.get("thread_id") or "").strip() or "root"
        return f"tg:{endpoint_id}:{chat_id}:{thread_id}"
    if channel_kind == "socket":
        session_id = str(target.get("session_id") or data.get("session_id") or "").strip() or "default"
        return f"socket:{endpoint_id}:{session_id}"
    if channel_kind == "stdio":
        return f"stdio:{endpoint_id}"
    return f"{channel_kind}:{endpoint_id}"


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
