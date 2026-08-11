from __future__ import annotations

from dataclasses import dataclass

from pal.control import ControlAction, ControlDelivery, ControlRoute
from pal.control.interactions import delivery_for_reply, terminal_delivery_for_interaction
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
            EventKind.MINION_CLARIFICATION_RESOLVED,
            EventKind.MINION_ARCHITECTURE_REVIEW_PENDING,
            EventKind.MINION_ARCHITECTURE_REVIEW_RESOLVED,
            EventKind.MINION_TERMINAL,
            EventKind.MINION_STANDALONE_REVIEW_COMPLETED,
        }

    async def handle(self, event: EventEnvelope, context) -> list[EventEnvelope]:
        payload = dict(event.payload or {})
        route = _route(payload.get("route"))
        if route is None:
            if self.provider is not None:
                settle = getattr(self.provider, "settle_event", None)
                if callable(settle):
                    settle(payload, accepted=False, error="minion event has no delivery route")
            return []
        if event.event_kind == EventKind.APPROVAL_REQUEST:
            delivery = minion_approval_delivery(payload, route)
            action_kind = "interactive_open"
        elif event.event_kind == EventKind.MINION_CLARIFICATION_REQUEST:
            delivery = minion_question_delivery(payload, route)
            action_kind = "interactive_open"
        elif event.event_kind == EventKind.MINION_CLARIFICATION_RESOLVED:
            clarification_id = str(payload.get("clarification_id") or "").strip()
            delivery = (
                terminal_delivery_for_interaction(
                    route,
                    interaction_id=clarification_id,
                    interaction_kind="minion_clarification",
                    text=str(payload.get("summary") or "Minion clarification recorded."),
                )
                if clarification_id
                else None
            )
            action_kind = "interactive_resolve"
        elif event.event_kind == EventKind.MINION_ARCHITECTURE_REVIEW_PENDING:
            delivery = minion_architecture_review_delivery(payload, route)
            action_kind = "interactive_open"
        elif event.event_kind == EventKind.MINION_ARCHITECTURE_REVIEW_RESOLVED:
            revision_id = str(payload.get("architecture_revision_id") or "").strip()
            delivery = (
                terminal_delivery_for_interaction(
                    route,
                    interaction_id=f"minion_v2_architecture_{revision_id}",
                    interaction_kind="minion_v2_architecture_review",
                    text=str(
                        payload.get("summary")
                        or "Minion architecture decision recorded."
                    ),
                )
                if revision_id
                else None
            )
            action_kind = "interactive_resolve"
        else:
            text = str(payload.get("summary") or payload.get("status") or "Minion workflow completed.")
            delivery = delivery_for_reply(route, text)
            action_kind = "route_reply"
        if delivery is None:
            if self.provider is not None:
                settle = getattr(self.provider, "settle_event", None)
                if callable(settle):
                    settle(payload, accepted=False, error="minion event has no delivery")
            return []
        deliveries: list[tuple[str, ControlAction]] = []
        if event.event_kind == EventKind.MINION_TERMINAL:
            for index, raw in enumerate(list(payload.get("resolved_interactions") or [])):
                item = dict(raw or {})
                interaction_id = str(item.get("interaction_id") or "").strip()
                interaction_kind = str(item.get("interaction_kind") or "").strip()
                if not interaction_id or not interaction_kind:
                    continue
                deliveries.append(
                    (
                        f"interaction:{index}",
                        ControlAction(
                            action_kind="interactive_resolve",
                            target_scope="interaction",
                            target_id=interaction_id,
                            route=route,
                            delivery=terminal_delivery_for_interaction(
                                route,
                                interaction_id=interaction_id,
                                interaction_kind=interaction_kind,
                                text=str(
                                    payload.get("summary")
                                    or payload.get("status")
                                    or "Minion workflow completed."
                                ),
                            ),
                            notes="minion terminal interaction cleanup",
                        ),
                    )
                )
        if event.event_kind == EventKind.MINION_ARCHITECTURE_REVIEW_PENDING:
            for index, attachment in enumerate(list(payload.get("attachments") or [])):
                item = dict(attachment or {})
                if not str(item.get("path") or "").strip():
                    continue
                deliveries.append(
                    (
                        f"attachment:{index}",
                        ControlAction(
                            action_kind="route_attachment",
                            target_scope="channel",
                            target_id=str(payload.get("workflow_id") or ""),
                            route=route,
                            delivery=ControlDelivery(
                                delivery_kind="attachment",
                                route=route,
                                payload=item,
                            ),
                            notes="minion V2 human review attachment",
                        ),
                    )
                )
        deliveries.append(
            (
                "primary",
                ControlAction(
                    action_kind=action_kind,
                    target_scope="interaction" if action_kind.startswith("interactive_") else "channel",
                    target_id=str(payload.get("workflow_id") or payload.get("run_id") or ""),
                    route=route,
                    delivery=delivery,
                    notes="minion V2 event",
                ),
            )
        )
        core = context.port_registry.get("core:core")
        accepted = core is not None
        error = "core control port is unavailable"
        if accepted:
            try:
                completed_parts: set[str] = set()
                if self.provider is not None:
                    read_parts = getattr(self.provider, "delivered_event_parts", None)
                    if callable(read_parts):
                        completed_parts = set(read_parts(payload))
                for part_key, action in deliveries:
                    if part_key in completed_parts:
                        continue
                    accepted = bool(
                        await core.handle_control_action_async(
                            action,
                            require_provider=True,
                        )
                    ) and accepted
                    if not accepted:
                        error = "channel provider did not accept minion delivery"
                        break
                    if self.provider is not None:
                        settle_part = getattr(self.provider, "settle_event_part", None)
                        if callable(settle_part) and not bool(
                            settle_part(payload, part_key)
                        ):
                            accepted = False
                            error = "manager did not acknowledge minion delivery part"
                            break
                else:
                    error = ""
            except Exception as exc:
                accepted = False
                error = f"{exc.__class__.__name__}: {exc}"
        if self.provider is not None:
            settle = getattr(self.provider, "settle_event", None)
            if callable(settle):
                settle(payload, accepted=accepted, error=error)
        return []


def _event_kind(kind: str) -> str:
    return {
        "approval_requested": EventKind.APPROVAL_REQUEST,
        "clarification_requested": EventKind.MINION_CLARIFICATION_REQUEST,
        "clarification_resolved": EventKind.MINION_CLARIFICATION_RESOLVED,
        "terminal": EventKind.MINION_TERMINAL,
        "standalone_review_completed": EventKind.MINION_STANDALONE_REVIEW_COMPLETED,
        "architecture_review_pending": EventKind.MINION_ARCHITECTURE_REVIEW_PENDING,
        "architecture_review_resolved": EventKind.MINION_ARCHITECTURE_REVIEW_RESOLVED,
        "workflow_terminal": EventKind.MINION_TERMINAL,
    }.get(kind, EventKind.MINION_PROGRESS)


def _event_payload(item: dict) -> dict:
    payload = dict(item.get("payload") or {})
    for key in ("delivery_id", "event_kind", "minion_id", "run_id", "invocation_id", "workflow_id", "minion_profile", "created_at"):
        payload.setdefault(key, item.get(key) or "")
    route = payload.get("route")
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
