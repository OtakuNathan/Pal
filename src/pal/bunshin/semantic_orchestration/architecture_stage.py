from __future__ import annotations
from pal.bunshin.semantic_orchestration.workflow_requests import WorkflowRequests
from pal.bunshin.semantic_orchestration.worker_results import _named_json_output
from pal.bunshin.semantic_orchestration.worker_results import _recorded_role_metrics
from pal.bunshin.semantic_orchestration.worker_results import _role_session_turn_index
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping
from pal.bunshin.semantic_orchestration.review_results import _path_pseudo_ref
from pal.bunshin.semantic_orchestration.workspace_safety import _lease_is_live
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.bunshin.sessions import architect_session_id_for_revision
from pal.bunshin.cycle_protocol import CycleSlot
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.replan import architecture_revision_finding_value
from pal.bunshin.work_items import submission_work_items
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.semantic_orchestration.architecture_authoring import ArchitectureAuthoring
from pal.bunshin.semantic_orchestration.assignment_identity import AssignmentIdentity
from pal.bunshin.semantic_orchestration.attempt_execution import AttemptExecution
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.plan_assignments import PlanAssignments
from pal.bunshin.semantic_orchestration.role_reports import RoleReports
from pal.bunshin.semantic_orchestration.workflow_facts import WorkflowFacts


