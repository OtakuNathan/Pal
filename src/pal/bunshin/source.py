from __future__ import annotations

from dataclasses import dataclass

from pal.control import ControlDelivery, ControlRoute
from pal.control.runtime_contracts import ControlRuntimePort
from pal.control.interactions import delivery_for_reply, terminal_delivery_for_interaction
from pal.core.events import EventHandler, EventSource
from pal.foundation import EventEnvelope
from pal.foundation.diagnostics import exception_reason
from pal.bunshin.event_contracts import BunshinEventPort, terminal_has_user_actions, terminal_is_control_only
from pal.bunshin.interactions import bunshin_approval_delivery, bunshin_architecture_review_delivery, bunshin_question_delivery
from pal.memory.contracts import MEMORY
from pal.shared import EventKind, SourceKind


@dataclass
class BunshinEventSource(EventSource):
    provider: BunshinEventPort
    source_id: str = "bunshin.manager"
    drain_limit: int = 20

    def prepare(self, context) -> bool:
        return self.provider.has_pending_events()

    def drain(self, context) -> list[EventEnvelope]:
        payload = self.provider.drain_events_sync(limit=self.drain_limit)
        result = []
        for item in list(payload.get("events") or []):
            if not isinstance(item, dict):
                continue
            kind = _event_kind(str(item.get("event_kind") or ""))
            result.append(EventEnvelope(event_kind=kind, source_kind=SourceKind.BUNSHIN, payload=_event_payload(item)))
        return result


