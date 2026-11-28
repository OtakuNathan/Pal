"""The manager's required event delivery and acknowledgement contract."""
from __future__ import annotations

from typing import Any, Mapping, Protocol


class BunshinEventPort(Protocol):
    def has_pending_events(self) -> bool: ...
    def drain_events_sync(self, *, limit: int = 20) -> dict[str, Any]: ...
    def may_send_event(self, event: dict[str, Any]) -> bool: ...
    def begin_background_delivery(self, event: dict[str, Any]) -> bool: ...
    def mark_background_delivery_uncertain(self, event: dict[str, Any]) -> None: ...
    def delivered_event_parts(self, event: dict[str, Any]) -> set[str]: ...
    def settle_event_part(self, event: dict[str, Any], part_key: str) -> bool: ...
    def settle_event(self, event: dict[str, Any], *, accepted: bool, error: str = "") -> bool: ...


def terminal_has_user_actions(payload: Mapping[str, Any]) -> bool:
    """A terminal event may still need to close a user interaction or review learning."""
    return bool(payload.get("resolved_interactions") or payload.get("memory_candidates"))


def channel_result_requested(payload: Mapping[str, Any]) -> bool:
    """Legacy ordinary workflows delivered to their channel; scheduled ones opt in."""
    return bool(payload.get("channel_delivery_requested", not payload.get("scheduled_run_id")))


def terminal_is_control_only(kind: object, payload: Mapping[str, Any]) -> bool:
    return kind == "terminal" or (kind == "workflow_terminal" and not channel_result_requested(payload))


def scheduled_terminal_presentation(event: Mapping[str, Any]) -> bool:
    """A scheduled final packet with an immutable result receipt."""
    payload = event.get("payload") or {}
    return bool(event.get("event_kind") == "workflow_terminal"
                and event.get("frozen_delivery_binding")
                and payload.get("status") == "completed"
                and dict(payload.get("result_artifact_ref") or {}).get("sha256"))


def same_scheduled_terminal_intent(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    """Only presentation may change; route, receipt, identity and actions must agree."""
    if not scheduled_terminal_presentation(old) or not scheduled_terminal_presentation(new):
        return False
    def intent(event: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(event["payload"])
        payload.pop("summary", None)
        payload.pop("attachments", None)
        return {**event, "payload": payload}
    return intent(old) == intent(new)
