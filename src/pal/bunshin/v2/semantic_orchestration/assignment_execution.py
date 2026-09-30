from __future__ import annotations
from pal.bunshin.v2.semantic_orchestration.assignment_rules import _ROLE_FAILURE_ATTEMPT_LIMIT
from pal.bunshin.v2.semantic_orchestration.assignment_rules import _charged_role_failure_attempt_count
from pal.bunshin.v2.semantic_orchestration.assignment_rules import _assignment_has_durable_submission
from pal.bunshin.v2.semantic_orchestration.assignment_rules import _is_transient_sqlite_lock
import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from pal.bunshin.v2.contracts import DeferredEffectError, PermanentEffectError, SubmissionInvariantError
from pal.bunshin.v2.background_assignments import BackgroundAssignments
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.role_protocol import RoleAssignmentState
from pal.bunshin.v2.semantic_orchestration.assignment_failures import AssignmentFailures
from pal.bunshin.v2.semantic_orchestration.assignment_identity import AssignmentIdentity
from pal.bunshin.v2.semantic_orchestration.assignment_retries import AssignmentRetries
from pal.bunshin.v2.semantic_orchestration.role_leases import RoleLeases


@dataclass
class AssignmentExecution:
    assignment_failures: AssignmentFailures
    assignment_identity: AssignmentIdentity
    assignment_retries: AssignmentRetries
    role_leases: RoleLeases
    background: BackgroundAssignments
    repository: BunshinV2Repository

    async def background_worker_loop(
        self,
        effect: Mapping[str, Any],
        runner: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
    ) -> Mapping[str, Any]:
        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "")
        supervisor_failures = 0
        while True:
            if self.background.stopping:
                assignment_id = self.background.assignment_id(effect_key, "")
                if not assignment_id:
                    raise DeferredEffectError(
                        "worker supervisor stopped before creating a durable assignment"
                    )
                return {
                    "provider_request_id": assignment_id,
                    "status": "suspended",
                }
            assignment_id = self.background.assignment_id(effect_key, "")
            if assignment_id:
                assignment = self.repository.role_assignments.read_role_assignment(assignment_id)
                if assignment is not None:
                    disposition = self.assignment_identity.role_assignment_disposition(effect, assignment)
                    if disposition:
                        self.role_leases.release_background_business_lease(effect)
                        return {
                            "provider_request_id": assignment_id,
                            "status": disposition,
                        }
            try:
                result = await runner(effect)
                assignment_id = self.background.assignment_id(effect_key, "")
                if assignment_id:
                    assignment = self.repository.role_assignments.read_role_assignment(assignment_id)
                    if (
                        assignment is not None
                        and assignment["state"]
                        == RoleAssignmentState.RESULT_RECORDED.value
                    ):
                        raise SubmissionInvariantError(
                            "worker business action returned without atomically settling "
                            "its durable submission"
                        )
                return result
            except asyncio.CancelledError:
                raise
            except DeferredEffectError:
                assignment_id = self.background.assignment_id(effect_key, "")
                if not assignment_id:
                    raise
                self.role_leases.release_background_business_lease(effect)
                return {
                    "provider_request_id": assignment_id,
                    "status": "suspended",
                }
            except Exception as exc:
                supervisor_failures += 1
                assignment_id = self.background.assignment_id(effect_key, "")
                if not assignment_id:
                    if not isinstance(exc, DeferredEffectError):
                        startup_failure = self.assignment_failures.settle_background_startup_failure(
                            effect,
                            exc,
                        )
                        if startup_failure is not None:
                            return startup_failure
                    raise
                assignment = self.repository.role_assignments.read_role_assignment(assignment_id)
                if assignment is None:
                    raise
                disposition = self.assignment_identity.role_assignment_disposition(effect, assignment)
                if disposition:
                    self.role_leases.release_background_business_lease(effect)
                    return {
                        "provider_request_id": assignment_id,
                        "status": disposition,
                    }
                permanent = isinstance(exc, PermanentEffectError)
                if assignment["state"] in {
                    RoleAssignmentState.CLAIMED.value,
                    RoleAssignmentState.RUNNING.value,
                } and not permanent:
                    assignment = self.assignment_retries.queue_active_assignment_retry(
                        assignment,
                        error_kind="worker_supervisor_failure",
                        error_text=f"{exc.__class__.__name__}: {exc}",
                    )
                attempts = self.repository.role_attempts.list_role_attempts(assignment_id)
                charged_failures = _charged_role_failure_attempt_count(attempts)
                if self.background.stopping:
                    self.role_leases.release_background_business_lease(effect)
                    return {
                        "provider_request_id": assignment_id,
                        "status": "suspended",
                    }
                if (
                    assignment["state"] in {
                        RoleAssignmentState.QUEUED.value,
                        RoleAssignmentState.RETRY_QUEUED.value,
                        RoleAssignmentState.RESULT_RECORDED.value,
                        RoleAssignmentState.SETTLED.value,
                    }
                    and max(charged_failures, supervisor_failures)
                    < _ROLE_FAILURE_ATTEMPT_LIMIT
                    and not permanent
                ):
                    # A recorded submission is already the durable LLM result.
                    # Replay the role wrapper so it can reconcile the same
                    # receipt with its business Action; _run_profile will not
                    # invoke the model again for this assignment state.
                    self.role_leases.release_background_business_lease(effect)
                    await asyncio.sleep(5.0)
                    continue
                if (
                    _assignment_has_durable_submission(assignment)
                    and _is_transient_sqlite_lock(exc)
                ):
                    # The model output is already durable and recovery can
                    # replay it without another LLM turn.  A transient
                    # repository lock after that boundary is reconciliation
                    # debt, not evidence that the role failed.
                    self.role_leases.release_background_business_lease(effect)
                    return {
                        "provider_request_id": assignment_id,
                        "status": "reconciliation_deferred",
                    }
                self.role_leases.release_background_business_lease(effect)
                return self.assignment_failures.settle_background_role_failure(
                    effect,
                    assignment,
                    exc,
                    exhausted=not permanent,
                )
