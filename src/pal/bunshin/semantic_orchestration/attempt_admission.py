from __future__ import annotations
from dataclasses import dataclass
from pal.bunshin.storage.role_assignments import semantic_business_lease
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.role_runtime import RoleSupervisor
from pal.bunshin.semantic_orchestration.attempt_models import BoundRoleHarness, ClaimedRoleAttempt, PreparedRoleSession, PreparedRoleWorkspace, RoleAttemptRequest


@dataclass
class AttemptAdmission:
    repository: BunshinV2Repository
    supervisor: RoleSupervisor

    async def execute(
        self, command: RoleAttemptRequest, stage_harness_binding: BoundRoleHarness,
        stage_role_session: PreparedRoleSession, stage_workspace_preparation: PreparedRoleWorkspace,
    ) -> ClaimedRoleAttempt:
        assignment = stage_role_session.assignment
        effective_harness_generation = stage_harness_binding.effective_harness_generation
        harness_spec = stage_harness_binding.harness_spec
        mode = stage_workspace_preparation.mode
        role = stage_workspace_preparation.role
        run_id = stage_workspace_preparation.run_id
        snapshot = command.snapshot
        await self.supervisor.acquire_process_slot(run_id)
        attempt = self.repository.role_assignments.claim_role_assignment(
            str(assignment["assignment_id"]),
            harness_id=harness_spec.harness_id,
            harness_generation=effective_harness_generation,
            business_lease=semantic_business_lease(
                snapshot, owner_id=command.invocation_id, resource_key=command.lease_resource,
                fencing_token=command.fencing_token,
            ) or None,
        )
        assignment_lease_resource = f"assignment:{assignment['assignment_id']}"
        assignment_lease = self.repository.leases.claim_lease(
            assignment_lease_resource,
            str(attempt["attempt_id"]),
            ttl_seconds=120,
            metadata={
                "workflow_id": snapshot.workflow_id,
                "aggregate_type": snapshot.aggregate_type.value,
                "aggregate_id": snapshot.aggregate_id,
                "role": role,
                "mode": mode,
            },
        )
        return ClaimedRoleAttempt(assignment_lease=assignment_lease, assignment_lease_resource=assignment_lease_resource, attempt=attempt)
