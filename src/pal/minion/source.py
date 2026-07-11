from __future__ import annotations

from dataclasses import dataclass

from pal.control import ControlAction, ControlRoute
from pal.control.interactions import delivery_for_reply
from pal.core.events import EventHandler, EventSource
from pal.foundation import EventEnvelope
from pal.minion.interactions import minion_approval_delivery, minion_architecture_review_delivery, minion_question_delivery
from pal.shared import EventKind, SourceKind


@dataclass
class MinionEventSource(EventSource):
    provider: object
    source_id: str = "minion.manager"
    drain_limit: int = 20

    def prepare(self, context) -> bool:
        _ = context
        return bool(getattr(self.provider, "has_pending_events", lambda: False)())

    def drain(self, context) -> list[EventEnvelope]:
        _ = context
        payload = getattr(self.provider, "drain_events_sync", lambda **_kwargs: {"events": []})(limit=self.drain_limit)
        result = []
        for item in list(payload.get("events") or []):
            if not isinstance(item, dict):
                continue
            kind = _event_kind(str(item.get("event_kind") or ""))
            result.append(EventEnvelope(event_kind=kind, source_kind=SourceKind.MINION, payload=_event_payload(item)))
        return result


@dataclass
class MinionControlEventHandler(EventHandler):
    provider: object | None = None

    def can_handle(self, event_kind: str) -> bool:
        return event_kind in {
            EventKind.APPROVAL_REQUEST,
            EventKind.MINION_CLARIFICATION_REQUEST,
            EventKind.MINION_ARCHITECTURE_REVIEW_PENDING,
            EventKind.MINION_TERMINAL,
            EventKind.MINION_STANDALONE_REVIEW_COMPLETED,
        }

    def handle(self, event: EventEnvelope, context) -> list[EventEnvelope]:
        _ = context
        payload = dict(event.payload or {})
        route = _route(payload.get("route"))
        if route is None:
            return []
        if event.event_kind == EventKind.APPROVAL_REQUEST:
            delivery = minion_approval_delivery(payload, route)
            action_kind = "interactive_open"
        elif event.event_kind == EventKind.MINION_CLARIFICATION_REQUEST:
            delivery = minion_question_delivery(payload, route)
            action_kind = "interactive_open"
        elif event.event_kind == EventKind.MINION_ARCHITECTURE_REVIEW_PENDING:
            delivery = minion_architecture_review_delivery(payload, route)
            action_kind = "interactive_open"
        else:
            text = str(payload.get("summary") or payload.get("status") or "Minion workflow completed.")
            delivery = delivery_for_reply(route, text)
            action_kind = "route_reply"
        if delivery is None:
            return []
        return [
            EventEnvelope(
                event_kind=EventKind.CONTROL_ACTION,
                source_kind=SourceKind.CONTROL,
                payload=ControlAction(
                    action_kind=action_kind,
                    target_scope="interaction" if action_kind == "interactive_open" else "channel",
                    target_id=str(payload.get("workflow_id") or payload.get("run_id") or ""),
                    route=route,
                    delivery=delivery,
                    notes="minion V2 event",
                ),
                correlation_id=event.correlation_id,
            )
        ]


def _event_kind(kind: str) -> str:
    return {
        "approval_requested": EventKind.APPROVAL_REQUEST,
        "clarification_requested": EventKind.MINION_CLARIFICATION_REQUEST,
        "terminal": EventKind.MINION_TERMINAL,
        "standalone_review_completed": EventKind.MINION_STANDALONE_REVIEW_COMPLETED,
        "architecture_review_pending": EventKind.MINION_ARCHITECTURE_REVIEW_PENDING,
    }.get(kind, EventKind.MINION_PROGRESS)


def _event_payload(item: dict) -> dict:
    payload = dict(item.get("payload") or {})
    for key in ("event_kind", "minion_id", "run_id", "invocation_id", "workflow_id", "minion_profile", "created_at"):
        payload.setdefault(key, item.get(key) or "")
    route = payload.get("route")
    if not isinstance(route, dict):
        route = dict(payload.get("control_route") or dict(payload.get("metadata") or {}).get("control_route") or {})
    if route:
        payload["route"] = route
    return payload


def _route(value) -> ControlRoute | None:
    if isinstance(value, ControlRoute):
        return value
    if not isinstance(value, dict) or not value:
        return None
    try:
        return ControlRoute(
            endpoint_id=str(value.get("endpoint_id") or ""),
            channel_kind=str(value.get("channel_kind") or ""),
            reply_target=dict(value.get("reply_target") or {}),
            control_scope_key=str(value.get("control_scope_key") or ""),
            correlation_id=str(value.get("correlation_id") or "") or None,
        )
    except Exception:
        return None
