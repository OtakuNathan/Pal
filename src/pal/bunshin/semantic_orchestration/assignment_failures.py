from __future__ import annotations
from pal.foundation.diagnostics import exception_report
from pal.bunshin.unit_of_work import BunshinUnitOfWork
from pal.bunshin.semantic_orchestration.assignment_rules import _charged_role_failure_attempt_count
from dataclasses import dataclass
from typing import Any, Mapping
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, AggregateVersionConflict, DeferredEffectError, SubmissionInvariantError
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.role_protocol import RoleAssignmentState, stable_hash
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.role_leases import RoleLeases
from pal.bunshin.semantic_orchestration.routes import SEMANTIC_EFFECT_ROUTES


@dataclass
class AssignmentFailures:
    effect_reads: EffectReads
    role_leases: RoleLeases
    artifacts: ContentAddressedArtifactStore
    repository: BunshinRepository

    def settle_background_startup_failure(
        self,
        effect: Mapping[str, Any],
        error: Exception,
    ) -> Mapping[str, Any] | None:
        """Triage a role that entered running state before assignment durability.

        Retrying the original outbox effect is unsafe after its business state
        has advanced: the causal guard will correctly supersede the replay, but
        that would otherwise strand the aggregate in a worker-owned state with
        no durable executor.  Once ``ROLE_FAILED`` is legal, record the startup
        failure on the aggregate itself and expose it to ordinary triage.
        """

        route = SEMANTIC_EFFECT_ROUTES.get(str(effect.get("effect_type") or ""))
        role = route.role.value if route is not None and route.role is not None else ""
        error_text = exception_report(error)
        failure_payload = {
            "kind": "role_startup_failed",
            "role": role,
            "attempt_count": 1,
            "error_kind": "pre_assignment_failure",
            "error": error_text,
            "effect_type": str(effect.get("effect_type") or ""),
        }
        failure_ref = self.artifacts.put_json(
            failure_payload,
            artifact_type="RoleAssignmentFailureArtifact",
        )
        for _attempt in range(3):
            snapshot = self.effect_reads.effect_snapshot(effect)
            if snapshot.state == "TRIAGE_REQUIRED":
                self.role_leases.release_background_business_lease(effect)
                return {
                    "provider_request_id": str(
                        effect.get("effect_key") or effect.get("effect_id") or ""
                    ),
                    "status": "triage_required",
                }
            if "ROLE_FAILED" not in self.repository.transitions.legal_actions(
                snapshot.aggregate_type,
                snapshot.state,
            ):
                return None
            try:
                with self.repository.transaction() as connection:
                    (connection or self.repository).transitions.dispatch(
                        ActionEnvelope(
                            action_type="ROLE_FAILED",
                            workflow_id=snapshot.workflow_id,
                            aggregate_type=snapshot.aggregate_type,
                            aggregate_id=snapshot.aggregate_id,
                            actor="bunshin-v2-worker-supervisor",
                            expected_version=snapshot.version,
                            idempotency_key=(
                                f"worker-startup-failed:"
                                f"{str(effect.get('effect_key') or effect.get('effect_id') or '')}:"
                                f"generation-{snapshot.version}"
                            ),
                            payload={
                                "failure_artifact_ref": failure_ref.to_dict(),
                                "blocker": {
                                    "kind": "role_startup_failure",
                                    "summary": error_text,
                                    "role": role,
                                    "attempt_count": 1,
                                },
                            },
                        ),
                    )
                    self.require_cycle_triage(snapshot, unit_of_work=connection)
                self.role_leases.release_background_business_lease(effect)
                return {
                    "provider_request_id": str(
                        effect.get("effect_key") or effect.get("effect_id") or ""
                    ),
                    "status": "triage_required",
                }
            except AggregateVersionConflict:
                continue
        raise DeferredEffectError(
            "role startup failure settlement lost repeated CAS races"
        )

    def settle_background_role_failure(
        self,
        effect: Mapping[str, Any],
        assignment: Mapping[str, Any],
        error: Exception,
        *,
        exhausted: bool,
    ) -> Mapping[str, Any]:
        assignment_id = str(assignment["assignment_id"])
        attempts = self.repository.role_attempts.list_role_attempts(assignment_id)
        charged_failures = max(1, _charged_role_failure_attempt_count(attempts))
        error_text = exception_report(error)
        failure_payload = {
            "kind": "role_assignment_failed",
            "role": str(assignment.get("role") or ""),
            "attempt_count": charged_failures,
            "exhausted": bool(exhausted),
            "error_kind": (
                "attempt_budget_exhausted"
                if exhausted
                else "permanent_role_failure"
            ),
            "error": error_text,
            "effect_type": str(effect.get("effect_type") or ""),
        }
        failure_ref = self.artifacts.put_json(
            failure_payload,
            artifact_type="RoleAssignmentFailureArtifact",
        )
        current_assignment = self.repository.role_assignments.read_role_assignment(assignment_id)
        if current_assignment is None:
            raise SubmissionInvariantError("role assignment disappeared before failure settlement")
        if current_assignment["state"] not in {
            RoleAssignmentState.RESULT_RECORDED.value,
            RoleAssignmentState.SETTLED.value,
        }:
            if not attempts:
                raise SubmissionInvariantError(
                    "role assignment failed before an attempt was durably claimed"
                )
            self.repository.role_retries.record_role_failure_result(
                assignment_id=assignment_id,
                attempt_id_value=str(attempts[-1]["attempt_id"]),
                error_kind=str(failure_payload["error_kind"]),
                error_text=error_text,
                failure_artifact_ref=failure_ref.to_dict(),
                payload_hash=stable_hash(failure_payload),
                settlement_action={
                    "action_type": "ROLE_FAILED",
                    "aggregate_type": str(current_assignment["aggregate_type"]),
                    "aggregate_id": str(current_assignment["aggregate_id"]),
                },
            )
            current_assignment = self.repository.role_assignments.read_role_assignment(assignment_id)
        if current_assignment is None:
            raise SubmissionInvariantError("role failure receipt was not durable")
        for _attempt in range(3):
            snapshot = self.effect_reads.effect_snapshot(effect)
            legal = self.repository.transitions.legal_actions(
                snapshot.aggregate_type,
                snapshot.state,
            )
            if "ROLE_FAILED" not in legal:
                if snapshot.state == "TRIAGE_REQUIRED":
                    self.require_cycle_triage(snapshot)
                    self.repository.role_submissions.settle_role_assignment(
                        assignment_id=assignment_id,
                        submission_payload_hash=str(
                            current_assignment["submission_payload_hash"]
                        ),
                    )
                    return {
                        "provider_request_id": assignment_id,
                        "status": "triage_required",
                    }
                self.repository.role_cancellation.cancel_role_assignments(
                    workflow_id=str(current_assignment["workflow_id"]),
                    aggregate_type=str(current_assignment["aggregate_type"]),
                    aggregate_id=str(current_assignment["aggregate_id"]),
                    reason=f"role failure superseded by parent state {snapshot.state}",
                )
                return {
                    "provider_request_id": assignment_id,
                    "status": "superseded",
                }
            try:
                failure_generation = max(0, int(snapshot.version))
                with self.repository.transaction() as connection:
                    (connection or self.repository).transitions.dispatch(
                        ActionEnvelope(
                            action_type="ROLE_FAILED",
                            workflow_id=snapshot.workflow_id,
                            aggregate_type=snapshot.aggregate_type,
                            aggregate_id=snapshot.aggregate_id,
                            actor="bunshin-v2-worker-supervisor",
                            expected_version=snapshot.version,
                            # One durable receipt may be replayed after an operator
                            # resolves triage.  Deduplicate retries inside the
                            # current aggregate generation without mistaking a
                            # later recovery cycle for the already-settled failure.
                            idempotency_key=(
                                f"worker-failed:{assignment_id}:"
                                f"generation-{failure_generation}"
                            ),
                            payload={
                                "failure_artifact_ref": failure_ref.to_dict(),
                                "blocker": {
                                    "kind": "role_failure",
                                    "summary": error_text,
                                    "role": str(current_assignment.get("role") or ""),
                                    "attempt_count": charged_failures,
                                },
                            },
                        ),
                        role_assignment_id=assignment_id,
                        role_submission_payload_hash=str(
                            current_assignment["submission_payload_hash"]
                        ),
                    )
                    self.require_cycle_triage(
                        snapshot,
                        unit_of_work=connection,
                    )
                return {
                    "provider_request_id": assignment_id,
                    "status": "triage_required",
                }
            except AggregateVersionConflict:
                continue
        raise DeferredEffectError("role failure receipt settlement lost repeated CAS races")

    def require_cycle_triage(
        self,
        snapshot: AggregateSnapshot,
        *,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        coordinator = WorkflowCoordinator(self.repository)
        if snapshot.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
            coordinator.require_plan_triage(
                workflow_id=snapshot.workflow_id,
                unit_of_work=unit_of_work,
            )
        elif snapshot.aggregate_type == AggregateType.DAG_NODE_RUN:
            coordinator.require_node_triage(
                workflow_id=snapshot.workflow_id,
                node_name=str(
                    snapshot.payload.get("module_name")
                    or snapshot.payload.get("unit_id")
                    or ""
                ),
                unit_of_work=unit_of_work,
            )
