from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.control import ControlAction, ControlRoute
from pal.control.interactions import (
    delivery_for_reply,
    minion_approval_delivery,
    minion_lesson_approval_delivery,
    minion_module_continue_delivery,
    minion_plan_acceptance_delivery,
    minion_question_delivery,
)
from pal.core.events import EventHandler, EventSource
from pal.foundation import EventEnvelope
from pal.shared import EventKind, SourceKind


@dataclass
class MinionEventSource(EventSource):
    provider: object
    source_id: str = "minion.manager"
    drain_limit: int = 20

    def prepare(self, context) -> bool:
        _ = context
        has_pending = getattr(self.provider, "has_pending_events", None)
        return bool(has_pending()) if callable(has_pending) else False

    def drain(self, context) -> list[EventEnvelope]:
        _ = context
        drain = getattr(self.provider, "drain_events_sync", None)
        if not callable(drain):
            return []
        payload = drain(limit=self.drain_limit)
        events = []
        for item in list(payload.get("events") or []):
            if not isinstance(item, dict):
                continue
            event_kind = _event_kind_for_minion_event(str(item.get("event_kind") or ""))
            events.append(
                EventEnvelope(
                    event_kind=event_kind,
                    source_kind=SourceKind.MINION,
                    payload=_payload_for_event(event_kind, item),
                )
            )
        return events


@dataclass
class MinionControlEventHandler(EventHandler):
    provider: object | None = None

    def can_handle(self, event_kind: str) -> bool:
        return event_kind in {
            EventKind.APPROVAL_REQUEST,
            EventKind.MINION_PROGRESS,
            EventKind.MINION_CHECKPOINT,
            EventKind.MINION_TERMINAL,
            EventKind.MINION_MODULE_COMPLETED,
            EventKind.MINION_WORK_ORDER_COMPLETED,
            EventKind.MINION_CLARIFICATION_REQUEST,
            EventKind.MINION_PLAN_ACCEPTANCE_PENDING,
        }

    def handle(self, event: EventEnvelope, context) -> list[EventEnvelope]:
        payload = dict(event.payload or {}) if isinstance(event.payload, dict) else {}
        if event.event_kind in {EventKind.MINION_TERMINAL, EventKind.MINION_MODULE_COMPLETED, EventKind.MINION_WORK_ORDER_COMPLETED}:
            _record_minion_observation(self.provider, context, payload)
        route = _route_from_payload(payload.get("route"))
        if route is None:
            return []
        if event.event_kind == EventKind.APPROVAL_REQUEST:
            delivery = minion_approval_delivery(payload, route)
            action = ControlAction(
                action_kind="interactive_open",
                target_scope="interaction",
                target_id=delivery.interaction.interaction_id if delivery.interaction is not None else None,
                route=route,
                delivery=delivery,
                notes="minion approval request",
            )
        elif event.event_kind == EventKind.MINION_CLARIFICATION_REQUEST:
            delivery = minion_question_delivery(payload, route)
            if delivery is None:
                return []
            action = ControlAction(
                action_kind="interactive_open",
                target_scope="interaction",
                target_id=delivery.interaction.interaction_id if delivery.interaction is not None else None,
                route=route,
                delivery=delivery,
                notes="minion clarification request",
            )
        elif event.event_kind == EventKind.MINION_PLAN_ACCEPTANCE_PENDING:
            delivery = minion_plan_acceptance_delivery(payload, route)
            if delivery is None:
                return []
            action = ControlAction(
                action_kind="interactive_open",
                target_scope="interaction",
                target_id=delivery.interaction.interaction_id if delivery.interaction is not None else None,
                route=route,
                delivery=delivery,
                notes="minion plan acceptance pending",
            )
        elif event.event_kind == EventKind.MINION_PROGRESS:
            # Progress is high-cardinality telemetry for the manager ledger, not a chat notification.
            return []
        elif event.event_kind == EventKind.MINION_CHECKPOINT:
            if not _should_notify_checkpoint(payload):
                return []
            text = _render_minion_event_notification(event.event_kind, payload)
            if not text:
                return []
            action = ControlAction(
                action_kind="route_reply",
                target_scope="channel",
                target_id=str(payload.get("run_id") or payload.get("minion_id") or ""),
                route=route,
                delivery=delivery_for_reply(route, text),
                notes="minion event notification",
            )
        elif event.event_kind in {EventKind.MINION_TERMINAL, EventKind.MINION_MODULE_COMPLETED, EventKind.MINION_WORK_ORDER_COMPLETED}:
            text = _render_minion_event_notification(event.event_kind, payload)
            if not text:
                return []
            module_continue_delivery = (
                minion_module_continue_delivery(payload, route)
                if event.event_kind == EventKind.MINION_MODULE_COMPLETED
                else None
            )
            if module_continue_delivery is not None:
                action = ControlAction(
                    action_kind="interactive_open",
                    target_scope="interaction",
                    target_id=(
                        module_continue_delivery.interaction.interaction_id
                        if module_continue_delivery.interaction is not None
                        else None
                    ),
                    route=route,
                    delivery=module_continue_delivery,
                    notes="minion module continue",
                )
            else:
                action = ControlAction(
                    action_kind="route_reply",
                    target_scope="channel",
                    target_id=str(payload.get("run_id") or payload.get("minion_id") or ""),
                    route=route,
                    delivery=delivery_for_reply(route, text),
                    notes="minion event notification",
                )
            envelopes = [
                EventEnvelope(
                    event_kind=EventKind.CONTROL_ACTION,
                    source_kind=SourceKind.CONTROL,
                    payload=action,
                    correlation_id=event.correlation_id,
                )
            ]
            lesson_delivery = minion_lesson_approval_delivery(payload, route)
            if lesson_delivery is not None:
                envelopes.append(
                    EventEnvelope(
                        event_kind=EventKind.CONTROL_ACTION,
                        source_kind=SourceKind.CONTROL,
                        payload=ControlAction(
                            action_kind="interactive_open",
                            target_scope="interaction",
                            target_id=(
                                lesson_delivery.interaction.interaction_id
                                if lesson_delivery.interaction is not None
                                else None
                            ),
                            route=route,
                            delivery=lesson_delivery,
                            notes="minion lesson approval",
                        ),
                        correlation_id=event.correlation_id,
                    )
                )
            return envelopes
        else:
            return []
        return [
            EventEnvelope(
                event_kind=EventKind.CONTROL_ACTION,
                source_kind=SourceKind.CONTROL,
                payload=action,
                correlation_id=event.correlation_id,
            )
        ]


