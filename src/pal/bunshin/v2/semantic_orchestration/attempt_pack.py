from __future__ import annotations
from pal.bunshin.v2.semantic_orchestration.runtime_settings import RoleRuntimeSettings
from dataclasses import dataclass
from pathlib import Path
from pal.bunshin.v2.paths import invocation_root
from pal.bunshin.v2.submission_drafts import AUTHORING_CONTRACT_VERSION
from pal.shared import BunshinInvocationPack
from pal.bunshin.v2.semantic_orchestration.role_checkpoints import RoleCheckpoints
from pal.bunshin.v2.role_contracts import role_session_stage_key
from pal.bunshin.v2.semantic_orchestration.attempt_models import (
    BoundRoleHarness, ClaimedRoleAttempt, MaterializedRolePack, PreparedRoleSession, PreparedRoleWorkspace,
    RoleAttemptRequest, RunnableRoleAssignment,
)


@dataclass
class AttemptPack:
    settings: RoleRuntimeSettings
    role_checkpoints: RoleCheckpoints
    runtime_root: Path

    async def execute(
        self, command: RoleAttemptRequest, stage_assignment_replay: RunnableRoleAssignment,
        stage_attempt_admission: ClaimedRoleAttempt, stage_harness_binding: BoundRoleHarness,
        stage_role_session: PreparedRoleSession, stage_workspace_preparation: PreparedRoleWorkspace,
    ) -> MaterializedRolePack:
        assignment = stage_role_session.assignment
        assignment_lease = stage_attempt_admission.assignment_lease
        assignment_lease_resource = stage_attempt_admission.assignment_lease_resource
        attempt = stage_attempt_admission.attempt
        effective_harness_generation = stage_harness_binding.effective_harness_generation
        harness_spec = stage_harness_binding.harness_spec
        input_fingerprint = stage_harness_binding.input_fingerprint
        invocation_id = command.invocation_id
        mode = stage_workspace_preparation.mode
        pack = stage_assignment_replay.pack
        role = stage_workspace_preparation.role
        session_scope_kind = stage_role_session.session_scope_kind
        session_subject_key = stage_role_session.session_subject_key
        snapshot = command.snapshot
        attempt_dir = (
            invocation_root(self.runtime_root)
            / invocation_id
            / "attempts"
            / f"fence-{assignment_lease.fencing_token}"
        )
        continuation_input_path, continuation_output_path = (
            self.role_checkpoints.prepare_agent_session_attempt(
                session_id=invocation_id,
                attempt_id=str(attempt["attempt_id"]),
            )
        )
        pack_value = pack.to_dict()
        # The durable role workspace is part of the worker's authoring
        # context.  Keep its lease identity in lockstep with the attempt
        # metadata below: retries keep the logical role session, but each
        # materialized attempt gets a new fencing owner.  Leaving the old
        # session id here makes draft_read present a context that the Role
        # Gateway (correctly) rejects as belonging to another assignment.
        workspace_value = dict(pack_value.get("workspace") or {})
        workspace_binding = dict(workspace_value.get("bunshin_v2") or {})
        workspace_binding.update(
            {
                # SubmissionDraftContext is reconstructed from the workspace
                # pack inside the worker.  Keep the complete immutable
                # authoring binding there, not only the per-attempt lease
                # fields; metadata.bunshin_v2 is not visible to that parser.
                "workflow_id": snapshot.workflow_id,
                "aggregate_type": snapshot.aggregate_type.value,
                "aggregate_id": snapshot.aggregate_id,
                "role": role,
                "mode": mode,
                "authoring_input_fingerprint": input_fingerprint,
                "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
                "invocation_id": str(attempt["attempt_id"]),
                "lease_resource": assignment_lease_resource,
                "lease_resource_key": assignment_lease_resource,
                "fencing_token": assignment_lease.fencing_token,
                "harness_id": harness_spec.harness_id,
                "harness_generation": effective_harness_generation,
                "harness_config": dict(harness_spec.config),
            }
        )
        workspace_value["bunshin_v2"] = workspace_binding
        workspace_value.update(
            {
                "artifact_dir": str(attempt_dir / "artifacts"),
                "artifact_stage_dir": str(attempt_dir / "artifact-stage"),
                "log_dir": str(attempt_dir / "logs"),
                "build_scratch_dir": str(attempt_dir / "build-scratch"),
            }
        )
        pack_value["workspace"] = workspace_value
        for key in (
            "artifact_dir",
            "artifact_stage_dir",
            "log_dir",
            "build_scratch_dir",
        ):
            Path(str(workspace_value[key])).mkdir(parents=True, exist_ok=True)
        metadata = dict(pack_value.get("metadata") or {})
        bunshin_v2 = dict(metadata.get("bunshin_v2") or {})
        bunshin_v2.update(
            {
                "invocation_id": str(attempt["attempt_id"]),
                "lease_resource": assignment_lease_resource,
                "lease_resource_key": assignment_lease_resource,
                "fencing_token": assignment_lease.fencing_token,
                # Keep both projections of the authoring binding aligned.
                # Retries may reuse a prompt whose derived workspace changed,
                # but the assignment fingerprint is immutable.
                "authoring_input_fingerprint": input_fingerprint,
                "harness_id": harness_spec.harness_id,
                "harness_generation": effective_harness_generation,
                "harness_config": dict(harness_spec.config),
            }
        )
        metadata["bunshin_v2"] = bunshin_v2
        metadata["agent_session"] = {
            "session_id": invocation_id,
            # A retry reuses the same durable assignment while a new
            # RepairBill receives a new assignment.  This is the semantic turn
            # identity used by the persistent role session.
            "response_key": str(assignment["assignment_id"]),
            "fencing_token": assignment_lease.fencing_token,
            "workflow_id": snapshot.workflow_id,
            "scope_kind": session_scope_kind,
            "subject_key": session_subject_key,
            "stage_key": role_session_stage_key(
                session_scope_kind,
                session_subject_key,
                role,
            ),
            "harness_id": harness_spec.harness_id,
            "harness_generation": effective_harness_generation,
            "continuation_input_path": str(continuation_input_path or ""),
            "continuation_output_path": str(continuation_output_path),
        }
        # Debug logging is runtime policy rather than durable role truth.
        # Snapshot it when the concrete role process is materialized.
        metadata["prompt_log_enabled"] = bool(self.settings.prompt_logging)
        if self.settings.prompt_logging:
            log_dir = str(workspace_value.get("log_dir") or "").strip()
            if log_dir:
                metadata["debug_log"] = {
                    "enabled": True,
                    "path": str(Path(log_dir) / "bunshin-debug.log"),
                }
        else:
            metadata.pop("debug_log", None)
        pack = BunshinInvocationPack.from_dict({**pack_value, "metadata": metadata})
        return MaterializedRolePack(continuation_output_path=continuation_output_path, pack=pack)
