from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pal.foundation.persistence import utc_now


@dataclass(frozen=True)
class TaskContextPack:
    work_order_id: str
    goal: str = ""
    schema_version: int = 1
    pack_id: str = field(default_factory=lambda: f"tcp_{uuid4().hex[:16]}")
    instruction: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    workspace: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    memory_pack: dict[str, Any] = field(default_factory=dict)
    allowed_capabilities: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    allowed_skills: list[str] = field(default_factory=list)
    approval_policy: dict[str, Any] = field(default_factory=dict)
    minion_profile: str = "generic"
    resolved_profile: dict[str, Any] = field(default_factory=dict)
    continuity: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.instruction and self.goal:
            object.__setattr__(self, "instruction", self.goal)
        if not self.allowed_capabilities and self.allowed_tools:
            object.__setattr__(self, "allowed_capabilities", list(self.allowed_tools))
        if self.allowed_capabilities and not self.allowed_tools:
            object.__setattr__(self, "allowed_tools", list(self.allowed_capabilities))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "pack_id": self.pack_id,
            "work_order_id": self.work_order_id,
            "goal": self.goal,
            "instruction": self.instruction,
            "acceptance_criteria": list(self.acceptance_criteria),
            "workspace": dict(self.workspace),
            "artifacts": [dict(item) for item in self.artifacts],
            "memory_pack": dict(self.memory_pack),
            "allowed_capabilities": list(self.allowed_capabilities),
            "allowed_tools": list(self.allowed_tools),
            "allowed_skills": list(self.allowed_skills),
            "approval_policy": dict(self.approval_policy),
            "minion_profile": self.minion_profile or "generic",
            "resolved_profile": dict(self.resolved_profile),
            "continuity": dict(self.continuity),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskContextPack":
        if not isinstance(payload, dict):
            raise ValueError("TaskContextPack payload must be an object")
        work_order_id = str(payload.get("work_order_id") or "").strip()
        if not work_order_id:
            raise ValueError("TaskContextPack.work_order_id is required")
        allowed_capabilities = _string_list(payload.get("allowed_capabilities"))
        allowed_tools = _string_list(payload.get("allowed_tools"))
        if not allowed_capabilities:
            allowed_capabilities = list(allowed_tools)
        if not allowed_tools:
            allowed_tools = list(allowed_capabilities)
        return cls(
            schema_version=int(payload.get("schema_version") or 1),
            pack_id=str(payload.get("pack_id") or f"tcp_{uuid4().hex[:16]}"),
            work_order_id=work_order_id,
            goal=str(payload.get("goal") or ""),
            instruction=str(payload.get("instruction") or payload.get("goal") or ""),
            acceptance_criteria=_string_list(payload.get("acceptance_criteria")),
            workspace=_dict(payload.get("workspace")),
            artifacts=[_dict(item) for item in list(payload.get("artifacts") or []) if isinstance(item, dict)],
            memory_pack=_dict(payload.get("memory_pack")),
            allowed_capabilities=allowed_capabilities,
            allowed_tools=allowed_tools,
            allowed_skills=_string_list(payload.get("allowed_skills")),
            approval_policy=_dict(payload.get("approval_policy")),
            minion_profile=str(payload.get("minion_profile") or "generic"),
            resolved_profile=_dict(payload.get("resolved_profile")),
            continuity=_dict(payload.get("continuity")),
            metadata=_dict(payload.get("metadata")),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "TaskContextPack":
        try:
            payload = json.loads(str(raw or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"TaskContextPack JSON is invalid: {exc}") from exc
        return cls.from_dict(payload)


@dataclass(frozen=True)
class MinionProgressEvent:
    work_order_id: str
    summary: str
    minion_id: str = ""
    run_id: str = ""
    phase: str = ""


@dataclass(frozen=True)
class CheckpointEvent:
    work_order_id: str
    summary: str
    milestone_index: int = 0
    status: str = "partial"
    minion_id: str = ""
    run_id: str = ""


@dataclass(frozen=True)
class MinionTerminalEvent:
    work_order_id: str
    status: str
    summary: str = ""
    minion_id: str = ""
    run_id: str = ""


@dataclass(frozen=True)
class MinionApprovalRequest:
    approval_id: str
    minion_id: str
    run_id: str
    work_order_id: str
    title: str
    requested_action: str
    risk: str = "high"
    impact: str = ""
    target: str = ""
    args_summary: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "work_order_id": self.work_order_id,
            "title": self.title,
            "requested_action": self.requested_action,
            "risk": self.risk,
            "impact": self.impact,
            "target": self.target,
            "args_summary": dict(self.args_summary),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MinionApprovalRequest":
        if not isinstance(payload, dict):
            raise ValueError("MinionApprovalRequest payload must be an object")
        approval_id = str(payload.get("approval_id") or "").strip()
        if not approval_id:
            raise ValueError("approval_id is required")
        return cls(
            approval_id=approval_id,
            minion_id=str(payload.get("minion_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            work_order_id=str(payload.get("work_order_id") or ""),
            title=str(payload.get("title") or "Minion approval request"),
            requested_action=str(payload.get("requested_action") or ""),
            risk=str(payload.get("risk") or "high"),
            impact=str(payload.get("impact") or ""),
            target=str(payload.get("target") or ""),
            args_summary=_dict(payload.get("args_summary")),
            created_at=str(payload.get("created_at") or utc_now()),
        )


@dataclass(frozen=True)
class MinionApprovalDecision:
    approval_id: str
    decision: str
    minion_id: str = ""
    run_id: str = ""
    edit_note: str = ""
    decided_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "decision": self.decision,
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "edit_note": self.edit_note,
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MinionApprovalDecision":
        if not isinstance(payload, dict):
            raise ValueError("MinionApprovalDecision payload must be an object")
        approval_id = str(payload.get("approval_id") or "").strip()
        decision = str(payload.get("decision") or "").strip().lower()
        if not approval_id:
            raise ValueError("approval_id is required")
        if decision not in {"accept", "reject", "edit"}:
            raise ValueError("decision must be accept, reject, or edit")
        return cls(
            approval_id=approval_id,
            decision=decision,
            minion_id=str(payload.get("minion_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            edit_note=str(payload.get("edit_note") or ""),
            decided_at=str(payload.get("decided_at") or utc_now()),
        )


@dataclass(frozen=True)
class MinionEvent:
    event_kind: str
    minion_id: str
    run_id: str
    work_order_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_kind": self.event_kind,
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "work_order_id": self.work_order_id,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MinionEvent":
        if not isinstance(payload, dict):
            raise ValueError("MinionEvent payload must be an object")
        event_kind = str(payload.get("event_kind") or "").strip()
        if not event_kind:
            raise ValueError("MinionEvent.event_kind is required")
        return cls(
            event_kind=event_kind,
            minion_id=str(payload.get("minion_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            work_order_id=str(payload.get("work_order_id") or ""),
            payload=_dict(payload.get("payload")),
            created_at=str(payload.get("created_at") or utc_now()),
        )


@dataclass(frozen=True)
class MinionRun:
    minion_id: str
    run_id: str
    work_order_id: str
    status: str
    instruction: str = ""
    pid: int | None = None
    started_at: str = field(default_factory=utc_now)
    ended_at: str = ""
    last_error: str = ""
    last_event: dict[str, Any] = field(default_factory=dict)
    pending_approval: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "work_order_id": self.work_order_id,
            "status": self.status,
            "instruction": self.instruction,
            "pid": self.pid,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "last_error": self.last_error,
            "last_event": dict(self.last_event),
            "pending_approval": dict(self.pending_approval),
        }


@dataclass(frozen=True)
class ServiceTriggerEvent:
    service_id: str
    trigger_kind: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in list(value or []) if str(item).strip()]
