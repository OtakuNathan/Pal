from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pal.shared import ServiceTriggerEvent


@dataclass(frozen=True)
class ServiceDefinition:
    service_id: str
    goal: str
    method: str = ""
    skill_refs: list[str] = field(default_factory=list)
    out_channel_id: str | None = None
    schedule: dict[str, object] = field(default_factory=dict)
    enabled: bool = True

class ScheduleEnginePort(Protocol):
    def next_due_at(self, service_id: str) -> str | None:
        ...


class ServiceRunnerPort(Protocol):
    def run(self, trigger: ServiceTriggerEvent) -> None:
        ...


class ServiceManagerPort(Protocol):
    def register(self, service: ServiceDefinition) -> None:
        ...
