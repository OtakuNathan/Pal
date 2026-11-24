from __future__ import annotations
from dataclasses import dataclass
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.repository import BunshinRepository
from pal.shared import BunshinInvocationPack
from pal.bunshin.semantic_orchestration.role_checkpoints import RoleCheckpoints
from pal.bunshin.role_protocol import RoleAssignmentState
from pal.bunshin.semantic_orchestration.assignment_rules import _assignment_input_fingerprint
from pal.bunshin.semantic_orchestration.role_environment import _refresh_ephemeral_role_reference_binds
from pal.bunshin.harnesses import PAL_HARNESS_ID, pal_harness_spec
from pal.bunshin.contracts import SubmissionInvariantError
from pal.bunshin.semantic_orchestration.attempt_models import AssignmentReuse, BoundRoleHarness, PreparedRoleSession, PreparedRoleWorkspace, RoleAttemptRequest


@dataclass
class HarnessBinding:
    artifacts: ContentAddressedArtifactStore
    repository: BunshinRepository
    role_checkpoints: RoleCheckpoints

    async def execute(
        self, command: RoleAttemptRequest, stage_assignment_reuse: AssignmentReuse,
        stage_role_session: PreparedRoleSession, stage_workspace_preparation: PreparedRoleWorkspace,
    ) -> BoundRoleHarness:
        assignment = stage_role_session.assignment
        durable_prompt_reused = stage_role_session.durable_prompt_reused
        harness_generation = stage_workspace_preparation.harness_generation
        invocation_id = command.invocation_id
        pack = stage_assignment_reuse.pack
        role = stage_workspace_preparation.role
        role_session = stage_role_session.role_session
        input_fingerprint = _assignment_input_fingerprint(assignment)
        harness_spec = harness_generation.select(role)
        if harness_spec != pal_harness_spec():
            raise ValueError("Bunshin role execution requires the built-in Pal harness")
        # A historical Pal session keeps the generation used to encrypt its
        # checkpoint even when the installed Python or worker module moved.
        session_harness_id = str(
            role_session.get("preferred_harness_id") or ""
        ).strip()
        if (session_harness_id not in {"", PAL_HARNESS_ID}
                and assignment["state"] not in {RoleAssignmentState.RESULT_RECORDED.value, RoleAssignmentState.SETTLED.value}):
            raise SubmissionInvariantError("historical external harness session requires reconciliation before Pal can run")
        session_harness_generation = str(
            role_session.get("preferred_harness_generation") or ""
        ).strip()
        completed_harness_attempt = (
            self.repository.role_attempts.read_latest_completed_role_harness_attempt(
                session_id=invocation_id,
                harness_id=harness_spec.harness_id,
            )
        )
        completed_harness_generation = str(
            (completed_harness_attempt or {}).get("harness_generation") or ""
        ).strip()
        if completed_harness_generation:
            effective_harness_generation = completed_harness_generation
        elif (
            session_harness_id == harness_spec.harness_id
            and session_harness_generation
        ):
            effective_harness_generation = session_harness_generation
        else:
            effective_harness_generation = harness_generation.generation_hash
        if assignment["state"] in {
            RoleAssignmentState.QUEUED.value,
            RoleAssignmentState.RETRY_QUEUED.value,
        }:
            original_prompt_ref = self.role_checkpoints.durable_assignment_prompt_ref(assignment)
            prior_attempt = self.repository.role_attempts.read_role_attempt(
                str(assignment.get("active_attempt_id") or "")
            )
            same_harness = (
                prior_attempt is None
                or str(prior_attempt.get("harness_id") or PAL_HARNESS_ID)
                == harness_spec.harness_id
            )
            if original_prompt_ref is not None and same_harness:
                durable_pack = BunshinInvocationPack.from_dict(
                    dict(self.artifacts.read_json(original_prompt_ref))
                )
                pack = _refresh_ephemeral_role_reference_binds(durable_pack, pack)
                durable_prompt_reused = True
        return BoundRoleHarness(
            durable_prompt_reused=durable_prompt_reused, effective_harness_generation=effective_harness_generation,
            harness_spec=harness_spec, input_fingerprint=input_fingerprint, pack=pack,
            pal_checkpoint_capable=True,
        )
