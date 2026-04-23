from __future__ import annotations

from dataclasses import dataclass

from pal.channel.contracts import ChannelEnvelope
from pal.control.contracts import ControlAction, ControlEvent, InteractionResult
from pal.control.routing import route_from_channel_envelope
from pal.control.service import ControlPlane
from pal.core.events import EventHandler
from pal.foundation import EventEnvelope
from pal.shared import EventKind, SourceKind


@dataclass(frozen=True)
class ControlEventHandler(EventHandler):
    control_plane: ControlPlane

    def can_handle(self, event_kind: str) -> bool:
        return event_kind in {EventKind.SLASH_COMMAND, EventKind.INTERACTION_RESULT, EventKind.APPROVAL_REQUEST}

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
        payload = payload_source if isinstance(payload_source, dict) else {}
        control_event = ControlEvent(
            event_kind=event.event_kind,
            source_kind=event.source_kind,
            payload=payload,
            route=route,
            correlation_id=event.correlation_id,
        )
        action = self.control_plane.parse_event(control_event)
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
