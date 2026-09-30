from __future__ import annotations
import pal.bunshin.turns as _dependency_turns
from dataclasses import dataclass
from pathlib import Path
from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.paths import invocation_root
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.submission_drafts import AUTHORING_CONTRACT_VERSION
from pal.bunshin.turns import sanitize_runner_session_pack
from pal.bunshin.harnesses import HARNESS_LAUNCH_PAL_SANDBOX
from pal.bunshin.v2.semantic_orchestration.role_environment import _bind_role_attempt_sandbox
import json
from pal.bunshin.ipc import BUNSHIN_RUNTIME_DB_PATH_ENV, python_subprocess_env
from pal.bunshin.sandbox import build_sandboxed_runner_invocation
from pal.bunshin.ipc import ROLE_GATEWAY_TOKEN_ENV
from pal.bunshin.v2.semantic_orchestration.attempt_models import (
    BoundRoleHarness, ClaimedRoleAttempt, MaterializedRolePack, PreparedRoleSession, PreparedRoleWorkspace,
    PublishedRoleAttempt, RoleAttemptRequest,
)


@dataclass
class AttemptPublication:
    artifacts: ContentAddressedArtifactStore
    repository: BunshinV2Repository
    runtime_db_path: Path | None
    runtime_root: Path

    async def execute(
        self, command: RoleAttemptRequest, stage_attempt_admission: ClaimedRoleAttempt,
        stage_attempt_pack: MaterializedRolePack, stage_harness_binding: BoundRoleHarness,
        stage_role_session: PreparedRoleSession, stage_workspace_preparation: PreparedRoleWorkspace,
    ) -> PublishedRoleAttempt:
        assignment = stage_role_session.assignment
        assignment_lease = stage_attempt_admission.assignment_lease
        assignment_lease_resource = stage_attempt_admission.assignment_lease_resource
        attempt = stage_attempt_admission.attempt
        binding_ref = stage_workspace_preparation.binding_ref
        bound_reference_refs = stage_workspace_preparation.bound_reference_refs
        durable_prompt_reused = stage_harness_binding.durable_prompt_reused
        effective_harness_generation = stage_harness_binding.effective_harness_generation
        fencing_token = command.fencing_token
        harness_spec = stage_harness_binding.harness_spec
        invocation_id = command.invocation_id
        lease_resource = command.lease_resource
        mode = stage_workspace_preparation.mode
        pack = stage_attempt_pack.pack
        profile = command.profile
        role = stage_workspace_preparation.role
        run_id = stage_workspace_preparation.run_id
        snapshot = command.snapshot
        if harness_spec.launch_kind == HARNESS_LAUNCH_PAL_SANDBOX:
            pack = _bind_role_attempt_sandbox(
                self.runtime_root,
                pack,
                run_id=run_id,
                durable_prompt_reused=durable_prompt_reused,
            )
        else:
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
        self.repository.role_attempts.start_role_attempt(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=str(attempt["attempt_id"]),
            lease_resource_key=assignment_lease_resource,
            fencing_token=assignment_lease.fencing_token,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        assignment_access_token = self.repository.role_access.issue_role_attempt_access_token(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=str(attempt["attempt_id"]),
            fencing_token=assignment_lease.fencing_token,
        )
        self.repository.role_invocations.record_role_invocation(
            invocation_id=invocation_id,
            workflow_id=snapshot.workflow_id,
            aggregate_type=snapshot.aggregate_type,
            aggregate_id=snapshot.aggregate_id,
            lease_resource_key=lease_resource,
            fencing_token=fencing_token,
            role=role,
            mode=mode,
            role_profile_id=profile,
            harness_id=harness_spec.harness_id,
            harness_generation=effective_harness_generation,
            family_binding_sha=str(binding_ref.get("sha256") or ""),
            authoring_contract_version=AUTHORING_CONTRACT_VERSION,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        invocation_dir = invocation_root(self.runtime_root) / invocation_id
        invocation_dir.mkdir(parents=True, exist_ok=True)
        # The process pack belongs to the assignment lease, not the caller's
        # stale logical-effect fence.  Keep this path identical to the
        # attempt-local workspace paths above.
        attempt_dir = (
            invocation_dir
            / "attempts"
            / f"fence-{assignment_lease.fencing_token}"
        )
        attempt_dir.mkdir(parents=True, exist_ok=True)
        pack_path = attempt_dir / "pack.json"
        pack_path.write_text(json.dumps(pack.to_dict(), ensure_ascii=False, sort_keys=True), encoding="utf-8")
        argv = [
            *harness_spec.worker_argv,
            "--runtime-root",
            str(self.runtime_root),
            "--pack-json",
            str(pack_path),
            "--bunshin-id",
            invocation_id,
            "--run-id",
            run_id,
        ]
        runner_env = python_subprocess_env()
        runner_env[ROLE_GATEWAY_TOKEN_ENV] = assignment_access_token
        if self.runtime_db_path is not None:
            runner_env[BUNSHIN_RUNTIME_DB_PATH_ENV] = str(self.runtime_db_path)
        if harness_spec.launch_kind == HARNESS_LAUNCH_PAL_SANDBOX:
            argv, env = build_sandboxed_runner_invocation(
                runtime_root=self.runtime_root,
                pack=pack,
                argv=argv,
                env=runner_env,
            )
        else:
            env = runner_env
        return PublishedRoleAttempt(argv=argv, env=env, pack=pack, prompt_ref=prompt_ref)