@dataclass
class ArchitectureStage:
    architecture_authoring: ArchitectureAuthoring
    assignment_identity: AssignmentIdentity
    attempt_execution: AttemptExecution
    effect_reads: EffectReads
    plan_assignments: PlanAssignments
    role_reports: RoleReports
    workflow_facts: WorkflowFacts
    artifacts: ContentAddressedArtifactStore
    repository: BunshinRepository
    requests: WorkflowRequests

    async def run_architecture_stage(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        initial_revision = self.effect_reads.effect_snapshot(effect)
        if self.workflow_facts.uses_git_skeleton(initial_revision.workflow_id):
            return await self.architecture_authoring.run_skeleton_architecture_stage(effect, initial_revision)
        stage = OrchestrationRole.ARCHITECT.value
        start_action = "START_ARCHITECT"
        running_state = "ARCHITECT_RUNNING"
        rebind_action = "REBIND_ARCHITECT"
        revision = self.effect_reads.effect_snapshot(effect)
        activation = RoleActivation(
            OrchestrationRole.ARCHITECT,
            (
                RoleMode.REVISION
                if self.workflow_facts.revision_input_base_manifest_ref(revision) is not None
                else RoleMode.AUTHOR
            ),
        )
        profile = self.workflow_facts.profile_for_role(revision.workflow_id, OrchestrationRole.ARCHITECT.value)
        if self.workflow_facts.architecture_worker_suppressed(revision, running_state=running_state, start_action=start_action):
            return {"status": "superseded"}
        invocation_id = architect_session_id_for_revision(
            revision.workflow_id,
            revision.aggregate_id,
            revision.payload,
        )
        lease_resource = f"architecture:{revision.aggregate_id}:{stage}"
        if revision.state == running_state:
            active_lease = self.repository.leases.read_lease(lease_resource)
            if active_lease and str(active_lease.get("owner_id") or "") and _lease_is_live(active_lease):
                self.plan_assignments.start_plan_cycle_assignment(
                    workflow_id=revision.workflow_id,
                    effect=effect,
                    slot=CycleSlot.PRODUCER,
                )
                return {"status": "already_running", "active_worker_id": str(active_lease["owner_id"])}
        lease = self.repository.leases.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": revision.workflow_id,
                "aggregate_id": revision.aggregate_id,
                "role": OrchestrationRole.ARCHITECT.value,
                "mode": activation.mode.value,
            },
        )
        try:
            revision = self.admit_architecture_stage(activation, effect, invocation_id, lease, lease_resource, rebind_action, revision, start_action)
            prompt, reference_refs = self.architecture_stage_prompt(stage, revision)
            terminal, prompt_ref, terminal_ref = await self.attempt_execution.run_profile(
                effect=effect,
                snapshot=revision,
                invocation_id=invocation_id,
                lease_resource=lease_resource,
                fencing_token=lease.fencing_token,
                profile=profile,
                activation=activation,
                instruction=prompt,
                reference_refs=reference_refs,
                workspace_override=None,
                prepare_workspace=True,
            )
            submission = _named_json_output(terminal, "architect.yaml")
            checklist_ref, current, requirements_ref, result_ref, revision_base_manifest_ref = self.publish_architecture_submission(revision, submission)
            with self.repository.transaction() as connection:
                (connection or self.repository).transitions.dispatch(
                    ActionEnvelope(
                        action_type="DATA_ARCHITECT_COMPLETED",
                        workflow_id=revision.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision.aggregate_id,
                        actor="bunshin-v2-architect",
                        expected_version=current.version,
                        idempotency_key=f"architect-output:{revision.aggregate_id}:{result_ref.sha256}",
                        payload={
                            "requirements_ref": requirements_ref.to_dict(),
                            "architecture_manifest_ref": result_ref.to_dict(),
                            "architect_checklist_ref": checklist_ref.to_dict(),
                            **(
                                {"revision_base_manifest_ref": revision_base_manifest_ref.to_dict()}
                                if revision_base_manifest_ref is not None
                                else {}
                            ),
                        },
                    ),
                    **self.assignment_identity.role_submission_settlement(
                        effect,
                        assignment_id=self.assignment_identity.terminal_role_assignment_id(terminal),
                    ),
                )
                WorkflowCoordinator(self.repository).submit_plan_product(
                    workflow_id=revision.workflow_id,
                    product_ref=result_ref.sha256,
                    unit_of_work=connection,
                )
            self.role_reports.record_role_turn(
                terminal=terminal,
                invocation_id=invocation_id,
                fencing_token=lease.fencing_token,
                turn_index=_role_session_turn_index(terminal),
                llm_request_ref=prompt_ref.to_dict(),
                llm_response_ref=terminal_ref.to_dict(),
                tool_summary_ref=result_ref.to_dict(),
                **_recorded_role_metrics(terminal),
            )
            return {"provider_request_id": invocation_id, "result_artifact_ref": result_ref.to_dict()}
        finally:
            try:
                self.repository.leases.release_lease(lease_resource, invocation_id, lease.fencing_token)
            except Exception:
                pass

    def architecture_stage_prompt(
        self,
        stage: str,
        revision: AggregateSnapshot,
    ) -> tuple[str, dict[str, ArtifactRef]]:
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            raise ValueError("architecture revision has no workflow")
        request = self.requests.read(workflow)
        requirements_ref = _ref_from_mapping(revision.payload.get("requirements_ref"))
        finding_value = architecture_revision_finding_value(revision.payload)
        base_manifest_ref = self.workflow_facts.revision_input_base_manifest_ref(revision)
        refs: dict[str, ArtifactRef]
        scoped_revision = base_manifest_ref is not None and finding_value is not None
        if base_manifest_ref is None:
            refs = {"task": requirements_ref}
        elif scoped_revision:
            refs = {
                "task": requirements_ref,
                "revision_scope": self.role_reports.publish_architecture_revision_scope(
                    revision,
                    base_manifest_ref=base_manifest_ref,
                    finding_value=finding_value,
                )
            }
        else:
            refs = {"task": requirements_ref}
            if revision.payload.get("edit_instruction_ref"):
                refs["edit_instruction"] = _ref_from_mapping(revision.payload.get("edit_instruction_ref"))
        if revision.payload.get("edit_instruction_ref"):
            refs["edit_instruction"] = _ref_from_mapping(revision.payload.get("edit_instruction_ref"))
        if finding_value and not scoped_revision:
            refs["revision_finding"] = self.role_reports.publish_architecture_finding_view(
                finding_value,
                audience="architect",
            )
        instruction = (
            "Read the ordered read-only task ledger and perform one bounded consistency pass. Then immediately call update_checklist to "
            "complete requirements/design and begin contract encoding. Use that checklist as the execution cursor: work only on its current "
            "phase, externalize each settled phase through the Contract tools before moving on, and batch independent calls in one response. "
            "Resolve only architecture-feasibility questions, declare the smallest complete directional contract DAG and end-to-end "
            "integration, defer private implementation, reconcile the Contract against the task, complete the checklist, and submit."
        )
        if scoped_revision:
            instruction += (
                " This is a guided revision: read revision_scope first and consult task.yaml only when exact upstream task semantics are needed; "
                "do not reread the repository, workflow request, or base manifest. The manager has preseeded the complete base "
                "contract privately. Start from the named semantic targets with the same incremental Contract tools used for initial authoring. "
                "Preserve unrelated semantics unless contract consistency requires a wider correction; revision_scope is repair guidance, not a write fence."
            )
        elif base_manifest_ref is not None:
            instruction += (
                " This is a human-authored revision. The manager has preseeded the base contract; apply the bound edit instruction without "
                "rediscovering the architecture, and preserve all unrelated semantics."
            )
        for index, reference in enumerate(list(request.get("references") or []) if base_manifest_ref is None else []):
            if not isinstance(reference, Mapping):
                continue
            path = str(reference.get("path") or "").strip()
            if path and Path(path).expanduser().exists():
                name = str(reference.get("name") or f"user_reference_{index + 1}").strip()
                refs[f"user_{name}"] = _path_pseudo_ref(path, name)
        return instruction, refs

    def publish_architecture_submission(self, revision: AggregateSnapshot, submission: Any) -> tuple[Any, Any, Any, Any, Any]:
        contract = dict(submission.get("contract") or {})
        checklist = {
            "kind": "work_items",
            "items": submission_work_items(submission.get("work_items")),
        }
        current = self.repository.snapshots.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            revision.aggregate_id,
        )
        if current is None:
            raise ValueError("architecture revision disappeared after Architect submission")
        requirements_ref = _ref_from_mapping(current.payload.get("requirements_ref"))
        revision_base_manifest_ref = self.workflow_facts.revision_input_base_manifest_ref(revision)
        result_ref = self.artifacts.put_json(
            {
                "schema_version": "2",
                "contract_schema": str(
                    submission.get("contract_schema") or ""
                ),
                "contract": contract,
                "graph_ir": dict(submission.get("graph_ir") or {}),
                "graph_source_map_ref": dict(
                    submission.get("graph_source_map_ref") or {}
                ),
                "requirements_ref": requirements_ref.to_dict(),
            },
            artifact_type="ContractArtifact",
            provenance={
                "architecture_revision_id": revision.aggregate_id,
                "role": "architect",
            },
            child_refs=((requirements_ref.sha256, "requirements"),)
            + (
                (
                    (
                        str(
                            dict(
                                submission.get("graph_source_map_ref")
                                or {}
                            ).get("sha256")
                            or ""
                        ),
                        "graph_source_map",
                    ),
                )
                if dict(
                    submission.get("graph_source_map_ref") or {}
                ).get("sha256")
                else ()
            ),
        )
        checklist_ref = self.artifacts.put_json(
            checklist,
            artifact_type="ArchitectWorkChecklistArtifact",
            provenance={
                "role": "architect",
                "authority": "work_cursor_only",
            },
            child_refs=((result_ref.sha256, "architecture_contract"),),
        )
        return checklist_ref, current, requirements_ref, result_ref, revision_base_manifest_ref

    def admit_architecture_stage(
        self, activation: Any, effect: Mapping[str, Any], invocation_id: str, lease: Any, lease_resource: str,
        rebind_action: Any, revision: AggregateSnapshot, start_action: Any,
    ) -> AggregateSnapshot:
        action_type = (
            start_action
            if start_action in self.repository.transitions.legal_actions(
                AggregateType.ARCHITECTURE_REVISION,
                revision.state,
            )
            else rebind_action
        )
        with self.repository.transaction() as connection:
            revision = (connection or self.repository).transitions.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=revision.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision.aggregate_id,
                    actor=invocation_id,
                    expected_version=revision.version,
                    idempotency_key=(
                        f"effect:{effect['effect_key']}:{action_type.lower()}:"
                        f"{lease.fencing_token}"
                    ),
                    payload={
                        "fencing_token": lease.fencing_token,
                        "active_worker_id": invocation_id,
                        "lease_resource_key": lease_resource,
                        "active_role": OrchestrationRole.ARCHITECT.value,
                        "active_role_mode": activation.mode.value,
                    },
                ),
            ).snapshot
            self.plan_assignments.start_plan_cycle_assignment(
                workflow_id=revision.workflow_id,
                effect=effect,
                slot=CycleSlot.PRODUCER,
                unit_of_work=connection,
            )
        return revision
