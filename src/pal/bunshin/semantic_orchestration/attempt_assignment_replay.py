from __future__ import annotations
import pal.bunshin.turns as _dependency_turns
from dataclasses import dataclass
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.role_contracts import OrchestrationRole
from pal.shared import BunshinInvocationPack
from pal.bunshin.semantic_orchestration.role_policy import _role_primary_artifact_name
from pal.bunshin.contracts import SubmissionInvariantError
from pal.bunshin.background_assignments import BackgroundAssignments
from pal.bunshin.semantic_orchestration.role_checkpoints import RoleCheckpoints
from pal.bunshin.role_protocol import RoleAssignmentState
from pal.bunshin.semantic_orchestration.attempt_models import (
    AttemptReplay, BoundRoleHarness, PreparedRoleSession, PreparedRoleWorkspace, RoleAttemptRequest,
    RunnableRoleAssignment,
)


@dataclass
class AssignmentReplay:
    artifacts: ContentAddressedArtifactStore
    background: BackgroundAssignments
    role_checkpoints: RoleCheckpoints

    async def execute(
        self, command: RoleAttemptRequest, stage_harness_binding: BoundRoleHarness,
        stage_role_session: PreparedRoleSession, stage_workspace_preparation: PreparedRoleWorkspace,
    ) -> RunnableRoleAssignment | AttemptReplay:
        activation = command.activation
        assignment = stage_role_session.assignment
        bound_reference_refs = stage_workspace_preparation.bound_reference_refs
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
            prompt_ref = self.role_checkpoints.durable_assignment_prompt_ref(assignment)
            if prompt_ref is None:
                if activation.role == OrchestrationRole.VERIFIER:
                    raise SubmissionInvariantError(
                        "durable verifier receipt lost its original prompt workspace binding"
                    )
                pack = _dependency_turns.sanitize_runner_session_pack(pack)
                prompt_ref = self.artifacts.put_json(
                    pack.to_dict(),
                    artifact_type="RolePromptPackArtifact",
                    child_refs=tuple(
                        (ref.sha256, name)
                        for name, ref in bound_reference_refs.items()
                        if ref.artifact_type != "LocalPathReference"
                    ),
                )
            terminal = self.role_checkpoints.terminal_from_assignment_receipt(
                assignment,
                primary_artifact_name=_role_primary_artifact_name(pack),
                summary="Reconciled the exact durable role submission receipt.",
            )
            terminal_ref = self.artifacts.put_json(
                terminal,
                artifact_type="RoleTerminalArtifact",
                child_refs=(
                    (prompt_ref.sha256, "prompt_pack"),
                    (
                        str(dict(assignment["submission_artifact_ref"])["sha256"]),
                        "submission_receipt",
                    ),
                ),
            )
            return AttemptReplay((terminal, prompt_ref, terminal_ref))

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
