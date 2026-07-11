from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pal.foundation.persistence import utc_now


@dataclass(frozen=True)
class MinionInvocationPack:
    invocation_id: str
    goal: str = ""
    schema_version: int = 1
    pack_id: str = field(default_factory=lambda: f"tcp_{uuid4().hex[:16]}")
    instruction: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    workspace: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    memory_pack: dict[str, Any] = field(default_factory=dict)
    allowed_capabilities: list[str] = field(default_factory=list)
    allowed_skills: list[str] = field(default_factory=list)
    approval_policy: dict[str, Any] = field(default_factory=dict)
    profile_group: str = "general"
    profile_name: str = "generic"
    minion_profile: str = "generic"
    resolved_profile: dict[str, Any] = field(default_factory=dict)
    continuity: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.instruction and self.goal:
            object.__setattr__(self, "instruction", self.goal)
        group, name = _profile_ref_parts(self.profile_group, self.profile_name, self.minion_profile)
        object.__setattr__(self, "profile_group", group)
        object.__setattr__(self, "profile_name", name)
        object.__setattr__(self, "minion_profile", _canonical_profile_id(group, name))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "pack_id": self.pack_id,
            "invocation_id": self.invocation_id,
            "goal": self.goal,
            "instruction": self.instruction,
            "acceptance_criteria": list(self.acceptance_criteria),
            "workspace": dict(self.workspace),
            "artifacts": [dict(item) for item in self.artifacts],
            "memory_pack": dict(self.memory_pack),
            "allowed_capabilities": list(self.allowed_capabilities),
            "allowed_skills": list(self.allowed_skills),
            "approval_policy": dict(self.approval_policy),
            "profile_group": self.profile_group or "general",
            "profile_name": self.profile_name or "generic",
            "minion_profile": self.minion_profile or "generic",
            "resolved_profile": dict(self.resolved_profile),
            "continuity": dict(self.continuity),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MinionInvocationPack":
        if not isinstance(payload, dict):
            raise ValueError("MinionInvocationPack payload must be an object")
        invocation_id = str(payload.get("invocation_id") or "").strip()
        if not invocation_id:
            raise ValueError("MinionInvocationPack.invocation_id is required")
        allowed_capabilities = _string_list(payload.get("allowed_capabilities"))
        return cls(
            schema_version=int(payload.get("schema_version") or 1),
            pack_id=str(payload.get("pack_id") or f"tcp_{uuid4().hex[:16]}"),
            invocation_id=invocation_id,
            goal=str(payload.get("goal") or ""),
            instruction=str(payload.get("instruction") or payload.get("goal") or ""),
            acceptance_criteria=_string_list(payload.get("acceptance_criteria")),
            workspace=_dict(payload.get("workspace")),
            artifacts=[_dict(item) for item in list(payload.get("artifacts") or []) if isinstance(item, dict)],
            memory_pack=_dict(payload.get("memory_pack")),
            allowed_capabilities=allowed_capabilities,
            allowed_skills=_string_list(payload.get("allowed_skills")),
            approval_policy=_dict(payload.get("approval_policy")),
            profile_group=str(payload.get("profile_group") or ""),
            profile_name=str(payload.get("profile_name") or ""),
            minion_profile=str(payload.get("minion_profile") or "generic"),
            resolved_profile=_dict(payload.get("resolved_profile")),
            continuity=_dict(payload.get("continuity")),
            metadata=_dict(payload.get("metadata")),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "MinionInvocationPack":
        try:
            payload = json.loads(str(raw or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"MinionInvocationPack JSON is invalid: {exc}") from exc
        return cls.from_dict(payload)


@dataclass(frozen=True)
class MinionApprovalDecision:
    approval_id: str
    decision: str
    minion_id: str = ""
    run_id: str = ""
    decided_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "decision": self.decision,
            "minion_id": self.minion_id,
            "run_id": self.run_id,
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
        if decision not in {"accept", "accept_all", "reject"}:
            raise ValueError("decision must be accept, accept_all, or reject")
        return cls(
            approval_id=approval_id,
            decision=decision,
            minion_id=str(payload.get("minion_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            decided_at=str(payload.get("decided_at") or utc_now()),
        )


@dataclass(frozen=True)
class ProactiveTriggerEvent:
    proactive_id: str
    trigger_kind: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in list(value or []) if str(item).strip()]


def _profile_ref_parts(group: str, name: str, canonical: str) -> tuple[str, str]:
    resolved_group = str(group or "").strip()
    resolved_name = str(name or "").strip()
    has_explicit_parts = bool(
        resolved_group
        and resolved_name
        and (resolved_group != "general" or resolved_name != "generic")
    )
    if resolved_group and resolved_name:
        if has_explicit_parts:
            return resolved_group, resolved_name
    raw = str(canonical or "").strip()
    if raw and "." in raw:
        left, right = raw.rsplit(".", 1)
        return left.strip() or "general", right.strip() or "generic"
    if raw and not has_explicit_parts:
        return resolved_group or "general", raw
    return resolved_group or "general", resolved_name or "generic"


def _canonical_profile_id(group: str, name: str) -> str:
    resolved_group = str(group or "general").strip().replace("/", ".") or "general"
    resolved_name = str(name or "generic").strip() or "generic"
    return resolved_name if resolved_group == "general" else f"{resolved_group}.{resolved_name}"
