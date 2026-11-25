from __future__ import annotations
from pal.bunshin.semantic_orchestration.workflow_requests import WorkflowRequests
from pal.bunshin.semantic_orchestration.role_inputs import _durable_workspace_preparation
from pal.bunshin.semantic_orchestration.role_environment import _prepare_role_workspace_before_environment, _role_submission_kind
from pal.bunshin.semantic_orchestration.assignment_retries import AssignmentRetries
from pal.bunshin.semantic_orchestration.role_checkpoints import RoleCheckpoints
from dataclasses import dataclass
from pathlib import Path
from pal.bunshin.harnesses import builtin_harness_generation
from pal.bunshin.adapters import prepare_workspace_environment
from pal.bunshin.lsp_prewarm import prewarm_workspace_lsp
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import AggregateType, DeferredEffectError, SubmissionInvariantError
from pal.bunshin.paths import invocation_root, role_run_id
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.role_contracts import OrchestrationRole
from pal.bunshin.role_protocol import RoleAssignmentState
from pal.bunshin.sandbox import _workspace_path_from_pack
from pal.shared import BunshinInvocationPack
from pal.bunshin.semantic_orchestration.attempt_inputs import AttemptInputs
from pal.bunshin.semantic_orchestration.attempt_models import PreparedRoleWorkspace, RoleAttemptRequest