def _should_notify_checkpoint(payload: dict[str, Any]) -> bool:
    _ = payload
    return False


def _event_kind_for_minion_event(event_kind: str) -> str:
    if event_kind == "approval_requested":
        return EventKind.APPROVAL_REQUEST
    if event_kind == "clarification_requested":
        return EventKind.MINION_CLARIFICATION_REQUEST
    if event_kind == "terminal":
        return EventKind.MINION_TERMINAL
    if event_kind == "module_completed":
        return EventKind.MINION_MODULE_COMPLETED
    if event_kind == "work_order_completed":
        return EventKind.MINION_WORK_ORDER_COMPLETED
    if event_kind == "checkpoint":
        return EventKind.MINION_CHECKPOINT
    if event_kind == "plan_acceptance_pending":
        return EventKind.MINION_PLAN_ACCEPTANCE_PENDING
    return EventKind.MINION_PROGRESS


def _payload_for_event(event_kind: str, item: dict) -> dict:
    payload = dict(item.get("payload") or {})
    payload.setdefault("event_kind", item.get("event_kind") or "")
    payload.setdefault("minion_id", item.get("minion_id") or "")
    payload.setdefault("run_id", item.get("run_id") or "")
    payload.setdefault("work_order_id", item.get("work_order_id") or "")
    payload.setdefault("minion_profile", item.get("minion_profile") or "")
    payload.setdefault("created_at", item.get("created_at") or "")
    route = payload.get("route")
    if not isinstance(route, dict):
        route = dict((payload.get("metadata") or {}).get("control_route") or {})
    if route:
        payload["route"] = route
    return payload


