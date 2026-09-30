from __future__ import annotations
from dataclasses import dataclass
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.contracts import SubmissionInvariantError
from pal.bunshin.v2.semantic_orchestration.assignment_retries import AssignmentRetries
from pal.bunshin.v2.semantic_orchestration.role_inputs import _role_session_scope
from pal.bunshin.v2.contracts import DeferredEffectError
from pal.bunshin.v2.role_protocol import RoleAssignmentRequest, RoleAssignmentState
from pal.bunshin.v2.semantic_orchestration.attempt_models import (
    AssignmentReuse, BoundRoleReferences, InitialRolePrompt, PreparedRoleSession, PreparedRoleWorkspace,
    RoleAttemptRequest,
)


@dataclass
class RoleSession:
    assignment_retries: AssignmentRetries
    repository: BunshinV2Repository

    async def execute(
        self, command: RoleAttemptRequest, stage_assignment_reuse: AssignmentReuse,
        stage_prompt_construction: InitialRolePrompt, stage_reference_binding: BoundRoleReferences,
        stage_workspace_preparation: PreparedRoleWorkspace,
    ) -> PreparedRoleSession:
        activation = command.activation
        assignment = stage_assignment_reuse.assignment
        binding_ref = stage_workspace_preparation.binding_ref
        durable_input_refs = stage_assignment_reuse.durable_input_refs
        effect = command.effect
        evaluation_generation = stage_reference_binding.evaluation_generation
        harness_generation = stage_workspace_preparation.harness_generation
        input_fingerprint = stage_prompt_construction.input_fingerprint
        invocation_id = command.invocation_id
        mode = stage_workspace_preparation.mode
        preferred_harness = stage_workspace_preparation.preferred_harness
        profile = command.profile
        role = stage_workspace_preparation.role
        snapshot = command.snapshot
        submission_kind = stage_assignment_reuse.submission_kind
        session_scope_kind, session_subject_key = _role_session_scope(snapshot, activation)
        role_session = self.repository.role_sessions.ensure_role_session(
            session_id=invocation_id,
            workflow_id=snapshot.workflow_id,
            aggregate_type=snapshot.aggregate_type,
            aggregate_id=snapshot.aggregate_id,
            role=role,
            mode=mode,
            role_profile_id=profile,
            family_binding_sha=str(binding_ref.get("sha256") or ""),
            preferred_harness_id=preferred_harness.harness_id,
            preferred_harness_generation=(
                harness_generation.generation_hash
            ),
            scope_kind=session_scope_kind,
            subject_key=session_subject_key,
        )
        durable_prompt_reused = False
        if assignment is None:
            try:
                assignment = self.repository.role_assignments.create_role_assignment(
                    RoleAssignmentRequest(
                        assignment_key=(
                            f"{str(effect.get('effect_key') or effect.get('effect_id') or '')}:"
                            f"{role}:{mode}:{input_fingerprint}"
                        ),
                        session_id=invocation_id,
                        workflow_id=snapshot.workflow_id,
                        aggregate_type=snapshot.aggregate_type.value,
                        aggregate_id=snapshot.aggregate_id,
                        role=role,
                        mode=mode,
                        role_profile_id=profile,
                        family_binding_sha=str(binding_ref.get("sha256") or ""),
                        input_fingerprint=input_fingerprint,
                        required_inputs=(),
                        input_refs=durable_input_refs,
                        execution_spec={
                            "effect_type": str(effect.get("effect_type") or "run_role"),
                            "effect_id": str(effect.get("effect_id") or ""),
                            "effect_key": str(effect.get("effect_key") or ""),
                            "workflow_id": snapshot.workflow_id,
                            "aggregate_type": snapshot.aggregate_type.value,
                            "aggregate_id": snapshot.aggregate_id,
                            "role": role,
                            "payload": dict(effect.get("payload") or {}),
                            "evaluation_generation": evaluation_generation,
                        },
                        submission_kind=submission_kind,
                    )
                )
            except ValueError as exc:
                # Triage/rebind cancellation and assignment creation are
                # separate durable effects.  During that small window the
                # previous assignment is still visible as open; this is a
                # retryable ordering race, not a role failure.
                if "role session already has an open assignment" in str(exc):
                    raise DeferredEffectError(str(exc)) from exc
                raise
        elif assignment["state"] in {
            RoleAssignmentState.CLAIMED.value,
            RoleAssignmentState.RUNNING.value,
        }:
            if self.assignment_retries.role_assignment_attempt_is_live(assignment):
                raise DeferredEffectError("role assignment already has a live process attempt")
            assignment = self.assignment_retries.queue_active_assignment_retry(
                assignment,
                error_kind="attempt_lease_expired",
                error_text="role attempt lease expired before submission settlement",
            )
            if assignment["state"] != RoleAssignmentState.RETRY_QUEUED.value:
                raise SubmissionInvariantError(
                    "expired active role assignment could not be made retryable"
                )
        return PreparedRoleSession(
            assignment=assignment, durable_prompt_reused=durable_prompt_reused, role_session=role_session,
            session_scope_kind=session_scope_kind, session_subject_key=session_subject_key,
        )
