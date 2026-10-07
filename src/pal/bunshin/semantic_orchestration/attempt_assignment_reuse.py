from __future__ import annotations
import pal.bunshin.turns as _dependency_turns
from dataclasses import dataclass
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.role_contracts import OrchestrationRole
from pal.bunshin.semantic_orchestration.role_environment import _role_submission_kind
from pal.bunshin.semantic_orchestration.role_policy import _role_primary_artifact_name
from pal.bunshin.turns import sanitize_runner_session_pack
from pal.bunshin.contracts import SubmissionInvariantError
from pal.bunshin.background_assignments import BackgroundAssignments
from pal.bunshin.semantic_orchestration.assignment_identity import AssignmentIdentity
from pal.bunshin.semantic_orchestration.assignment_retries import AssignmentRetries
from pal.bunshin.semantic_orchestration.role_checkpoints import RoleCheckpoints
from pal.bunshin.semantic_orchestration.attempt_models import (
    AssignmentReuse as AssignmentReuseResult, AttemptReplay, BoundRoleReferences, PreparedRolePrompt,
    PreparedRoleWorkspace, RoleAttemptRequest,
)


@dataclass
class AssignmentReuse:
    artifacts: ContentAddressedArtifactStore
    assignment_identity: AssignmentIdentity
    assignment_retries: AssignmentRetries
    background: BackgroundAssignments
    repository: BunshinRepository
    role_checkpoints: RoleCheckpoints

    async def execute(
        self, command: RoleAttemptRequest, stage_reference_binding: BoundRoleReferences,
        stage_tool_policy: PreparedRolePrompt, stage_workspace_preparation: PreparedRoleWorkspace,
    ) -> AssignmentReuseResult | AttemptReplay:
        activation = command.activation
        bound_reference_refs = stage_workspace_preparation.bound_reference_refs
        contract_authoring = stage_workspace_preparation.contract_authoring
        effect = command.effect
        evaluation_generation = stage_reference_binding.evaluation_generation
        mode = stage_workspace_preparation.mode
        pack = stage_tool_policy.pack
        role = stage_workspace_preparation.role
        snapshot = command.snapshot
        submission_kind = _role_submission_kind(
            activation,
            contract_authoring=contract_authoring,
        )
        durable_input_refs = {
            name: ref.to_dict()
            for name, ref in bound_reference_refs.items()
            if ref.artifact_type != "LocalPathReference"
        }
        assignment = self.assignment_retries.retry_assignment_for_effect(
            effect,
            snapshot=snapshot,
            role=role,
            mode=mode,
            submission_kind=submission_kind,
        )
        reusable_assignment = (
            None
            if assignment is not None
            else self.assignment_identity.reusable_role_assignment(
                workflow_id=snapshot.workflow_id,
                aggregate_type=snapshot.aggregate_type.value,
                aggregate_id=snapshot.aggregate_id,
                role=role,
                mode=mode,
                submission_kind=submission_kind,
                input_refs=durable_input_refs,
                evaluation_generation=evaluation_generation,
            )
        )
        if reusable_assignment is not None:
            self.repository.role_cancellation.cancel_role_assignments(
                workflow_id=snapshot.workflow_id,
                aggregate_type=snapshot.aggregate_type,
                aggregate_id=snapshot.aggregate_id,
                reason=(
                    "superseded by equivalent durable role submission "
                    + str(reusable_assignment["assignment_id"])
                ),
                exclude_assignment_id=str(reusable_assignment["assignment_id"]),
            )
            self.background.signal_ready(effect, str(reusable_assignment["assignment_id"]))
            prompt_ref = self.role_checkpoints.durable_assignment_prompt_ref(reusable_assignment)
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
                reusable_assignment,
                primary_artifact_name=_role_primary_artifact_name(pack),
                summary="Reconciled an equivalent durable role submission receipt.",
            )
            terminal_ref = self.artifacts.put_json(
                terminal,
                artifact_type="RoleTerminalArtifact",
                child_refs=(
                    (prompt_ref.sha256, "prompt_pack"),
                    (
                        str(dict(reusable_assignment["submission_artifact_ref"])["sha256"]),
                        "submission_receipt",
                    ),
                ),
            )
            return AttemptReplay((terminal, prompt_ref, terminal_ref))
        return AssignmentReuseResult(assignment=assignment, durable_input_refs=durable_input_refs, pack=pack, submission_kind=submission_kind)