@dataclass
class WorkspacePreparation:
    artifacts: ContentAddressedArtifactStore
    attempt_inputs: AttemptInputs
    repository: BunshinRepository
    requests: WorkflowRequests
    runtime_root: Path
    assignment_retries: AssignmentRetries
    role_checkpoints: RoleCheckpoints

    async def execute(self, command: RoleAttemptRequest) -> PreparedRoleWorkspace:
        activation = command.activation
        fencing_token = command.fencing_token
        invocation_id = command.invocation_id
        prepare_workspace = command.prepare_workspace
        reference_refs = command.reference_refs
        snapshot = command.snapshot
        workspace_override = command.workspace_override
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, snapshot.workflow_id)
        if workflow is None:
            raise ValueError("worker workflow does not exist")
        request = self.requests.read(workflow)
        binding_ref = dict(workflow.payload.get("family_binding_ref") or {})
        binding = dict(self.artifacts.read_json(binding_ref)) if binding_ref else {}
        family_policies = dict(binding.get("policies") or {})
        llm_policy = dict(family_policies.get("llm") or {})
        workspace_policy = dict(family_policies.get("workspace") or {})
        workspace = dict(workspace_override or request.get("workspace") or {})
        if not workspace:
            workspace = {"kind": "new_project", "project_name": f"workflow-{snapshot.workflow_id}"}
        workspace["execution_mode"] = str(request.get("execution_mode") or "planned")
        # The repository source the Manager-prepared role workspace is
        # cloned or copied from; captured before preparation because the
        # prepared workspace always carries a repo_path, even when it was
        # created empty for a project without a repository.
        workspace_source_root = str(
            workspace.get("repo_path") or workspace.get("cwd") or ""
        ).strip()
        role = activation.role.value
        mode = activation.mode.value
        harness_generation = builtin_harness_generation()
        preferred_harness = harness_generation.select(role)
        if activation.role == OrchestrationRole.IMPLEMENTATION:
            workspace["manager_owned_submission_paths"] = [
                "coder_report.json",
                "producer_report.json",
            ]
        run_id = role_run_id(invocation_id)
        assignment = self.assignment_retries.retry_assignment_for_effect(
            command.effect, snapshot=snapshot, role=role, mode=mode,
            submission_kind=_role_submission_kind(
                activation, contract_authoring=bool(workspace.get("contract_authoring_mode")),
            ),
        )
        retry_prompt_ref = None
        if assignment is not None and assignment["state"] in {
            RoleAssignmentState.QUEUED.value, RoleAssignmentState.RETRY_QUEUED.value,
            RoleAssignmentState.CLAIMED.value, RoleAssignmentState.RUNNING.value,
        }:
            if self.assignment_retries.role_assignment_attempt_is_live(assignment):
                raise DeferredEffectError("role assignment already has a live process attempt")
            retry_prompt_ref = self.role_checkpoints.retry_assignment_prompt_ref(
                assignment, harness_id=preferred_harness.harness_id,
            )
        if retry_prompt_ref is not None:
            # A continuation owns its original worktree. Select that worktree
            # before either materializing inputs or inspecting the environment.
            prior = BunshinInvocationPack.from_dict(dict(self.artifacts.read_json(retry_prompt_ref)))
            root = _workspace_path_from_pack(prior)
            if root is None or not root.is_dir():
                raise SubmissionInvariantError("durable role prompt workspace is unavailable")
            workspace = dict(prior.workspace)
            uses_bound_durable_workspace = True
        else:
            workspace, uses_bound_durable_workspace = _prepare_role_workspace_before_environment(
                self.runtime_root, workspace, role=role, invocation_id=invocation_id,
                run_id=run_id, fencing_token=fencing_token, prepare_workspace=prepare_workspace,
            )
        if retry_prompt_ref is not None or not bool(workspace.get("role_workspace_prepared")):
            invocation_dir = invocation_root(self.runtime_root) / invocation_id
            attempt_dir = invocation_dir / "attempts" / f"fence-{fencing_token}"
            workspace.update(
                {
                    "run_dir": str((workspace.get("run_dir") if retry_prompt_ref is not None else None) or invocation_dir),
                    "artifact_dir": str(attempt_dir / "artifacts"),
                    "artifact_stage_dir": str(attempt_dir / "artifact-stage"),
                    "log_dir": str(attempt_dir / "logs"),
                    "build_scratch_dir": str(attempt_dir / "build-scratch"),
                    "review_scratch_dir": str(
                        workspace.get("review_scratch_dir")
                        or attempt_dir / "review-scratch"
                    ),
                }
            )
            for key in (
                "artifact_dir",
                "artifact_stage_dir",
                "log_dir",
                "build_scratch_dir",
                "review_scratch_dir",
            ):
                Path(str(workspace[key])).mkdir(parents=True, exist_ok=True)
        # Bound inputs bind before sandbox environment preparation so the
        # environment scan, the prompt pack, and the spawned process all see
        # the same verified inputs/ tree; a binding failure fails the
        # attempt closed with no spawn.
        bound_input_entries = self.attempt_inputs.bind_role_attempt_inputs(
            workflow=workflow,
            request=request,
            workspace=workspace,
            snapshot=snapshot,
            workspace_source_root=workspace_source_root,
        )
        contract_authoring = bool(workspace.get("contract_authoring_mode"))
        bound_reference_refs = dict(reference_refs)
        if bool(workspace_policy.get("prepare", False)):
            workspace.pop("prepared_environment_fingerprint", None)
            workspace.pop("lsp_environment_fingerprint", None)
            workspace, preparation = prepare_workspace_environment(
                workspace,
                runtime_root=self.runtime_root,
            )
            # LSP is implementation/verification evidence.  Architect and
            # architecture-review roles only author/compile-check contracts;
            # a missing optional language server must never block that phase.
            lsp_role = activation.role in {
                OrchestrationRole.IMPLEMENTATION,
                OrchestrationRole.VERIFIER,
            }
            if (
                lsp_role
                and bool(workspace_policy.get("prewarm_lsp", False))
                and list(workspace.get("languages") or [])
            ):
                lsp_preparation = prewarm_workspace_lsp(
                    runtime_root=self.runtime_root,
                    workspace=workspace,
                )
                preparation["lsp_workspace_preparation"] = lsp_preparation
                environment_fingerprint = str(
                    lsp_preparation.get("environment_fingerprint") or ""
                ).strip()
                if environment_fingerprint:
                    workspace["lsp_environment_fingerprint"] = environment_fingerprint
            preparation_ref = self.artifacts.put_json(
                _durable_workspace_preparation(preparation),
                artifact_type="WorkspacePreparationArtifact",
                provenance={"family_id": str(binding.get("family_id") or ""), "role": role},
            )
            bound_reference_refs["workspace_preparation"] = preparation_ref
        return PreparedRoleWorkspace(
            binding=binding, binding_ref=binding_ref, bound_input_entries=bound_input_entries,
            bound_reference_refs=bound_reference_refs, contract_authoring=contract_authoring,
            family_policies=family_policies, harness_generation=harness_generation, llm_policy=llm_policy, mode=mode,
            preferred_harness=preferred_harness, request=request, role=role, run_id=run_id,
            uses_bound_durable_workspace=uses_bound_durable_workspace, workspace=workspace,
            retry_prompt_ref=retry_prompt_ref,
        )
