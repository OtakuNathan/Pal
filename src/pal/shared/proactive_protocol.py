"""Resident-facing scheduling contract, stable across proactive plugin reloads."""
from __future__ import annotations

from typing import Protocol

from pal.shared.ports import PortKey


class SilentOnceSchedulerPort(Protocol):
    def ensure_silent_once(self, *, proactive_id: str, goal: str, method: str) -> str:
        """Persist a due-once null-output task without rearming an existing id."""
        ...


SILENT_ONCE_SCHEDULER = PortKey[SilentOnceSchedulerPort](
    "proactive:silent_once_scheduler", SilentOnceSchedulerPort,
)
