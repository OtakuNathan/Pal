from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pal.failure.contracts import (
    FAILURE_VERIFICATION_DEGRADED,
    FAILURE_VERIFICATION_FAILED,
    FAILURE_VERIFICATION_OK,
    FailureDraft,
    FailureReport,
    FailureSignal,
    FailureUserFeedback,
    RepairResolutionRecord,
    RepairWorkOrderDraft,
    VerificationResult,
)


@dataclass
class FailureRuntime:
    recent_reports: list[FailureReport] = field(default_factory=list)
    recent_failures: list[dict[str, Any]] = field(default_factory=list)

    def begin_draft(self, signal: FailureSignal) -> FailureDraft:
        return FailureDraft(
            subsystem=signal.subsystem,
            component=signal.component,
            failure_kind=signal.failure_kind,
            severity=signal.severity,
            primary_blocker=signal.primary_blocker,
            secondary_issues=list(signal.secondary_issues),
            repair_domain=signal.repair_domain,
            related_ids=dict(signal.related_ids),
            evidence=dict(signal.evidence),
            safe_to_retry=signal.safe_to_retry,
            known_first_party_repair_domain=signal.known_first_party_repair_domain,
            current_blocker=signal.primary_blocker,
        )

    def absorb_maintenance_outcome(
        self,
        draft: FailureDraft,
        *,
        action_name: str,
        status: str,
        ok: bool,
        text: str = "",
        structured: dict[str, Any] | None = None,
    ) -> None:
        draft.attempted_actions.append(action_name)
        draft.maintenance_outcomes.append(
            {
                "action_name": action_name,
                "status": status,
                "ok": ok,
                "text": text,
                "structured": dict(structured or {}),
            }
        )

    def absorb_secondary_issue(self, draft: FailureDraft, issue: str) -> None:
        if issue and issue not in draft.secondary_issues:
            draft.secondary_issues.append(issue)

    def record_document_checked(self, draft: FailureDraft, document: str) -> None:
        if document and document not in draft.documents_checked:
            draft.documents_checked.append(document)

    def record_verification(self, draft: FailureDraft, verification: VerificationResult) -> None:
        draft.verification_outcomes.append(verification)

    def build_report(
        self,
        draft: FailureDraft,
        *,
        verification: VerificationResult,
        enriched_fields: dict[str, Any] | None = None,
    ) -> FailureReport:
        enriched = dict(enriched_fields or {})
        possible_solutions = _normalize_string_list(enriched.get("possible_solutions"))
        if not possible_solutions:
            possible_solutions = ["Inspect the affected subsystem state and repeat the last safe maintenance step."]
        why_blocked = str(enriched.get("why_blocked") or draft.why_blocked or "Inline repair could not clear the active blocker.").strip()
        current_blocker = str(enriched.get("current_blocker") or draft.current_blocker or draft.primary_blocker).strip()
        impact = str(enriched.get("impact") or draft.impact or f"{draft.subsystem} remains degraded during the current turn.").strip()
        recommended_next_step = str(
            enriched.get("recommended_next_step")
            or draft.recommended_next_step
            or "Review the failure evidence and continue with a developer-guided repair."
        ).strip()
        report = FailureReport(
            report_id=f"failure-{uuid4()}",
            subsystem=draft.subsystem,
            component=draft.component,
            severity=draft.severity,
            failure_kind=draft.failure_kind,
            why_blocked=why_blocked,
            current_blocker=current_blocker,
            impact=impact,
            attempted_actions=list(draft.attempted_actions),
            evidence=dict(draft.evidence),
            documents_checked=list(draft.documents_checked),
            possible_solutions=possible_solutions,
            safe_to_retry=draft.safe_to_retry,
            requires_developer_action=True,
            recommended_next_step=recommended_next_step,
            related_ids=dict(draft.related_ids),
            verification_status=verification.status,
        )
        self.recent_reports.append(report)
        self.recent_reports[:] = self.recent_reports[-32:]
        return report

    def render_user_feedback(
        self,
        draft: FailureDraft,
        *,
        verification: VerificationResult,
        report: FailureReport | None = None,
    ) -> FailureUserFeedback:
        if verification.status == FAILURE_VERIFICATION_OK:
            return FailureUserFeedback(
                status=FAILURE_VERIFICATION_OK,
                summary=f"I repaired the {draft.subsystem} issue in this turn.",
                blocker=draft.primary_blocker,
                next_step="Continuing with the updated runtime state.",
            )
        if verification.status == FAILURE_VERIFICATION_DEGRADED:
            feedback = FailureUserFeedback(
                status=FAILURE_VERIFICATION_DEGRADED,
                summary=f"I stabilized the {draft.subsystem} issue enough to continue in degraded mode.",
                blocker=draft.primary_blocker,
                next_step="Some functionality may remain limited until a fuller repair is performed.",
            )
            self.recent_failures.append(
                {
                    "subsystem": draft.subsystem,
                    "component": draft.component,
                    "failure_kind": draft.failure_kind,
                    "primary_blocker": draft.primary_blocker,
                    "verification_status": verification.status,
                }
            )
            self.recent_failures[:] = self.recent_failures[-32:]
            return feedback
        return FailureUserFeedback(
            status=FAILURE_VERIFICATION_FAILED,
            summary=f"I could not repair the {draft.subsystem} issue inline.",
            blocker=report.current_blocker if report is not None else draft.primary_blocker,
            next_step=report.recommended_next_step if report is not None else "Developer escalation is required.",
        )

    def build_repair_resolution_record(
        self,
        draft: FailureDraft,
        *,
        verification: VerificationResult,
    ) -> RepairResolutionRecord:
        action_lines = [item["action_name"] for item in draft.maintenance_outcomes if item.get("action_name")]
        task_text = f"Restore healthy operation for {draft.subsystem}:{draft.component}."
        action_text = ", ".join(action_lines) or "Inspected state and applied bounded runtime maintenance."
        result_text = verification.reason.strip() or "The blocker was cleared and the repair was verified."
        return RepairResolutionRecord(
            subsystem=draft.subsystem,
            component=draft.component,
            failure_kind=draft.failure_kind,
            situation_text=draft.primary_blocker,
            task_text=task_text,
            action_text=action_text,
            result_text=result_text,
            related_ids=dict(draft.related_ids),
        )

    def maybe_build_work_order_draft(
        self,
        draft: FailureDraft,
        *,
        verification: VerificationResult,
    ) -> RepairWorkOrderDraft | None:
        if verification.status == FAILURE_VERIFICATION_OK:
            return None
        if not draft.known_first_party_repair_domain:
            return None
        if not draft.repair_domain:
            return None
        return RepairWorkOrderDraft(
            subsystem=draft.subsystem,
            component=draft.component,
            repair_domain=draft.repair_domain,
            primary_blocker=draft.primary_blocker,
            summary=f"Inline repair was insufficient for {draft.subsystem}:{draft.component}.",
            related_ids=dict(draft.related_ids),
        )

    def show_summary(self) -> dict[str, Any]:
        return {
            "recent_report_count": len(self.recent_reports),
            "recent_failure_count": len(self.recent_failures),
            "latest_report_id": self.recent_reports[-1].report_id if self.recent_reports else None,
        }


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []

