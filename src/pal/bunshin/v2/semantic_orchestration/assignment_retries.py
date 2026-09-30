from __future__ import annotations
from pal.bunshin.v2.semantic_orchestration.workspace_safety import _lease_is_live
import contextlib
from dataclasses import dataclass
from typing import Any, Mapping
from pal.bunshin.v2.contracts import AggregateSnapshot, SubmissionInvariantError
from pal.bunshin.v2.background_assignments import BackgroundAssignments
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.role_protocol import RoleAssignmentState


@dataclass
class AssignmentRetries:
    background: BackgroundAssignments
    repository: BunshinV2Repository

    def queue_active_assignment_retry(
        self,
        assignment: Mapping[str, Any],
        *,
        error_kind: str,
        error_text: str,
    ) -> dict[str, Any]:
        attempt_id_value = str(assignment.get("active_attempt_id") or "")
        if not attempt_id_value:
            return dict(assignment)
        attempt = self.repository.role_attempts.read_role_attempt(attempt_id_value)
        if attempt is None:
            return dict(assignment)
        lease_resource = str(attempt.get("lease_resource_key") or "")
        fencing_token = int(attempt.get("fencing_token") or 0)
        updated = self.repository.role_retries.queue_role_attempt_retry(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=attempt_id_value,
            error_kind=error_kind,
            error_text=error_text,
        )
        if lease_resource and fencing_token:
            with contextlib.suppress(Exception):
                self.repository.leases.release_lease(
                    lease_resource,
                    attempt_id_value,
                    fencing_token,
                )
        return updated

    def queue_interrupted_assignment_retry(self, effect_key: str) -> None:
        assignment_id = self.background.assignment_id(effect_key, "")
        if not assignment_id:
            return
        assignment = self.repository.role_assignments.read_role_assignment(assignment_id)
        if assignment is None or assignment["state"] not in {
            RoleAssignmentState.CLAIMED.value,
            RoleAssignmentState.RUNNING.value,
        }:
            return
        self.queue_active_assignment_retry(
            assignment,
            error_kind="manager_shutdown",
            error_text="manager stopped before the role assignment settled",
        )

    def retry_assignment_for_effect(
        self,
        effect: Mapping[str, Any],
        *,
        snapshot: AggregateSnapshot,
        role: str,
        mode: str,
        submission_kind: str,
    ) -> dict[str, Any] | None:
        """Return the durable assignment owned by this logical effect retry.

        A process attempt may fail after many completed model/tool rounds.  The
        supervisor must claim another attempt on the same assignment, prompt
        pack, and cognitive session.  Recompiling attempt-local workspace/LSP
        projections into a new assignment changes its input fingerprint and
        makes the model see a fresh job.
        """

        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "")
        assignment_id = self.background.assignment_id(effect_key, "")
        assignment = (
            self.repository.role_assignments.read_role_assignment(assignment_id)
            if assignment_id
            else None
        )
        if assignment is None and effect_key:
            # The effect-to-assignment index is intentionally an in-memory
            # accelerator.  After a manager restart the durable assignment is
            # still the logical coroutine's owner, so recover it from the
            # execution spec instead of creating a second assignment in the
            # same role session.
            candidates: list[dict[str, Any]] = []
            for candidate in self.repository.role_assignments.list_role_assignments(
                workflow_id=snapshot.workflow_id
            ):
                execution_spec = dict(candidate.get("execution_spec") or {})
                if str(
                    execution_spec.get("effect_key")
                    or execution_spec.get("effect_id")
                    or ""
                ) != effect_key:
                    continue
                candidates.append(dict(candidate))
            if len(candidates) > 1:
                raise SubmissionInvariantError(
                    "logical role effect has multiple durable assignments"
                )
            if candidates:
                assignment = candidates[0]
                assignment_id = str(assignment.get("assignment_id") or "")
                if assignment_id:
                    self.background.bind(effect_key, assignment_id)
        if assignment is None:
            return None
        expected = (
            snapshot.workflow_id,
            snapshot.aggregate_type.value,
            snapshot.aggregate_id,
            str(role),
            str(mode),
            str(submission_kind),
        )
        actual = (
            str(assignment.get("workflow_id") or ""),
            str(assignment.get("aggregate_type") or ""),
            str(assignment.get("aggregate_id") or ""),
            str(assignment.get("role") or ""),
            str(assignment.get("mode") or ""),
            str(assignment.get("submission_kind") or ""),
        )
        if actual != expected:
            raise SubmissionInvariantError(
                "logical role effect retry resolved to a different durable assignment"
            )
        if str(assignment.get("state") or "") not in {
            RoleAssignmentState.QUEUED.value,
            RoleAssignmentState.CLAIMED.value,
            RoleAssignmentState.RUNNING.value,
            RoleAssignmentState.RETRY_QUEUED.value,
            RoleAssignmentState.RESULT_RECORDED.value,
            RoleAssignmentState.SETTLED.value,
        }:
            return None
        return dict(assignment)

    def role_assignment_attempt_is_live(
        self,
        assignment: Mapping[str, Any],
    ) -> bool:
        attempt_id_value = str(assignment.get("active_attempt_id") or "")
        if not attempt_id_value:
            return False
        attempt = self.repository.role_attempts.read_role_attempt(attempt_id_value)
        if attempt is None:
            return False
        resource = str(attempt.get("lease_resource_key") or "")
        token = int(attempt.get("fencing_token") or 0)
        if not resource or token <= 0:
            return False
        lease = self.repository.leases.read_lease(resource)
        return bool(
            lease is not None
            and str(lease.get("owner_id") or "") == attempt_id_value
            and int(lease.get("fencing_token") or 0) == token
            and _lease_is_live(lease)
        )
