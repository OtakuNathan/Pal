from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


FAILURE_VERIFICATION_OK = "ok"
FAILURE_VERIFICATION_DEGRADED = "degraded"
FAILURE_VERIFICATION_FAILED = "failed"


@dataclass(frozen=True)
class FailureSignal:
    subsystem: str
    component: str
    failure_kind: str
    severity: str
    primary_blocker: str
    evidence: dict[str, Any] = field(default_factory=dict)
    related_ids: dict[str, str] = field(default_factory=dict)
    safe_to_retry: bool = False
    repair_domain: str = ""
    known_first_party_repair_domain: bool = False
    related_modules: tuple[str, ...] = ()
    secondary_issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationResult:
    status: str
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class FailureDraft:
    subsystem: str
    component: str
    failure_kind: str
    severity: str
    primary_blocker: str
    secondary_issues: list[str] = field(default_factory=list)
    attempted_actions: list[str] = field(default_factory=list)
    documents_checked: list[str] = field(default_factory=list)
    maintenance_outcomes: list[dict[str, Any]] = field(default_factory=list)
    verification_outcomes: list[VerificationResult] = field(default_factory=list)
    repair_domain: str = ""
    related_ids: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    safe_to_retry: bool = False
    known_first_party_repair_domain: bool = False
    current_blocker: str = ""
    why_blocked: str = ""
    impact: str = ""
    possible_solutions: list[str] = field(default_factory=list)
    recommended_next_step: str = ""
    requires_developer_action: bool = False


@dataclass(frozen=True)
class FailureReport:
    report_id: str
    subsystem: str
    component: str
    severity: str
    failure_kind: str
    why_blocked: str
    current_blocker: str
    impact: str
    attempted_actions: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    documents_checked: list[str] = field(default_factory=list)
    possible_solutions: list[str] = field(default_factory=list)
    safe_to_retry: bool = False
    requires_developer_action: bool = True
    recommended_next_step: str = ""
    related_ids: dict[str, str] = field(default_factory=dict)
    verification_status: str = FAILURE_VERIFICATION_FAILED


@dataclass(frozen=True)
class FailureUserFeedback:
    status: str
    summary: str
    blocker: str
    next_step: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RepairResolutionRecord:
    subsystem: str
    component: str
    failure_kind: str
    situation_text: str
    task_text: str
    action_text: str
    result_text: str
    related_ids: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RepairWorkOrderDraft:
    subsystem: str
    component: str
    repair_domain: str
    primary_blocker: str
    summary: str
    related_ids: dict[str, str] = field(default_factory=dict)

