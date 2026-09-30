from __future__ import annotations
from dataclasses import dataclass
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.contracts import SubmissionInvariantError
from pal.bunshin.v2.semantic_orchestration.role_checkpoints import RoleCheckpoints
from pal.bunshin.v2.contracts import DeferredEffectError
from pal.bunshin.v2.role_protocol import RoleAssignmentState
import contextlib
from pal.bunshin.v2.contracts import PermanentEffectError
from pal.bunshin.v2.semantic_orchestration.attempt_models import (
    BoundRoleHarness, ClaimedRoleAttempt, CollectedRoleTerminal, MaterializedRolePack, PreparedRoleSession,
    RoleAttemptRequest, ValidatedRoleTerminal,
)


@dataclass
class TerminalValidation:
    repository: BunshinV2Repository
    role_checkpoints: RoleCheckpoints

    async def execute(
        self, command: RoleAttemptRequest, stage_attempt_admission: ClaimedRoleAttempt,
        stage_attempt_pack: MaterializedRolePack, stage_harness_binding: BoundRoleHarness,
        stage_process_result: CollectedRoleTerminal, stage_role_session: PreparedRoleSession,
    ) -> ValidatedRoleTerminal:
        assignment = stage_role_session.assignment
        assignment_lease = stage_attempt_admission.assignment_lease
        assignment_lease_resource = stage_attempt_admission.assignment_lease_resource
        attempt = stage_attempt_admission.attempt
        continuation_output_path = stage_attempt_pack.continuation_output_path
        fencing_token = command.fencing_token
        invocation_id = command.invocation_id
        pal_checkpoint_capable = stage_harness_binding.pal_checkpoint_capable
        terminal_payload = stage_process_result.terminal_payload
        if (
            str(terminal_payload.get("status") or "") == "suspended"
            and bool(terminal_payload.get("manager_restart"))
        ):
            self.repository.role_retries.queue_role_attempt_retry(
                assignment_id=str(assignment["assignment_id"]),
                attempt_id_value=str(attempt["attempt_id"]),
                error_kind="manager_restart",
                error_text=str(
                    terminal_payload.get("summary")
                    or "worker deferred for manager restart"
                ),
            )
            with contextlib.suppress(Exception):
                self.repository.leases.release_lease(
                    assignment_lease_resource,
                    str(attempt["attempt_id"]),
                    assignment_lease.fencing_token,
                )
            checkpoint = self.role_checkpoints.publish_agent_session_checkpoint(
                invocation_id,
                assignment_lease.fencing_token,
                continuation_output_path,
            )
            if checkpoint is None:
                raise RuntimeError(
                    "worker reached a manager-restart safe point without a durable continuation"
                )
            self.repository.role_invocations.suspend_role_invocation(
                invocation_id=invocation_id,
                fencing_token=fencing_token,
                status="interrupted",
            )
            raise DeferredEffectError(
                str(terminal_payload.get("summary") or "worker deferred for manager restart")
            )
        if str(terminal_payload.get("status") or "") != "completed":
            summary = str(terminal_payload.get("summary") or "V2 semantic worker failed")
            completion_stalled = (
                str(terminal_payload.get("blocker_kind") or "")
                == "completion_gate_stalled"
            )
            if not completion_stalled:
                self.repository.role_retries.queue_role_attempt_retry(
                    assignment_id=str(assignment["assignment_id"]),
                    attempt_id_value=str(attempt["attempt_id"]),
                    error_kind="worker_terminal_failed",
                    error_text=summary,
                )
            with contextlib.suppress(Exception):
                self.repository.leases.release_lease(
                    assignment_lease_resource,
                    str(attempt["attempt_id"]),
                    assignment_lease.fencing_token,
                )
            checkpoint = self.role_checkpoints.publish_agent_session_checkpoint(
                invocation_id,
                assignment_lease.fencing_token,
                continuation_output_path,
            )
            if checkpoint is not None and pal_checkpoint_capable:
                self.repository.role_invocations.suspend_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    status="interrupted",
                )
            else:
                self.repository.role_invocations.finish_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    status="failed",
                )
            if completion_stalled:
                raise PermanentEffectError(summary)
            raise RuntimeError(summary)
        assignment_after_process = self.repository.role_assignments.read_role_assignment(
            str(assignment["assignment_id"])
        )
        if assignment_after_process is None or assignment_after_process["state"] not in {
            RoleAssignmentState.RESULT_RECORDED.value,
            RoleAssignmentState.SETTLED.value,
        }:
            with contextlib.suppress(Exception):
                self.repository.leases.release_lease(
                    assignment_lease_resource,
                    str(attempt["attempt_id"]),
                    assignment_lease.fencing_token,
                )
            checkpoint = self.role_checkpoints.publish_agent_session_checkpoint(
                invocation_id,
                assignment_lease.fencing_token,
                continuation_output_path,
            )
            if checkpoint is not None and pal_checkpoint_capable:
                self.repository.role_invocations.suspend_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    status="interrupted",
                )
            else:
                self.repository.role_invocations.finish_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    status="failed",
                )
            raise SubmissionInvariantError(
                "role participant reported completion before its durable submission receipt"
            )
        with contextlib.suppress(Exception):
            self.repository.leases.release_lease(
                assignment_lease_resource,
                str(attempt["attempt_id"]),
                assignment_lease.fencing_token,
            )
        return ValidatedRoleTerminal(assignment_after_process=assignment_after_process)
