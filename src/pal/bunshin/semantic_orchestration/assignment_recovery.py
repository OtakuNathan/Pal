from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from pal.bunshin.background_assignments import BackgroundAssignments
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.role_protocol import RoleAssignmentState
from pal.bunshin.semantic_orchestration.assignment_execution import AssignmentExecution
from pal.bunshin.semantic_orchestration.assignment_identity import AssignmentIdentity
from pal.bunshin.semantic_orchestration.assignment_retries import AssignmentRetries
from pal.bunshin.semantic_orchestration.routes import SEMANTIC_EFFECT_ROUTES


@dataclass
class AssignmentRecovery:
    assignment_execution: AssignmentExecution
    assignment_identity: AssignmentIdentity
    assignment_retries: AssignmentRetries
    background: BackgroundAssignments
    repository: BunshinRepository

    async def recover_background_assignments(self, handlers) -> int:
        if self.background.stopping:
            return 0
        recoverable = sorted(
            self.repository.role_assignments.list_role_assignments(
            states=(
                RoleAssignmentState.QUEUED.value,
                RoleAssignmentState.CLAIMED.value,
                RoleAssignmentState.RUNNING.value,
                RoleAssignmentState.RETRY_QUEUED.value,
                RoleAssignmentState.RESULT_RECORDED.value,
            )
            ),
            key=lambda item: (
                {
                    RoleAssignmentState.RESULT_RECORDED.value: 0,
                    RoleAssignmentState.RUNNING.value: 1,
                    RoleAssignmentState.CLAIMED.value: 1,
                    RoleAssignmentState.RETRY_QUEUED.value: 2,
                    RoleAssignmentState.QUEUED.value: 3,
                }.get(str(item.get("state") or ""), 9),
                str(item.get("created_at") or ""),
                str(item.get("assignment_id") or ""),
            ),
        )
        started = 0
        for assignment in recoverable:
            effect = dict(assignment.get("execution_spec") or {})
            effect_key = str(effect.get("effect_key") or assignment["assignment_key"])
            effect["effect_key"] = effect_key
            effect.setdefault("effect_id", f"assignment:{assignment['assignment_id']}")
            runner = self.runner_for_recovered_effect(effect, handlers)
            if runner is None:
                continue
            existing_worker = self.background.task(effect_key)
            if existing_worker is not None and not existing_worker.done():
                # The live worker owns both the logical effect and its
                # assignment projection. A periodic recovery scan may observe
                # older durable rows for the same effect, but it must never
                # rebind the projection underneath that worker.
                continue
            if self.assignment_retries.role_assignment_attempt_is_live(assignment):
                # Another supervisor still owns the process attempt. This is
                # especially important for reconcile effects, which may run a
                # semantic worker inline rather than through _background_workers.
                # Recovery may take over only after the durable attempt lease
                # expires; otherwise two supervisors can race at submission.
                continue
            disposition = self.assignment_identity.role_assignment_disposition(effect, assignment)
            if disposition:
                self.repository.role_cancellation.cancel_role_assignments(
                    workflow_id=str(assignment["workflow_id"]),
                    aggregate_type=str(assignment["aggregate_type"]),
                    aggregate_id=str(assignment["aggregate_id"]),
                    reason=f"assignment recovery suppressed: {disposition}",
                )
                continue
            task = asyncio.create_task(
                self.assignment_execution.background_worker_loop(effect, runner),
                name=f"bunshin-v2-recovered-{str(assignment['assignment_id'])[-12:]}",
            )
            self.background.recover(effect_key, str(assignment["assignment_id"]), task)
            started += 1
        return started

    def runner_for_recovered_effect(
        self,
        effect: Mapping[str, Any],
        handlers: Mapping[str, Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]],
    ) -> Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None:
        route = SEMANTIC_EFFECT_ROUTES.get(str(effect.get("effect_type") or ""))
        if route is None or not route.background:
            return None
        return handlers[str(effect.get("effect_type") or "")]