def _render_minion_event_notification(event_kind: str, payload: dict[str, Any]) -> str:
    profile = str(payload.get("minion_profile") or "minion")
    run_id = str(payload.get("run_id") or "")
    work_order_id = str(payload.get("work_order_id") or "")
    if event_kind == EventKind.MINION_CHECKPOINT:
        status = str(payload.get("status") or "checkpoint")
        milestone = payload.get("milestone_index")
        summary = str(payload.get("summary") or "").strip()
        lines = [f"Minion checkpoint: {status}", f"Profile: {profile}"]
        if run_id:
            lines.append(f"Run: {run_id}")
        if work_order_id:
            lines.append(f"Work order: {work_order_id}")
        if milestone is not None:
            lines.append(f"Milestone: {milestone}")
        if summary:
            lines.append(f"Summary: {summary}")
        return "\n".join(lines)
    if event_kind == EventKind.MINION_PROGRESS:
        phase = str(payload.get("phase") or payload.get("event_kind") or "progress")
        summary = str(payload.get("summary") or "").strip()
        lines = [f"Minion progress: {phase}", f"Profile: {profile}"]
        if run_id:
            lines.append(f"Run: {run_id}")
        if work_order_id:
            lines.append(f"Work order: {work_order_id}")
        if summary and summary != phase:
            lines.append(f"Summary: {summary}")
        return "\n".join(lines)
    if event_kind == EventKind.MINION_MODULE_COMPLETED:
        status = str(payload.get("status") or "completed")
        summary = str(payload.get("summary") or "").strip()
        module_id = str(payload.get("module_id") or "").strip()
        lines = [f"Minion module completed: {status}", f"Profile: {profile}"]
        if module_id:
            lines.append(f"Module: {module_id}")
        if work_order_id:
            lines.append(f"Work order: {work_order_id}")
        completed_count = payload.get("completed_milestone_count")
        if completed_count is not None:
            lines.append(f"Milestones: {completed_count}")
        if summary:
            lines.append("Summary:")
            lines.append(_preview_text(summary, limit=500))
        return "\n".join(lines)
    if event_kind == EventKind.MINION_WORK_ORDER_COMPLETED:
        status = str(payload.get("status") or "completed")
        summary = str(payload.get("summary") or "").strip()
        task_id = str(payload.get("task_id") or "").strip()
        lines = [f"Minion work order completed: {status}", f"Profile: {profile}"]
        if task_id:
            lines.append(f"Task: {task_id}")
        if work_order_id:
            lines.append(f"Work order: {work_order_id}")
        completed_count = payload.get("completed_milestone_count")
        if completed_count is not None:
            lines.append(f"Milestones: {completed_count}")
        if summary:
            lines.append("Summary:")
            lines.append(_preview_text(summary, limit=500))
        return "\n".join(lines)
    if event_kind == EventKind.MINION_TERMINAL:
        status = str(payload.get("status") or "terminal")
        summary = _preview_text(_strip_lesson_sections(str(payload.get("summary") or "")).strip(), limit=500)
        artifacts = _artifact_list(payload.get("artifacts"))
        lines = [f"Minion finished: {status}", f"Profile: {profile}"]
        if run_id:
            lines.append(f"Run: {run_id}")
        if work_order_id:
            lines.append(f"Work order: {work_order_id}")
        if artifacts:
            lines.append("Artifacts:")
            for index, artifact in enumerate(artifacts[:3], start=1):
                label = str(artifact.get("title") or artifact.get("relative_path") or "artifact").strip()
                path = str(artifact.get("path") or artifact.get("relative_path") or "").strip()
                lines.append(f"[{index}] {label}: {path}")
            if len(artifacts) > 3:
                lines.append(f"[...] {len(artifacts) - 3} more")
        if summary:
            if artifacts:
                lines.append("")
            lines.append("Summary:")
            lines.append(summary)
        return "\n".join(lines)
    return ""


def _artifact_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _record_minion_observation(provider: object | None, context: Any, payload: dict[str, Any]) -> None:
    targets: list[Any] = []
    if provider is not None:
        targets.append(provider)
    port_registry = getattr(context, "port_registry", None)
    if isinstance(port_registry, dict):
        port = port_registry.get("minion:minion")
        if port is not None and port not in targets:
            targets.append(port)
    for target in targets:
        record = getattr(target, "record_minion_observation", None)
        if callable(record):
            try:
                record(payload)
            except Exception:
                continue
            return


def _preview_text(value: str, *, limit: int) -> str:
    text = _compact_preview_text(str(value or ""))
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _compact_preview_text(value: str) -> str:
    lines: list[str] = []
    blank_pending = False
    for raw_line in str(value or "").strip().splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            blank_pending = bool(lines)
            continue
        if blank_pending:
            lines.append("")
            blank_pending = False
        lines.append(line)
    return "\n".join(lines).strip()


def _strip_lesson_sections(text: str) -> str:
    current_lesson_section = False
    kept: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip().strip("-* ")
        if _lesson_heading_kind(stripped):
            current_lesson_section = True
            continue
        if current_lesson_section and stripped:
            continue
        current_lesson_section = False
        kept.append(line.rstrip())
    return "\n".join(kept).strip()


def _lesson_heading_kind(text: str) -> str:
    normalized = str(text or "").strip().strip("#*_` ")
    while normalized and not (normalized[0].isalnum() or normalized[0] == "_"):
        normalized = normalized[1:].strip()
    lowered = normalized.lower().replace("_", " ")
    lowered = lowered.rstrip(":").strip()
    if lowered in {"task lesson", "task lessons", "task wise lessons", "task-wise lessons"}:
        return "task_lessons"
    if lowered in {"system lesson", "system lessons", "system wise lessons", "system-wise lessons"}:
        return "system_lessons"
    if lowered.startswith(("task lesson:", "task lessons:", "task wise lessons:", "task-wise lessons:")):
        return "task_lessons"
    if lowered.startswith(("system lesson:", "system lessons:", "system wise lessons:", "system-wise lessons:")):
        return "system_lessons"
    return ""


def _route_from_payload(payload: object) -> ControlRoute | None:
    if not isinstance(payload, dict):
        return None
    endpoint_id = str(payload.get("endpoint_id") or "").strip()
    channel_kind = str(payload.get("channel_kind") or "").strip()
    if not endpoint_id or not channel_kind:
        return None
    return ControlRoute(
        endpoint_id=endpoint_id,
        channel_kind=channel_kind,
        reply_target=dict(payload.get("reply_target") or {}),
        control_scope_key=str(payload.get("control_scope_key") or ""),
        correlation_id=str(payload.get("correlation_id") or "") or None,
    )
