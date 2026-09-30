from __future__ import annotations
from pal.bunshin.v2.semantic_orchestration.workflow_requests import WorkflowRequests
from pal.bunshin.v2.semantic_orchestration.assignment_rules import _contract_submit_idempotency_key
from pal.bunshin.v2.semantic_orchestration.architecture_instructions import _architect_authoring_locations
from pal.bunshin.v2.semantic_orchestration.architecture_instructions import _contract_architect_instruction
from pal.bunshin.v2.semantic_orchestration.worker_results import _named_json_output
from pal.bunshin.v2.semantic_orchestration.worker_results import _recorded_role_metrics
from pal.bunshin.v2.semantic_orchestration.worker_results import _role_session_turn_index
from pal.bunshin.v2.semantic_orchestration.review_results import _ref_from_mapping
from pal.bunshin.v2.semantic_orchestration.review_results import _path_pseudo_ref
from pal.bunshin.v2.semantic_orchestration.workspace_safety import _lease_is_live
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, PermanentEffectError
from pal.bunshin.v2.contract_protocol import CONTRACT_ARTIFACT, software_contract_projection
from pal.bunshin.v2.contract_submission import architect_path
from pal.bunshin.v2.sessions import architect_session_id_for_revision
from pal.bunshin.v2.skeleton import architecture_revision_scope
from pal.bunshin.v2.cycle_protocol import CycleSlot
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.replan import architecture_revision_finding_value
from pal.bunshin.v2.work_items import submission_work_items
from pal.bunshin.v2.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.v2.semantic_orchestration.assignment_identity import AssignmentIdentity
from pal.bunshin.v2.semantic_orchestration.attempt_execution import AttemptExecution
from pal.bunshin.v2.semantic_orchestration.plan_assignments import PlanAssignments
from pal.bunshin.v2.semantic_orchestration.role_reports import RoleReports
from pal.bunshin.v2.semantic_orchestration.workflow_facts import WorkflowFacts
from pal.bunshin.v2.skeleton import GitBackedSkeletonService


