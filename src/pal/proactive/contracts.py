from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pal.shared import ProactiveTriggerEvent


@dataclass(frozen=True)
class ProactiveDefinition:
    proactive_id: str
    goal: str
    method: str = ""
    skill_refs: list[str] = field(default_factory=list)
    out_channel_id: str | None = None
    schedule: dict[str, object] = field(default_factory=dict)
    out_reply_target: dict[str, object] = field(default_factory=dict)
    enabled: bool = True

class ScheduleEnginePort(Protocol):
    def next_due_at(self, proactive_id: str) -> str | None:
        ...


class ProactiveRunnerPort(Protocol):
    def run(self, trigger: ProactiveTriggerEvent) -> None:
        ...


class ProactiveManagerPort(Protocol):
    def register(self, definition: ProactiveDefinition) -> None:
        ...
