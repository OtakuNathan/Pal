from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pal.control.contracts import ControlAction, ControlEvent, InteractionResult
from pal.control.routing import route_from_channel_envelope
from pal.control.service import ControlPlane
from pal.core.events import EventHandler
from pal.foundation import EventEnvelope
from pal.shared import ChannelEnvelope, EventKind, SourceKind
from pal.shared.payloads import extract_text_from_payload
from pal.llm.ir import LLMMessageIR


@dataclass(frozen=True)
class ControlEventHandler(EventHandler):
    control_plane: ControlPlane

    def can_handle(self, event_kind: str) -> bool:
        return event_kind in {
            EventKind.SLASH_COMMAND,
            EventKind.INTERACTION_RESULT,
        }

    def handle(self, event: EventEnvelope, context) -> list[EventEnvelope] | None:
        _ = context
        payload_source = event.payload
        route = None
        if isinstance(payload_source, ChannelEnvelope):
            route = route_from_channel_envelope(payload_source)
            payload_source = payload_source.event.payload
        if event.event_kind == EventKind.INTERACTION_RESULT:
            if not isinstance(payload_source, InteractionResult):
                return []
            interaction_result = payload_source
            if interaction_result.route is None and route is not None:
                interaction_result = InteractionResult(
                    interaction_id=interaction_result.interaction_id,
                    interaction_kind=interaction_result.interaction_kind,
                    action_key=interaction_result.action_key,
                    action_args=dict(interaction_result.action_args),
                    route=route,
                )
            action = self.control_plane.handle_interaction(interaction_result)
            if action is None:
                return []
            return [
                EventEnvelope(
                    event_kind=EventKind.CONTROL_ACTION,
                    source_kind=SourceKind.CONTROL,
                    payload=action,
                    correlation_id=event.correlation_id or event.event_id,
                )
            ]
        if isinstance(payload_source, dict):
            payload = payload_source
        elif isinstance(payload_source, LLMMessageIR):
            control_payload = payload_source.metadata.get("control_payload")
            payload = dict(control_payload) if isinstance(control_payload, Mapping) else {}
            payload["text"] = extract_text_from_payload(payload_source)
        else:
            payload = {"text": extract_text_from_payload(payload_source)}
        control_event = ControlEvent(
            event_kind=event.event_kind,
            source_kind=event.source_kind,
            payload=payload,
            route=route,
            correlation_id=event.correlation_id,
        )
        action = self.control_plane.parse_event(control_event)
        if action is not None and action.action_kind == "fallback_user_message":
            fallback = _slash_command_fallback_user_message(event)
            if fallback is not None:
                return [fallback]
            return []
        if action is None:
            return []
        return [
            EventEnvelope(
                event_kind=EventKind.CONTROL_ACTION,
                source_kind=SourceKind.CONTROL,
                payload=action,
                correlation_id=event.correlation_id or event.event_id,
            )
        ]


def _slash_command_fallback_user_message(event: EventEnvelope) -> EventEnvelope | None:
    if event.event_kind != EventKind.SLASH_COMMAND:
        return None
    if not isinstance(event.payload, ChannelEnvelope):
        return None
    channel_envelope = event.payload
    inner = channel_envelope.event
    fallback_inner = EventEnvelope(
        event_kind=EventKind.USER_MESSAGE,
        source_kind=inner.source_kind,
        payload=inner.payload,
        correlation_id=inner.correlation_id,
        created_at=inner.created_at,
        event_id=inner.event_id,
    )
    fallback_channel = ChannelEnvelope(
        event=fallback_inner,
        endpoint=channel_envelope.endpoint,
        response_handle=channel_envelope.response_handle,
        opening_delivery_binding=channel_envelope.opening_delivery_binding,
    )
    return EventEnvelope(
        event_kind=EventKind.USER_MESSAGE,
        source_kind=event.source_kind,
        payload=fallback_channel,
        correlation_id=event.correlation_id,
        created_at=event.created_at,
        event_id=event.event_id,
    )