@dataclass
class ArchitectureAuthoring:
    assignment_identity: AssignmentIdentity
    attempt_execution: AttemptExecution
    plan_assignments: PlanAssignments
    role_reports: RoleReports
    workflow_facts: WorkflowFacts
    artifacts: ContentAddressedArtifactStore
    repository: BunshinV2Repository
    requests: WorkflowRequests
    skeleton: GitBackedSkeletonService

    async def run_skeleton_architecture_stage(
        self,
        effect: Mapping[str, Any],
        revision: AggregateSnapshot,
    ) -> Mapping[str, Any]:
        if self.workflow_facts.architecture_worker_suppressed(
            revision,
            running_state="ARCHITECT_RUNNING",
            start_action="START_ARCHITECT",
        ):
            return {"status": "superseded"}
        invocation_id = architect_session_id_for_revision(
            revision.workflow_id,
            revision.aggregate_id,
            revision.payload,
        )
        lease_resource = f"architecture:{revision.aggregate_id}:writer"
        if revision.state == "ARCHITECT_RUNNING":
            active_lease = self.repository.leases.read_lease(lease_resource)
            if active_lease and str(active_lease.get("owner_id") or "") and _lease_is_live(active_lease):
                return {"status": "already_running", "active_worker_id": str(active_lease["owner_id"])}
        base_artifact, base_manifest_ref, finding_value, request, requirements_ref, revision_scope, scope_base_path_states, scope_base_submission = self.read_revision_baseline(revision)
        try:
            architecture_workspace = self.skeleton.provision_architecture_workspace(
                workflow_id=revision.workflow_id,
                workflow_name=str(request.get("workflow_name") or request.get("goal") or revision.workflow_id),
                revision_name=revision.aggregate_id,
                workspace=dict(request.get("workspace") or {}),
                requirements_ref=requirements_ref,
                base_artifact=base_artifact,
            )
        except ValueError as exc:
            raise PermanentEffectError(str(exc)) from exc
        lease = self.repository.leases.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": revision.workflow_id,
                "aggregate_id": revision.aggregate_id,
                "stage": "architect",
                "workspace_path": str(architecture_workspace.worktree),
            },
        )
        handed_off_to_quiescer = False
        try:
            start_action = (
                "START_ARCHITECT"
                if "START_ARCHITECT"
                in self.repository.transitions.legal_actions(AggregateType.ARCHITECTURE_REVISION, revision.state)
                else "REBIND_ARCHITECT"
            )
            with self.repository.transaction() as connection:
                revision = (connection or self.repository).transitions.dispatch(
                    ActionEnvelope(
                        action_type=start_action,
                        workflow_id=revision.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision.aggregate_id,
                        actor=invocation_id,
                        expected_version=revision.version,
                        idempotency_key=f"effect:{effect['effect_key']}:{start_action.lower()}:{lease.fencing_token}",
                        payload={
                            "fencing_token": lease.fencing_token,
                            "active_worker_id": invocation_id,
                            "lease_resource_key": lease_resource,
                            "active_role": OrchestrationRole.ARCHITECT.value,
                            "active_role_mode": (
                                RoleMode.REVISION.value
                                if revision_scope is not None
                                else RoleMode.AUTHOR.value
                            ),
                            "architecture_workspace_path": str(architecture_workspace.worktree),
                            "architecture_common_git_dir": str(architecture_workspace.common_git_dir),
                            "architecture_base_sha": architecture_workspace.base_sha,
                            "architecture_base_tree_sha": architecture_workspace.base_tree_sha,
                            "architecture_repository_layout": {
                                "project_name": architecture_workspace.project_name,
                                "project_key": architecture_workspace.project_key,
                                "workflow_name": architecture_workspace.workflow_name,
                                "workflow_key": architecture_workspace.workflow_key,
                                "workflow_branch": architecture_workspace.workflow_branch,
                            },
                            "architecture_branch": architecture_workspace.architecture_branch,
                            "workspace_snapshot_ref": architecture_workspace.workspace_snapshot_ref.to_dict(),
                        },
                    ),
                ).snapshot
                self.plan_assignments.start_plan_cycle_assignment(
                    workflow_id=revision.workflow_id,
                    effect=effect,
                    slot=CycleSlot.PRODUCER,
                    unit_of_work=connection,
                )
            prompt_ref, terminal, terminal_ref, workspace_override = await self.run_architect_with_bound_inputs(
                architecture_workspace, base_manifest_ref, effect, finding_value, invocation_id, lease, lease_resource,
                request, requirements_ref, revision, revision_scope, scope_base_path_states, scope_base_submission,
            )
            submission_ref = self.record_architecture_submission(
                architecture_workspace, base_manifest_ref, effect, invocation_id, lease, prompt_ref, revision,
                terminal, terminal_ref, workspace_override,
            )
            # ARCHITECT_SUBMITTED transfers the live writer lease to the
            # quiesce/snapshot effects. Releasing it here makes a normal
            # submission look like expired-worker recovery and races the
            # worker process teardown.
            handed_off_to_quiescer = True
            return {"provider_request_id": invocation_id, "result_artifact_ref": submission_ref.to_dict()}
        finally:
            if not handed_off_to_quiescer:
                try:
                    self.repository.leases.release_lease(lease_resource, invocation_id, lease.fencing_token)
                except Exception:
                    pass

    def record_architecture_submission(
        self, architecture_workspace: Any, base_manifest_ref: Any, effect: Mapping[str, Any], invocation_id: str,
        lease: Any, prompt_ref: Any, revision: AggregateSnapshot, terminal: Any, terminal_ref: Any,
        workspace_override: dict[str, Any],
    ) -> ArtifactRef:
        role_submission = _named_json_output(terminal, "architect.yaml")
        contract = dict(role_submission.get("contract") or {})
        submission = software_contract_projection(contract)
        authoring_locations = _architect_authoring_locations(
            architect_path(workspace_override),
            contract,
        )
        checklist = {
            "kind": "work_items",
            "items": submission_work_items(
                role_submission.get("work_items")
            ),
        }
        current = self.repository.snapshots.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            revision.aggregate_id,
        )
        if current is None:
            raise ValueError("architecture revision disappeared after Architect submission")
        requirements_ref = _ref_from_mapping(current.payload.get("requirements_ref"))
        submission_ref = self.artifacts.put_json(
            {
                **dict(role_submission),
                "compiled_skeleton_submission": submission,
                "authoring_locations": authoring_locations,
            },
            artifact_type="ContractSubmissionIntentArtifact",
            provenance={"role": "architect"},
            child_refs=((requirements_ref.sha256, "requirements"),),
        )
        checklist_ref = self.artifacts.put_json(
            checklist,
            artifact_type="ArchitectWorkChecklistArtifact",
            provenance={
                "role": "architect",
                "authority": "work_cursor_only",
            },
            child_refs=(
                (submission_ref.sha256, "architecture_submission"),
            ),
        )
        self.role_reports.record_role_turn(
            terminal=terminal,
            invocation_id=invocation_id,
            fencing_token=lease.fencing_token,
            turn_index=_role_session_turn_index(terminal),
            llm_request_ref=prompt_ref.to_dict(),
            llm_response_ref=terminal_ref.to_dict(),
            tool_summary_ref=submission_ref.to_dict(),
            **_recorded_role_metrics(terminal),
        )
        self.repository.transitions.dispatch(
            ActionEnvelope(
                action_type="ARCHITECT_SUBMITTED",
                workflow_id=revision.workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision.aggregate_id,
                actor=invocation_id,
                expected_version=current.version,
                idempotency_key=_contract_submit_idempotency_key(
                    revision.aggregate_id,
                    current.version,
                    submission_ref.sha256,
                ),
                payload={
                    "requirements_ref": requirements_ref.to_dict(),
                    "pending_architecture_submission_ref": submission_ref.to_dict(),
                    "architect_checklist_ref": checklist_ref.to_dict(),
                    "fencing_token": lease.fencing_token,
                    "architecture_workspace_path": str(architecture_workspace.worktree),
                    "architecture_common_git_dir": str(architecture_workspace.common_git_dir),
                    "architecture_base_sha": architecture_workspace.base_sha,
                    "architecture_base_tree_sha": architecture_workspace.base_tree_sha,
                    "architecture_repository_layout": {
                        "project_name": architecture_workspace.project_name,
                        "project_key": architecture_workspace.project_key,
                        "workflow_name": architecture_workspace.workflow_name,
                        "workflow_key": architecture_workspace.workflow_key,
                        "workflow_branch": architecture_workspace.workflow_branch,
                    },
                    "architecture_branch": architecture_workspace.architecture_branch,
                    "workspace_snapshot_ref": architecture_workspace.workspace_snapshot_ref.to_dict(),
                    **(
                        {"revision_base_manifest_ref": base_manifest_ref.to_dict()}
                        if base_manifest_ref is not None
                        else {}
                    ),
                },
            ),
            **self.assignment_identity.role_submission_settlement(
                effect,
                assignment_id=self.assignment_identity.terminal_role_assignment_id(terminal),
            ),
        )
        return submission_ref

    async def run_architect_with_bound_inputs(
        self, architecture_workspace: Any, base_manifest_ref: Any, effect: Mapping[str, Any], finding_value: Any,
        invocation_id: str, lease: Any, lease_resource: str, request: Any, requirements_ref: Any,
        revision: AggregateSnapshot, revision_scope: Mapping[str, Any] | None,
        scope_base_path_states: Mapping[str, str] | None, scope_base_submission: Mapping[str, Any] | None,
    ) -> tuple[Any, Any, Any, dict[str, Any]]:
        references: dict[str, ArtifactRef] = {"task": requirements_ref}
        if finding_value:
            references["revision_finding"] = self.role_reports.publish_architecture_finding_view(
                finding_value,
                audience="architect",
            )
        if revision_scope is not None:
            scope_children: list[tuple[str, str]] = []
            if finding_value:
                scope_children.append(
                    (_ref_from_mapping(finding_value).sha256, "revision_finding")
                )
            references["revision_scope"] = self.artifacts.put_json(
                {
                    "affected_modules": list(
                        revision_scope.get("affected_modules") or []
                    ),
                    "allowed_paths": list(revision_scope.get("allowed_paths") or []),
                    "immutable_requirement_paths": list(
                        revision_scope.get("immutable_requirement_paths") or []
                    ),
                    "allow_topology_changes": bool(
                        revision_scope.get("allow_topology_changes")
                    ),
                },
                artifact_type="ArchitectureSkeletonRevisionScopeArtifact",
                provenance={
                    "owner": "manager",
                    "audience": "architect",
                },
                child_refs=tuple(scope_children),
            )
        if revision.payload.get("edit_instruction_ref"):
            references["edit_instruction"] = _ref_from_mapping(revision.payload["edit_instruction_ref"])
        for index, raw_reference in enumerate(list(request.get("references") or [])):
            reference = dict(raw_reference or {})
            path = str(reference.get("path") or "").strip()
            if path and Path(path).expanduser().exists():
                name = str(reference.get("name") or f"user_reference_{index + 1}").strip()
                references[f"user_{name}"] = _path_pseudo_ref(path, name)
        finding_payload = (
            dict(self.artifacts.read_json(_ref_from_mapping(finding_value)))
            if finding_value
            else {}
        )
        instruction = _contract_architect_instruction(
            finding=finding_payload,
            has_base_manifest=base_manifest_ref is not None,
            has_revision_scope=revision_scope is not None,
        )
        workspace_override: dict[str, Any] = {
            "kind": "existing_repo",
            "repo_path": str(architecture_workspace.worktree),
            "workspace_binding": "canonical",
            "project_name": architecture_workspace.project_name,
            "contract_authoring_mode": True,
            "architecture_base_sha": architecture_workspace.base_sha,
        }
        if scope_base_submission is not None:
            workspace_override.update(
                {
                    "architecture_revision_base_submission": dict(scope_base_submission),
                    "architecture_revision_base_sha": architecture_workspace.base_sha,
                }
            )
            if scope_base_path_states is not None:
                workspace_override["architecture_revision_base_path_states"] = dict(
                    scope_base_path_states
                )
            if revision_scope is not None:
                workspace_override["architecture_revision_scope"] = dict(revision_scope)
        terminal, prompt_ref, terminal_ref = await self.attempt_execution.run_profile(
            effect=effect,
            snapshot=revision,
            invocation_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=lease.fencing_token,
            profile=self.workflow_facts.profile_for_role(revision.workflow_id, "architect"),
            activation=RoleActivation(
                OrchestrationRole.ARCHITECT,
                RoleMode.REVISION if revision_scope is not None else RoleMode.AUTHOR,
            ),
            instruction=instruction,
            reference_refs=references,
            workspace_override=workspace_override,
            prepare_workspace=True,
        )
        return prompt_ref, terminal, terminal_ref, workspace_override

    def read_revision_baseline(
        self, revision: AggregateSnapshot,
    ) -> tuple[Mapping[str, Any] | None, Any, Any, Any, Any, Mapping[str, Any] | None, Mapping[str, str] | None, Mapping[str, Any] | None]:
        requirements_ref = _ref_from_mapping(revision.payload.get("requirements_ref"))
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            raise ValueError("architecture revision has no workflow")
        request = self.requests.read(workflow)
        base_manifest_ref = self.workflow_facts.revision_input_base_manifest_ref(revision)
        base_artifact: Mapping[str, Any] | None = None
        if base_manifest_ref is not None:
            record = self.repository.artifacts.read_artifact_record(base_manifest_ref.sha256)
            if (
                record is None
                or str(record.get("artifact_type") or "")
                != CONTRACT_ARTIFACT
            ):
                raise ValueError(
                    "SWE architecture revision requires a ContractArtifact baseline"
                )
            raw_base = dict(
                self.artifacts.read_json(base_manifest_ref)
            )
            base_contract = dict(raw_base.get("contract") or {})
            if (
                str(
                    raw_base.get("contract_schema") or ""
                )
                != "software_engineering.v1"
            ):
                raise ValueError(
                    "SWE architecture revision baseline uses another schema"
                )
            base_artifact = {
                **raw_base,
                "submission": (
                    software_contract_projection(
                        base_contract
                    )
                ),
            }
        finding_value = architecture_revision_finding_value(revision.payload)
        repair_baseline_value = revision.payload.get("architecture_repair_baseline_ref")
        repair_baseline: Mapping[str, Any] | None = None
        if repair_baseline_value:
            repair_baseline = self.artifacts.read_json(
                _ref_from_mapping(repair_baseline_value)
            )
        scope_base_submission: Mapping[str, Any] | None = None
        scope_base_path_states: Mapping[str, str] | None = None
        if repair_baseline is not None:
            scope_base_submission = dict(repair_baseline.get("submission") or {})
            scope_base_path_states = {
                str(path): str(value)
                for path, value in dict(repair_baseline.get("path_states") or {}).items()
            }
        elif base_artifact is not None:
            scope_base_submission = dict(base_artifact.get("submission") or {})
        revision_scope: Mapping[str, Any] | None = None
        if scope_base_submission is not None and finding_value:
            revision_scope = architecture_revision_scope(
                scope_base_submission,
                self.artifacts.read_json(_ref_from_mapping(finding_value)),
            )
        return base_artifact, base_manifest_ref, finding_value, request, requirements_ref, revision_scope, scope_base_path_states, scope_base_submission
