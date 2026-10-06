from __future__ import annotations
from pal.bunshin.v2.artifacts import ArtifactRef
import asyncio
from dataclasses import dataclass
from pal.bunshin.v2.background_assignments import BackgroundAssignments
from pal.bunshin.v2.storage.role_assignments import semantic_business_lease
from typing import Any
from pal.bunshin.v2.contracts import AggregateSnapshot, DeferredEffectError
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.role_contracts import RoleActivation
from typing import Mapping
from pal.bunshin.v2.role_runtime import RoleSupervisor
import contextlib
from pal.bunshin.v2.semantic_orchestration.role_leases import RoleLeases
from pal.bunshin.v2.semantic_orchestration.attempt_models import RoleAttemptRequest, AttemptReplay
from pal.bunshin.v2.semantic_orchestration.attempt_workspace_preparation import WorkspacePreparation
from pal.bunshin.v2.semantic_orchestration.attempt_verifier_context import VerifierContext
from pal.bunshin.v2.semantic_orchestration.attempt_reference_binding import ReferenceBinding
from pal.bunshin.v2.semantic_orchestration.attempt_prompt_construction import PromptConstruction
from pal.bunshin.v2.semantic_orchestration.attempt_playbook_binding import PlaybookBinding
from pal.bunshin.v2.semantic_orchestration.attempt_tool_policy import ToolPolicy
from pal.bunshin.v2.semantic_orchestration.attempt_assignment_reuse import AssignmentReuse
from pal.bunshin.v2.semantic_orchestration.attempt_role_session import RoleSession
from pal.bunshin.v2.semantic_orchestration.attempt_harness_binding import HarnessBinding
from pal.bunshin.v2.semantic_orchestration.attempt_assignment_replay import AssignmentReplay
from pal.bunshin.v2.semantic_orchestration.attempt_admission import AttemptAdmission
from pal.bunshin.v2.semantic_orchestration.attempt_pack import AttemptPack
from pal.bunshin.v2.semantic_orchestration.attempt_publication import AttemptPublication
from pal.bunshin.v2.semantic_orchestration.attempt_worker_execution import WorkerExecution
from pal.bunshin.v2.semantic_orchestration.attempt_process_result import ProcessResult
from pal.bunshin.v2.semantic_orchestration.attempt_terminal_validation import TerminalValidation
from pal.bunshin.v2.semantic_orchestration.attempt_completion import AttemptCompletion


