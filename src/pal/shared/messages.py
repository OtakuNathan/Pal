from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TaskContextPack:
    work_order_id: str
    goal: str
    memory_pack: dict[str, Any] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    allowed_skills: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MinionProgressEvent:
    work_order_id: str
    summary: str


@dataclass(frozen=True)
class CheckpointEvent:
    work_order_id: str
    summary: str
    next_resume_point: str = ""


@dataclass(frozen=True)
class MinionTerminalEvent:
    work_order_id: str
    status: str
    summary: str = ""


@dataclass(frozen=True)
class ServiceTriggerEvent:
    service_id: str
    trigger_kind: str
    metadata: dict[str, Any] = field(default_factory=dict)
