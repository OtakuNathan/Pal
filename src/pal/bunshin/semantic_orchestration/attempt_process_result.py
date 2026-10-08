from __future__ import annotations
from dataclasses import dataclass
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.semantic_orchestration.role_policy import _role_primary_artifact_name
from pal.bunshin.semantic_orchestration.role_checkpoints import RoleCheckpoints
import contextlib
from pal.bunshin.semantic_orchestration.worker_results import _meaningful_stderr_tail
from pal.bunshin.semantic_orchestration.worker_results import _worker_terminal_failure
from pal.bunshin.semantic_orchestration.worker_results import _worker_stderr_failures
from pal.bunshin.contracts import PermanentEffectError
from pal.bunshin.semantic_orchestration.attempt_models import (
    BoundRoleHarness, ClaimedRoleAttempt, CollectedRoleTerminal, ExitedRoleProcess, MaterializedRolePack,
    PreparedRoleSession, PublishedRoleAttempt, RoleAttemptRequest,
)


@dataclass
class ProcessResult:
    repository: BunshinRepository
    role_checkpoints: RoleCheckpoints

    async def execute(
        self, command: RoleAttemptRequest, stage_attempt_admission: ClaimedRoleAttempt,
        stage_attempt_pack: MaterializedRolePack, stage_attempt_publication: PublishedRoleAttempt,
        stage_harness_binding: BoundRoleHarness, stage_role_session: PreparedRoleSession,
        stage_worker_execution: ExitedRoleProcess,
    ) -> CollectedRoleTerminal:
        assignment = stage_role_session.assignment
        assignment_lease = stage_attempt_admission.assignment_lease
        assignment_lease_resource = stage_attempt_admission.assignment_lease_resource
        attempt = stage_attempt_admission.attempt
        continuation_output_path = stage_attempt_pack.continuation_output_path
        events = stage_worker_execution.events
        fencing_token = command.fencing_token
        invocation_id = command.invocation_id
        owner = stage_worker_execution.owner
        pack = stage_attempt_publication.pack
        pal_checkpoint_capable = stage_harness_binding.pal_checkpoint_capable
        worker_error = stage_worker_execution.worker_error
        stderr = owner.stderr
        assignment_after_process = self.repository.role_assignments.read_role_assignment(
            str(assignment["assignment_id"])
        )
        has_submission_receipt = bool(
            dict((assignment_after_process or {}).get("submission_artifact_ref") or {})
        )
        if owner.returncode != 0 and not has_submission_receipt:
            stderr_text = stderr.decode("utf-8", errors="replace")
            error_tail = _meaningful_stderr_tail(stderr_text)
            fallback_events, fallback_worker_error = _worker_stderr_failures(stderr_text)
            failure_events = events
            if not any(item.get("event_kind") == "terminal" for item in events):
                failure_events = [*events, *fallback_events]
            worker_error = worker_error or fallback_worker_error
            terminal_error_kind, terminal_error, retry_directive = (
                _worker_terminal_failure(failure_events)
            )
            details = (
                terminal_error
                or worker_error
                or error_tail
                or "worker emitted no structured error"
            )
            secondary_error = worker_error or error_tail
            if terminal_error and secondary_error and secondary_error not in terminal_error:
                details = f"{terminal_error}\nWorker process error: {secondary_error}"
            permanent = retry_directive == "do_not_retry"
            if not permanent:
                self.repository.role_retries.queue_role_attempt_retry(
                    assignment_id=str(assignment["assignment_id"]),
                    attempt_id_value=str(attempt["attempt_id"]),
                    error_kind=terminal_error_kind or "worker_process_failed",
                    error_text=details,
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
            if permanent:
                raise PermanentEffectError(details)
            raise RuntimeError(f"V2 worker exited {owner.returncode}: {details}")
        terminal = next((item for item in reversed(events) if str(item.get("event_kind") or "") == "terminal"), None)
        if terminal is None and has_submission_receipt:
            terminal = self.role_checkpoints.terminal_from_assignment_receipt(
                dict(assignment_after_process or {}),
                primary_artifact_name=_role_primary_artifact_name(pack),
                summary="Recovered a durable submission after the role process ended.",
            )
        if terminal is None:
            self.repository.role_retries.queue_role_attempt_retry(
                assignment_id=str(assignment["assignment_id"]),
                attempt_id_value=str(attempt["attempt_id"]),
                error_kind="missing_terminal_and_receipt",
                error_text="worker ended without terminal event or durable submission receipt",
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
            raise RuntimeError("V2 semantic worker ended without terminal event")
        terminal_payload = dict(terminal.get("payload") or {})
        return CollectedRoleTerminal(terminal=terminal, terminal_payload=terminal_payload)