@dataclass
class AttemptExecution:
    workspace_preparation: WorkspacePreparation
    verifier_context: VerifierContext
    reference_binding: ReferenceBinding
    prompt_construction: PromptConstruction
    playbook_binding: PlaybookBinding
    tool_policy: ToolPolicy
    assignment_reuse: AssignmentReuse
    role_session: RoleSession
    harness_binding: HarnessBinding
    assignment_replay: AssignmentReplay
    attempt_admission: AttemptAdmission
    attempt_pack: AttemptPack
    attempt_publication: AttemptPublication
    worker_execution: WorkerExecution
    process_result: ProcessResult
    terminal_validation: TerminalValidation
    attempt_completion: AttemptCompletion
    repository: BunshinV2Repository
    role_leases: RoleLeases
    supervisor: RoleSupervisor
    background: BackgroundAssignments | None = None

    async def run_profile(
        self,
        *,
        effect: Mapping[str, Any],
        snapshot: AggregateSnapshot,
        invocation_id: str,
        lease_resource: str,
        fencing_token: int,
        profile: str,
        activation: RoleActivation,
        instruction: str,
        reference_refs: Mapping[str, ArtifactRef],
        workspace_override: Mapping[str, Any] | None = None,
        prepare_workspace: bool = True,
    ) -> tuple[dict[str, Any], ArtifactRef, ArtifactRef]:
        """Run one logical role while its aggregate ownership stays live.

        The aggregate lease belongs to the durable logical coroutine and is
        therefore renewed while it waits for native-process capacity.  The
        attempt lease is deliberately created later, inside
        ``_run_profile_inner``, only after a process permit is available.
        """

        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "")
        if self.background is not None and effect_key:
            self.background.assert_not_retired(effect_key)
            existing = self.background.task(effect_key)
            if existing is not asyncio.current_task():
                if existing is not None and not existing.done():
                    raise DeferredEffectError("role effect already has an owned execution task")

                async def owned_profile() -> Mapping[str, Any]:
                    return {"profile_result": await self.run_profile(
                        effect=effect, snapshot=snapshot, invocation_id=invocation_id,
                        lease_resource=lease_resource, fencing_token=fencing_token,
                        profile=profile, activation=activation, instruction=instruction,
                        reference_refs=reference_refs, workspace_override=workspace_override,
                        prepare_workspace=prepare_workspace,
                    )}

                # Inline/direct entry uses the same exact effect-key registry.
                # Own just this role operation, never an unrelated outer task.
                owned = asyncio.create_task(owned_profile())
                self.background.track(effect_key, owned)
                result = await owned
                return result["profile_result"]
            self.background.bind_lease(effect_key, invocation_id, lease_resource, fencing_token)
        self.role_leases.bind_background_business_lease(
            effect, owner_id=invocation_id, resource_key=lease_resource, fencing_token=fencing_token,
        )
        self.repository.leases.renew_lease(
            lease_resource,
            invocation_id,
            fencing_token,
            ttl_seconds=120,
        )
        heartbeat = asyncio.create_task(
            self.role_leases.lease_heartbeat(
                lease_resource,
                invocation_id,
                fencing_token,
            )
        )
        try:
            return await self.run_profile_inner(
                effect=effect,
                snapshot=snapshot,
                invocation_id=invocation_id,
                lease_resource=lease_resource,
                fencing_token=fencing_token,
                profile=profile,
                activation=activation,
                instruction=instruction,
                reference_refs=reference_refs,
                workspace_override=workspace_override,
                prepare_workspace=prepare_workspace,
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await self.supervisor.release_process_slot()

    async def run_profile_inner(
        self,
        *,
        effect: Mapping[str, Any],
        snapshot: AggregateSnapshot,
        invocation_id: str,
        lease_resource: str,
        fencing_token: int,
        profile: str,
        activation: RoleActivation,
        instruction: str,
        reference_refs: Mapping[str, ArtifactRef],
        workspace_override: Mapping[str, Any] | None = None,
        prepare_workspace: bool = True,
    ) -> tuple[dict[str, Any], ArtifactRef, ArtifactRef]:
        command = RoleAttemptRequest(
            effect=effect, snapshot=snapshot, invocation_id=invocation_id, lease_resource=lease_resource,
            fencing_token=fencing_token, profile=profile, activation=activation, instruction=instruction,
            reference_refs=reference_refs, workspace_override=workspace_override, prepare_workspace=prepare_workspace,
        )
        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "")
        if self.background is not None and effect_key:
            self.background.assert_not_retired(effect_key)
        business_lease = semantic_business_lease(
            snapshot, owner_id=invocation_id, resource_key=lease_resource, fencing_token=fencing_token,
        )
        if business_lease:
            self.repository.role_assignments.assert_semantic_admission(
                workflow_id=snapshot.workflow_id, aggregate_type=snapshot.aggregate_type.value,
                aggregate_id=snapshot.aggregate_id, business_lease=business_lease,
                require_running=False,
            )
        stage_workspace_preparation = await self.workspace_preparation.execute(command)
        stage_verifier_context = await self.verifier_context.execute(command, stage_workspace_preparation)
        stage_reference_binding = await self.reference_binding.execute(command, stage_workspace_preparation)
        stage_prompt_construction = await self.prompt_construction.execute(command, stage_reference_binding, stage_verifier_context, stage_workspace_preparation)
        stage_playbook_binding = await self.playbook_binding.execute(command, stage_prompt_construction, stage_workspace_preparation)
        stage_tool_policy = await self.tool_policy.execute(command, stage_playbook_binding, stage_prompt_construction, stage_workspace_preparation)
        stage_assignment_reuse = await self.assignment_reuse.execute(command, stage_reference_binding, stage_tool_policy, stage_workspace_preparation)
        if isinstance(stage_assignment_reuse, AttemptReplay):
            return stage_assignment_reuse.result
        stage_role_session = await self.role_session.execute(command, stage_assignment_reuse, stage_prompt_construction, stage_reference_binding, stage_workspace_preparation)
        stage_harness_binding = await self.harness_binding.execute(command, stage_assignment_reuse, stage_role_session, stage_workspace_preparation)
        stage_assignment_replay = await self.assignment_replay.execute(command, stage_harness_binding, stage_role_session, stage_workspace_preparation)
        if isinstance(stage_assignment_replay, AttemptReplay):
            return stage_assignment_replay.result
        stage_attempt_admission = await self.attempt_admission.execute(command, stage_harness_binding, stage_role_session, stage_workspace_preparation)
        stage_attempt_pack = await self.attempt_pack.execute(
            command, stage_assignment_replay, stage_attempt_admission, stage_harness_binding, stage_role_session,
            stage_workspace_preparation,
        )
        stage_attempt_publication = await self.attempt_publication.execute(
            command, stage_attempt_admission, stage_attempt_pack, stage_harness_binding, stage_role_session,
            stage_workspace_preparation,
        )
        stage_worker_execution = await self.worker_execution.execute(command, stage_attempt_admission, stage_attempt_publication, stage_workspace_preparation)
        stage_process_result = await self.process_result.execute(
            command, stage_attempt_admission, stage_attempt_pack, stage_attempt_publication, stage_harness_binding,
            stage_role_session, stage_worker_execution,
        )
        stage_terminal_validation = await self.terminal_validation.execute(
            command, stage_attempt_admission, stage_attempt_pack, stage_harness_binding, stage_process_result,
            stage_role_session,
        )
        stage_attempt_completion = await self.attempt_completion.execute(
            command, stage_attempt_admission, stage_attempt_pack, stage_attempt_publication, stage_harness_binding,
            stage_process_result, stage_terminal_validation, stage_worker_execution,
        )
        return stage_attempt_completion
