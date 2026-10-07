from __future__ import annotations
from dataclasses import dataclass
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.semantic_orchestration.role_policy import _role_primary_artifact_name
from pal.bunshin.semantic_orchestration.role_checkpoints import RoleCheckpoints
from pal.bunshin.semantic_orchestration.worker_results import _worker_event_timing
from pal.bunshin.semantic_orchestration.attempt_models import (
    AttemptResult, BoundRoleHarness, ClaimedRoleAttempt, CollectedRoleTerminal, ExitedRoleProcess,
    MaterializedRolePack, PublishedRoleAttempt, RoleAttemptRequest, ValidatedRoleTerminal,
)


@dataclass
class AttemptCompletion:
    artifacts: ContentAddressedArtifactStore
    repository: BunshinRepository
    role_checkpoints: RoleCheckpoints

    async def execute(
        self, command: RoleAttemptRequest, stage_attempt_admission: ClaimedRoleAttempt,
        stage_attempt_pack: MaterializedRolePack, stage_attempt_publication: PublishedRoleAttempt,
        stage_harness_binding: BoundRoleHarness, stage_process_result: CollectedRoleTerminal,
        stage_terminal_validation: ValidatedRoleTerminal, stage_worker_execution: ExitedRoleProcess,
    ) -> AttemptResult:
        assignment_after_process = stage_terminal_validation.assignment_after_process
        assignment_lease = stage_attempt_admission.assignment_lease
        continuation_output_path = stage_attempt_pack.continuation_output_path
        events = stage_worker_execution.events
        fencing_token = command.fencing_token
        invocation_id = command.invocation_id
        pack = stage_attempt_publication.pack
        pal_checkpoint_capable = stage_harness_binding.pal_checkpoint_capable
        prompt_ref = stage_attempt_publication.prompt_ref
        terminal = stage_process_result.terminal
        terminal = self.role_checkpoints.terminal_from_assignment_receipt(
            assignment_after_process,
            primary_artifact_name=_role_primary_artifact_name(pack),
            summary=str(dict(terminal.get("payload") or {}).get("summary") or "Role submission recorded."),
            original_terminal=terminal,
        )
        terminal_payload = dict(terminal.get("payload") or {})
        checkpoint = self.role_checkpoints.publish_agent_session_checkpoint(
            invocation_id,
            assignment_lease.fencing_token,
            continuation_output_path,
        )
        terminal_payload["v2_timing"] = _worker_event_timing(events)
        if checkpoint is not None:
            terminal_payload["session_turn_index"] = int(
                dict(checkpoint.get("metrics") or {}).get(
                    "llm_round_count"
                )
                or 0
            )
        terminal = {**terminal, "payload": terminal_payload}
        terminal_ref = self.artifacts.put_json(
            terminal,
            artifact_type="RoleTerminalArtifact",
            child_refs=(
                (prompt_ref.sha256, "prompt_pack"),
                (
                    str(dict(assignment_after_process["submission_artifact_ref"])["sha256"]),
                    "submission_receipt",
                ),
            ),
        )
        if pal_checkpoint_capable:
            if checkpoint is None:
                raise RuntimeError("resumable role completed without a durable agent-session checkpoint")
            self.repository.role_invocations.suspend_role_invocation(
                invocation_id=invocation_id,
                fencing_token=fencing_token,
            )
        else:
            self.repository.role_invocations.finish_role_invocation(
                invocation_id=invocation_id,
                fencing_token=fencing_token,
                status="completed",
            )
        return terminal, prompt_ref, terminal_ref
