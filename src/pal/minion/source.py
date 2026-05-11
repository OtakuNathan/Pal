from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pal.control import ControlAction, ControlRoute, InteractionButtonSpec, InteractionMessageSpec
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
        }

    def handle(self, event: EventEnvelope, context) -> list[EventEnvelope]:
        payload = dict(event.payload or {}) if isinstance(event.payload, dict) else {}
        if event.event_kind == EventKind.MINION_TERMINAL:
            _record_minion_observation(self.provider, context, payload)
        route = _route_from_payload(payload.get("route"))
        if route is None:
            return []
        if event.event_kind == EventKind.APPROVAL_REQUEST:
            spec = _build_minion_approval_interaction(payload, route)
            action = ControlAction(
                action_kind="interactive_open",
                target_scope="interaction",
                target_id=str(payload.get("approval_id") or f"approval_{uuid4().hex[:8]}"),
                route=route,
                args={
                    "kind": "approval_request",
                    "interaction": _interaction_payload(spec),
                },
                notes="minion approval request",
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
                args={"text": text},
                notes="minion event notification",
            )
        elif event.event_kind == EventKind.MINION_TERMINAL:
            text = _render_minion_event_notification(event.event_kind, payload)
            if not text:
                return []
            action = ControlAction(
                action_kind="route_reply",
                target_scope="channel",
                target_id=str(payload.get("run_id") or payload.get("minion_id") or ""),
                route=route,
                args={"text": text},
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
            lesson_spec = _build_minion_lesson_interaction(payload, route)
            if lesson_spec is not None:
                envelopes.append(
                    EventEnvelope(
                        event_kind=EventKind.CONTROL_ACTION,
                        source_kind=SourceKind.CONTROL,
                        payload=ControlAction(
                            action_kind="interactive_open",
                            target_scope="interaction",
                            target_id=lesson_spec.interaction_id,
                            route=route,
                            args={"kind": "minion_lesson_approval", "interaction": _interaction_payload(lesson_spec)},
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
    if event_kind == "terminal":
        return EventKind.MINION_TERMINAL
    if event_kind == "checkpoint":
        return EventKind.MINION_CHECKPOINT
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


def _build_minion_approval_interaction(payload: dict[str, Any], route: ControlRoute) -> InteractionMessageSpec:
    approval_id = str(payload.get("approval_id") or f"approval_{uuid4().hex[:8]}")
    return InteractionMessageSpec(
        interaction_id=approval_id,
        interaction_kind="approval_request",
        route=route,
        text=_render_minion_approval_text(payload),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Accept",
                    action_key="control.action.dispatch",
                    action_args=_minion_approval_action_payload(payload, "accept"),
                ),
                InteractionButtonSpec(
                    label="Reject",
                    action_key="control.action.dispatch",
                    action_args=_minion_approval_action_payload(payload, "reject"),
                ),
                InteractionButtonSpec(
                    label="Edit",
                    action_key="control.action.dispatch",
                    action_args=_minion_approval_action_payload(payload, "edit"),
                ),
            ),
        ),
    )


def _interaction_payload(spec: InteractionMessageSpec) -> dict[str, Any]:
    return {
        "interaction_id": spec.interaction_id,
        "interaction_kind": spec.interaction_kind,
        "text": spec.text,
        "buttons": [
            [
                {"label": button.label, "action_key": button.action_key, "action_args": dict(button.action_args)}
                for button in row
            ]
            for row in spec.buttons
        ],
        "expires_at": spec.expires_at,
    }


def _minion_approval_action_payload(payload: dict[str, Any], decision: str) -> dict[str, Any]:
    approval_id = str(payload.get("approval_id") or "")
    return {
        "action_kind": "minion_approval_decision",
        "target_scope": "minion",
        "target_id": approval_id,
        "args": {
            "approval_id": approval_id,
            "run_id": str(payload.get("run_id") or ""),
            "minion_id": str(payload.get("minion_id") or ""),
            "decision": decision,
        },
    }


def _build_minion_lesson_interaction(payload: dict[str, Any], route: ControlRoute) -> InteractionMessageSpec | None:
    task_lessons = _string_list(payload.get("task_lessons"))
    system_lessons = _string_list(payload.get("system_lessons"))
    if not task_lessons and not system_lessons:
        return None
    run_id = str(payload.get("run_id") or "")
    work_order_id = str(payload.get("work_order_id") or "")
    interaction_id = f"minion_lesson_{run_id or uuid4().hex[:12]}"
    lines = [
        "Minion proposed reusable lessons.",
        "",
        "Absorb these into Pal memory?",
    ]
    if task_lessons:
        lines.append("")
        lines.append("Task lessons:")
        lines.extend(f"- {lesson}" for lesson in task_lessons)
    if system_lessons:
        lines.append("")
        lines.append("System lessons:")
        lines.extend(f"- {lesson}" for lesson in system_lessons)
    base_args = {
        "work_order_id": work_order_id,
        "run_id": run_id,
        "minion_id": str(payload.get("minion_id") or ""),
        "task_lessons": task_lessons,
        "system_lessons": system_lessons,
    }
    return InteractionMessageSpec(
        interaction_id=interaction_id,
        interaction_kind="minion_lesson_approval",
        route=route,
        text="\n".join(lines),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Accept",
                    action_key="control.action.dispatch",
                    action_args={
                        "action_kind": "minion_lesson_decision",
                        "target_scope": "minion",
                        "target_id": work_order_id,
                        "args": {**base_args, "decision": "accept"},
                    },
                ),
                InteractionButtonSpec(
                    label="Reject",
                    action_key="control.action.dispatch",
                    action_args={
                        "action_kind": "minion_lesson_decision",
                        "target_scope": "minion",
                        "target_id": work_order_id,
                        "args": {**base_args, "decision": "reject"},
                    },
                ),
                InteractionButtonSpec(
                    label="Edit",
                    action_key="control.action.dispatch",
                    action_args={
                        "action_kind": "minion_lesson_decision",
                        "target_scope": "minion",
                        "target_id": work_order_id,
                        "args": {**base_args, "decision": "edit"},
                    },
                ),
            ),
        ),
    )


def _render_minion_approval_text(payload: dict[str, Any]) -> str:
    args_summary = payload.get("args_summary")
    if not isinstance(args_summary, dict):
        args_summary = {}
    lines = [
        "Minion approval request",
        "",
        f"Title: {payload.get('title') or 'High-risk operation'}",
        f"Run: {payload.get('run_id') or '-'}",
        f"Work order: {payload.get('work_order_id') or '-'}",
        f"Requested action: {payload.get('requested_action') or payload.get('target') or '-'}",
        f"Risk: {payload.get('risk') or 'high'}",
    ]
    impact = str(payload.get("impact") or "").strip()
    if impact:
        lines.append(f"Impact: {impact}")
    target = str(payload.get("target") or "").strip()
    if target:
        lines.append(f"Target: {target}")
    if args_summary:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(args_summary.items()))
        if rendered:
            lines.append(f"Args: {rendered}")
    return "\n".join(lines)


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
            for artifact in artifacts[:3]:
                label = str(artifact.get("title") or artifact.get("relative_path") or "artifact").strip()
                path = str(artifact.get("path") or artifact.get("relative_path") or "").strip()
                lines.append(f"- {label}: {path}")
            if len(artifacts) > 3:
                lines.append(f"- ... {len(artifacts) - 3} more")
        if summary:
            lines.append(f"Summary: {summary}")
        return "\n".join(lines)
    return ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = []
    return _dedupe_nonempty([str(item) for item in values])


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
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _dedupe_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = " ".join(str(value or "").split())
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


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
