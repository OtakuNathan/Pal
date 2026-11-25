from __future__ import annotations
from dataclasses import dataclass
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.shared import BunshinInvocationPack
from pal.bunshin.semantic_orchestration.role_policy import _role_primary_artifact_name
from pal.bunshin.contracts import AggregateType, SubmissionInvariantError
from pal.bunshin.role_contracts import validate_family_binding_payload
from typing import Any, Mapping
from pal.bunshin.background_assignments import BackgroundAssignments
from pal.bunshin.semantic_orchestration.role_checkpoints import RoleCheckpoints
from pal.bunshin.role_protocol import RoleAssignmentState
from pal.bunshin.semantic_orchestration.attempt_models import (
    AttemptReplay, BoundRoleHarness, PreparedRoleSession, PreparedRoleWorkspace, RoleAttemptRequest,
    RunnableRoleAssignment,
)


def replay_assignment_receipt(
    artifacts: ContentAddressedArtifactStore, checkpoints: RoleCheckpoints,
    assignment: Mapping[str, Any], *, summary: str,
) -> AttemptReplay:
    checkpoints.require_success_receipt(assignment)
    prompt_ref = checkpoints.durable_assignment_prompt_ref(assignment)
    if prompt_ref is None:
        raise SubmissionInvariantError("durable role receipt lost its original prompt binding")
    pack = BunshinInvocationPack.from_dict(dict(artifacts.read_json(prompt_ref)))
    try:
        primary = _role_primary_artifact_name(pack)
    except ValueError:
        # Older packs may lack the output-policy projection. The pinned
        # Family, not a newly prepared workspace, still owns that contract.
        workflow = checkpoints.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, str(assignment["workflow_id"]))
        binding_ref = dict(workflow.payload.get("family_binding_ref") or {}) if workflow else {}
        if binding_ref.get("sha256") != assignment.get("family_binding_sha"):
            raise SubmissionInvariantError("durable role receipt lost its pinned Family binding")
        binding = validate_family_binding_payload(artifacts.read_json(binding_ref)) if binding_ref else {}
        profile = dict(dict(binding.get(str(assignment["role"])) or {}).get("role_profile") or {})
        primary = str(dict(profile.get("output_policy") or {}).get("primary_artifact") or "")
        if not primary:
            raise SubmissionInvariantError("durable role receipt lost its pinned output contract")
    terminal = checkpoints.terminal_from_assignment_receipt(
        assignment, primary_artifact_name=primary, summary=summary,
    )
    terminal_ref = artifacts.put_json(
        terminal, artifact_type="RoleTerminalArtifact", child_refs=(
            (prompt_ref.sha256, "prompt_pack"),
            (str(dict(assignment["submission_artifact_ref"])["sha256"]), "submission_receipt"),
        ),
    )
    return AttemptReplay((terminal, prompt_ref, terminal_ref))


@dataclass
class AssignmentReplay:
    artifacts: ContentAddressedArtifactStore
    background: BackgroundAssignments
    role_checkpoints: RoleCheckpoints

    def recorded_result(self, command: RoleAttemptRequest, assignment: Mapping[str, Any]) -> AttemptReplay:
        """Replay an exact receipt without preparing a new worker or workspace."""
        self.background.signal_ready(command.effect, str(assignment["assignment_id"]))
        replay = replay_assignment_receipt(
            self.artifacts, self.role_checkpoints, assignment,
            summary="Reconciled the exact durable role submission receipt.",
        )
        return replay

    async def execute(
        self, command: RoleAttemptRequest, stage_harness_binding: BoundRoleHarness,
        stage_role_session: PreparedRoleSession, stage_workspace_preparation: PreparedRoleWorkspace,
    ) -> RunnableRoleAssignment | AttemptReplay:
        assignment = stage_role_session.assignment
        durable_prompt_reused = stage_harness_binding.durable_prompt_reused
        effect = command.effect
        invocation_id = command.invocation_id
        pack = stage_harness_binding.pack
        request = stage_workspace_preparation.request
        snapshot = command.snapshot
        self.background.signal_ready(effect, str(assignment["assignment_id"]))

        if assignment["state"] in {
            RoleAssignmentState.RESULT_RECORDED.value,
            RoleAssignmentState.SETTLED.value,
        }:
            return self.recorded_result(command, assignment)

        if assignment["state"] not in {
            RoleAssignmentState.QUEUED.value,
            RoleAssignmentState.RETRY_QUEUED.value,
        }:
            raise SubmissionInvariantError(
                f"role assignment cannot start from {assignment['state']}"
            )
        if not durable_prompt_reused:
            pack_value = pack.to_dict()
            metadata = dict(pack_value.get("metadata") or {})
            metadata["initial_skill_injections"] = self.role_checkpoints.role_session_skill_injections(
                request=request,
                workflow_id=snapshot.workflow_id,
                session_id=invocation_id,
            )
            pack = BunshinInvocationPack.from_dict(
                {
                    **pack_value,
                    "metadata": metadata,
                }
            )
        return RunnableRoleAssignment(pack=pack)
