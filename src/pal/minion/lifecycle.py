from __future__ import annotations


ACTIVE_RUN_STATUSES = {"starting", "running", "approval_pending", "clarification_pending"}
TERMINAL_RUN_STATUSES = {"completed", "failed", "blocked", "killed"}

RUN_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "starting": {
        "running",
        "approval_pending",
        "clarification_pending",
        "completed",
        "failed",
        "blocked",
        "killed",
    },
    "running": {
        "approval_pending",
        "clarification_pending",
        "completed",
        "failed",
        "blocked",
        "killed",
    },
    "approval_pending": {
        "running",
        "approval_pending",
        "clarification_pending",
        "completed",
        "failed",
        "blocked",
        "killed",
    },
    "clarification_pending": {
        "running",
        "approval_pending",
        "clarification_pending",
        "completed",
        "failed",
        "blocked",
        "killed",
    },
    "completed": set(),
    "failed": set(),
    "blocked": set(),
    "killed": set(),
}


class RunStatusTransitionError(ValueError):
    pass


def transition_run_status(current: str, target: str) -> str:
    current_status = str(current or "starting").strip() or "starting"
    target_status = str(target or "").strip()
    if not target_status:
        raise RunStatusTransitionError("target run status is required")
    if target_status == current_status:
        return current_status
    allowed = RUN_STATUS_TRANSITIONS.get(current_status)
    if allowed is None:
        raise RunStatusTransitionError(f"unknown run status: {current_status}")
    if target_status not in allowed:
        raise RunStatusTransitionError(f"invalid run status transition: {current_status} -> {target_status}")
    return target_status