@dataclass
class BunshinControlEventHandler(EventHandler):
    provider: BunshinEventPort
    controls: ControlRuntimePort

    def can_handle(self, event_kind: str) -> bool:
        return event_kind in {
            EventKind.APPROVAL_REQUEST,
            EventKind.BUNSHIN_CLARIFICATION_REQUEST,
            EventKind.BUNSHIN_CLARIFICATION_RESOLVED,
            EventKind.BUNSHIN_ARCHITECTURE_REVIEW_PENDING,
            EventKind.BUNSHIN_ARCHITECTURE_REVIEW_RESOLVED,
            EventKind.BUNSHIN_TERMINAL,
            EventKind.BUNSHIN_STANDALONE_REVIEW_COMPLETED,
        }

    async def handle(self, event: EventEnvelope, context) -> list[EventEnvelope]:
        payload = dict(event.payload or {})
        worker_terminal = payload.get("event_kind") == "terminal"
        control_only = terminal_is_control_only(payload.get("event_kind"), payload)
        if control_only and not terminal_has_user_actions(payload):
            self.provider.settle_event(payload, accepted=True)
            return []
        route = _route(payload.get("route"))
        if route is None:
            self.provider.settle_event(payload, accepted=False, error="bunshin event has no delivery route")
            return []
        if event.event_kind == EventKind.APPROVAL_REQUEST:
            delivery = bunshin_approval_delivery(payload, route)
        elif event.event_kind == EventKind.BUNSHIN_CLARIFICATION_REQUEST:
            delivery = bunshin_question_delivery(payload, route)
        elif event.event_kind == EventKind.BUNSHIN_CLARIFICATION_RESOLVED:
            clarification_id = str(payload.get("clarification_id") or "").strip()
            delivery = (terminal_delivery_for_interaction(
                route, interaction_id=clarification_id, interaction_kind="bunshin_clarification",
                text=str(payload.get("summary") or "Bunshin clarification recorded.")) if clarification_id else None)
        elif event.event_kind == EventKind.BUNSHIN_ARCHITECTURE_REVIEW_PENDING:
            delivery = bunshin_architecture_review_delivery(payload, route)
        elif event.event_kind == EventKind.BUNSHIN_ARCHITECTURE_REVIEW_RESOLVED:
            revision_id = str(payload.get("architecture_revision_id") or "").strip()
            delivery = (terminal_delivery_for_interaction(
                route, interaction_id=f"bunshin_v2_architecture_{revision_id}",
                interaction_kind="bunshin_architecture_review",
                text=str(payload.get("summary") or "Bunshin architecture decision recorded.")) if revision_id else None)
        elif control_only:
            delivery = None
        else:
            text = str(payload.get("summary") or payload.get("status") or "Bunshin workflow completed.")
            delivery = delivery_for_reply(route, text)
        if delivery is None and not control_only:
            self.provider.settle_event(payload, accepted=False, error="bunshin event has no delivery")
            return []
        deliveries: list[tuple[str, ControlDelivery]] = []
        if event.event_kind == EventKind.BUNSHIN_TERMINAL:
            for index, raw in enumerate(list(payload.get("resolved_interactions") or [])):
                item = dict(raw or {})
                interaction_id = str(item.get("interaction_id") or "").strip()
                interaction_kind = str(item.get("interaction_kind") or "").strip()
                if interaction_id and interaction_kind:
                    deliveries.append((f"interaction:{index}", terminal_delivery_for_interaction(
                        route, interaction_id=interaction_id, interaction_kind=interaction_kind,
                        text=("Bunshin assignment ended; this request is closed." if worker_terminal else
                              "Bunshin workflow ended; this request is closed.") if control_only else
                        str(payload.get("summary") or payload.get("status") or "Bunshin workflow completed."))))
        if not control_only and event.event_kind in {EventKind.BUNSHIN_ARCHITECTURE_REVIEW_PENDING, EventKind.BUNSHIN_TERMINAL}:
            for index, attachment in enumerate(list(payload.get("attachments") or [])):
                item = dict(attachment or {})
                if str(item.get("path") or "").strip():
                    deliveries.append((f"attachment:{index}", ControlDelivery(
                        delivery_kind="attachment", route=route, payload=item)))
        if delivery is not None:
            deliveries.append(("primary", delivery))
        accepted = True
        error = ""
        background = not control_only and payload.get("background_handoff") and payload.get("event_kind") == "workflow_terminal"
        try:
            completed_parts = self.provider.delivered_event_parts(payload)
            if event.event_kind == EventKind.BUNSHIN_TERMINAL and payload.get("memory_candidates") and "memory_proposals" not in completed_parts:
                memory = context.port_registry.optional(MEMORY)
                if memory is None:
                    raise RuntimeError("host memory review service is unavailable")
                from pal.memory.mutations import content_hash
                proposal = {**payload, "source_kind": "bunshin", "source_label": "Bunshin",
                    "source_ref": str(payload.get("invocation_id") or payload.get("run_id") or payload.get("workflow_id")),
                    "candidate_batch_id": "bunshin:" + content_hash({
                        "invocation": payload.get("invocation_id") or payload.get("run_id"),
                        "workflow": payload.get("workflow_id"), "candidates": payload["memory_candidates"]})}
                deliveries.append(("memory_proposals", memory.stage_memory_proposal(proposal, route)))
            if background and any(part_key not in completed_parts for part_key, _ in deliveries):
                if not self.provider.begin_background_delivery(payload):
                    return []
            for part_key, part_delivery in deliveries:
                if part_key in completed_parts:
                    continue
                if not self.provider.may_send_event(payload):
                    return []
                if not await self.controls.deliver(part_delivery, require_provider=True):
                    accepted = False
                    error = "channel provider did not accept bunshin delivery"
                    break
                if not self.provider.settle_event_part(payload, part_key):
                    if background:
                        self.provider.mark_background_delivery_uncertain(payload)
                        return []
                    accepted = False
                    error = "manager did not acknowledge bunshin delivery part"
                    break
        except Exception as exc:
            if background:
                self.provider.mark_background_delivery_uncertain(payload)
                return []
            accepted = False
            error = exception_reason(exc)
        self.provider.settle_event(payload, accepted=accepted, error=error)
        return []


def _event_kind(kind: str) -> str:
    return {
        "approval_requested": EventKind.APPROVAL_REQUEST,
        "clarification_requested": EventKind.BUNSHIN_CLARIFICATION_REQUEST,
        "clarification_resolved": EventKind.BUNSHIN_CLARIFICATION_RESOLVED,
        "terminal": EventKind.BUNSHIN_TERMINAL,
        "standalone_review_completed": EventKind.BUNSHIN_STANDALONE_REVIEW_COMPLETED,
        "architecture_review_pending": EventKind.BUNSHIN_ARCHITECTURE_REVIEW_PENDING,
        "architecture_review_resolved": EventKind.BUNSHIN_ARCHITECTURE_REVIEW_RESOLVED,
        "workflow_terminal": EventKind.BUNSHIN_TERMINAL,
    }.get(kind, EventKind.BUNSHIN_PROGRESS)


def _event_payload(item: dict) -> dict:
    payload = dict(item.get("payload") or {})
    for key in ("task_id", "delivery_id", "event_kind", "bunshin_id", "run_id", "invocation_id", "workflow_id", "bunshin_profile", "created_at"):
        payload.setdefault(key, item.get(key) or "")
    # Delivery authority belongs to the outer Manager event, not role text.
    payload["event_kind"] = item.get("event_kind") or ""
    if item.get("task_id"):
        payload["task_id"] = item["task_id"]
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
