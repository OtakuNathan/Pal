from __future__ import annotations

from dataclasses import dataclass


RUNTIME_ONLY_SURFACES = frozenset(
    {
        "l1",
        "l2",
        "active_turn_stack",
        "in_flight_tool_calls",
        "ephemeral_scheduler_timers",
    }
)

DURABLE_TRUTH_SURFACES = frozenset(
    {
        "persona_preferences",
        "channel_endpoints",
        "llm_endpoints",
        "l3_memory",
        "tasking_truth",
        "service_truth",
        "developer_reports",
    }
)


@dataclass(frozen=True)
class OwnershipBoundary:
    runtime_only: frozenset[str] = RUNTIME_ONLY_SURFACES
    durable_truth: frozenset[str] = DURABLE_TRUTH_SURFACES
