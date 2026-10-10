from __future__ import annotations
from pal.bunshin.artifacts import ArtifactRef
from pal.bunshin.contracts import AggregateSnapshot
from pal.bunshin.semantic_orchestration.assignment_rules import _implementation_action_idempotency_key
from pal.bunshin.semantic_orchestration.role_environment import _workspace_tooling_from_work_view
from pal.bunshin.semantic_orchestration.verification_workspace import _verification_corpus_files
from pal.bunshin.semantic_orchestration.verification_workspace import _ensure_workspace_directory
from pal.bunshin.semantic_orchestration.worker_results import _primary_json_output
from pal.bunshin.semantic_orchestration.worker_results import _recorded_role_metrics
from pal.bunshin.semantic_orchestration.worker_results import _role_session_turn_index
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping
from pal.bunshin.semantic_orchestration.verification_policy import _validate_skeleton_coder_report
from pal.bunshin.semantic_orchestration.verification_policy import _reject_manager_identity_fields
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.adapters import SOFTWARE_GIT_ADAPTER
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateType, SubmissionInvariantError
from pal.bunshin.work_views import UnitWorkViewBuilder
from pal.bunshin.skeleton import compiled_module_write_scopes
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.verification import repair_bill_semantic_view
from pal.bunshin.work_items import submission_work_items
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.semantic_orchestration.assignment_identity import AssignmentIdentity
from pal.bunshin.semantic_orchestration.attempt_execution import AttemptExecution
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.role_leases import RoleLeases
from pal.bunshin.semantic_orchestration.role_reports import RoleReports
from pal.bunshin.semantic_orchestration.workflow_facts import WorkflowFacts
from pal.bunshin.contract_runtime import ContractArtifactAccess


@dataclass
class ImplementationRun:
    assignment_identity: AssignmentIdentity
    attempt_execution: AttemptExecution
    effect_reads: EffectReads
    role_leases: RoleLeases
    role_reports: RoleReports
    workflow_facts: WorkflowFacts
    artifacts: ContentAddressedArtifactStore
    contracts: ContractArtifactAccess
    repository: BunshinRepository

    async def run_implementation_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        mode = RoleMode(self.effect_reads.effect_role_mode(effect))
        return await self.run_implementation(effect, repair=mode == RoleMode.REPAIR)

    async def run_implementation(self, effect: Mapping[str, Any], *, repair: bool) -> Mapping[str, Any]:
        node = self.effect_reads.effect_snapshot(effect)
        node = await self.role_leases.ensure_node_effect_lease(
            node,
            action_type="REBIND_REPAIRER" if repair else "REBIND_PRODUCER",
            activation=RoleActivation(
                OrchestrationRole.IMPLEMENTATION,
                RoleMode.REPAIR if repair else RoleMode.PRODUCE,
            ),
        )
        invocation_id = str(node.payload.get("active_worker_id") or "")
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        fencing_token = int(node.payload.get("fencing_token") or 0)
        self.repository.leases.assert_fencing_token(lease_resource, invocation_id, fencing_token)
        skeleton_manifest = (
            self.workflow_facts.execution_adapter(node) == SOFTWARE_GIT_ADAPTER
        )
        self.role_reports.write_node_journal(
            node,
            owner_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            updates={
                "current_micro_plan": (
                    ["reproduce RepairBill", "apply minimal repair", "run focused regression"]
                    if repair
                    else [
                        (
                            "inspect ModuleWorkView"
                            if skeleton_manifest
                            else "inspect UnitWorkView"
                        ),
                        "implement contract",
                        "run focused self-checks",
                    ]
                ),
                "last_safe_point": "worker_started",
            },
        )
        coder_read_only_overlays, instruction, path_policy, references, view_ref, work_view = self.prepare_implementation_inputs(node, repair, skeleton_manifest)
        terminal, prompt_ref, terminal_ref = await self.attempt_execution.run_profile(
            effect=effect,
            snapshot=node,
            invocation_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            profile=self.workflow_facts.profile_for_role(node.workflow_id, "implementation"),
            activation=RoleActivation(
                OrchestrationRole.IMPLEMENTATION,
                RoleMode.REPAIR if repair else RoleMode.PRODUCE,
            ),
            instruction=instruction,
            reference_refs=references,
            workspace_override={
                "kind": "existing_repo",
                "repo_path": str(node.payload.get("workspace_path") or ""),
                "workspace_binding": "canonical",
                "project_name": str(node.payload.get("unit_id") or "unit"),
                **_workspace_tooling_from_work_view(work_view),
                "write_path_scopes": list(compiled_module_write_scopes(path_policy)),
                "read_only_overlay_paths": coder_read_only_overlays,
            },
            prepare_workspace=True,
        )
        report_ref, status = self.record_candidate_evidence(
            fencing_token, invocation_id, lease_resource, node, prompt_ref, skeleton_manifest, terminal, terminal_ref,
            view_ref, work_view,
        )
        current = self.repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
        if status == "task_blocked" or (
            node.payload.get("execution_mode") == "direct" and status in {"architecture_defect", "module_split_request"}
        ):
            from pal.bunshin.workflow_runtime import WorkflowCoordinator
            from pal.bunshin.direct_contract import task_requirement_blocker
            if node.payload.get("execution_mode") != "direct":
                raise ValueError("task blocker requires direct mode")
            with self.repository.transaction() as connection:
                WorkflowCoordinator(self.repository).require_node_triage(
                    workflow_id=node.workflow_id, node_name=str(node.payload["module_name"]),
                    unit_of_work=connection,
                )
                (connection or self.repository).transitions.dispatch(
                    ActionEnvelope(
                        action_type="ENTER_TRIAGE", workflow_id=node.workflow_id,
                        aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node.aggregate_id,
                        actor=invocation_id, expected_version=current.version,
                        idempotency_key=f"direct-blocker:{node.aggregate_id}:{report_ref.sha256}",
                        payload={"finding_artifact_ref": report_ref.to_dict(),
                                 "blocker": task_requirement_blocker(self.artifacts, report_ref)},
                    ),
                    **self.assignment_identity.role_submission_settlement(
                        effect, assignment_id=self.assignment_identity.terminal_role_assignment_id(terminal)),
                )
            self.repository.leases.release_lease(lease_resource, invocation_id, fencing_token)
            return {"provider_request_id": invocation_id, "result_artifact_ref": report_ref.to_dict()}
        if status in {"architecture_defect", "module_split_request"}:
            self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="PRODUCER_ARCHITECTURE_DEFECT",
                    workflow_id=node.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor=invocation_id,
                    expected_version=current.version,
                    idempotency_key=_implementation_action_idempotency_key(
                        "defect",
                        node.aggregate_id,
                        int(node.payload.get("candidate_cycle") or 0),
                        report_ref.sha256,
                    ),
                    payload={"finding_artifact_ref": report_ref.to_dict()},
                ),
                **self.assignment_identity.role_submission_settlement(
                    effect,
                    assignment_id=self.assignment_identity.terminal_role_assignment_id(terminal),
                ),
            )
            self.repository.leases.release_lease(lease_resource, invocation_id, fencing_token)
            return {"provider_request_id": invocation_id, "result_artifact_ref": report_ref.to_dict()}
        if status != "candidate_ready":
            raise ValueError(f"producer report has unsupported status: {status}")
        self.repository.transitions.dispatch(
            ActionEnvelope(
                action_type="SUBMIT_CANDIDATE",
                workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node.aggregate_id,
                actor=invocation_id,
                expected_version=current.version,
                idempotency_key=_implementation_action_idempotency_key(
                    "submit",
                    node.aggregate_id,
                    int(node.payload.get("candidate_cycle") or 0),
                    report_ref.sha256,
                ),
                payload={
                    "fencing_token": fencing_token,
                    "producer_report_ref": report_ref.to_dict(),
                    "unit_work_view_ref": view_ref.to_dict(),
                },
            ),
            **self.assignment_identity.role_submission_settlement(
                effect,
                assignment_id=self.assignment_identity.terminal_role_assignment_id(terminal),
            ),
        )
        return {"provider_request_id": invocation_id, "result_artifact_ref": report_ref.to_dict()}

    def record_candidate_evidence(
        self, fencing_token: int, invocation_id: str, lease_resource: str, node: AggregateSnapshot, prompt_ref: Any,
        skeleton_manifest: Any, terminal: Any, terminal_ref: Any, view_ref: Any, work_view: Any,
    ) -> tuple[ArtifactRef, Any]:
        report = _primary_json_output(terminal)
        if skeleton_manifest:
            # Durable role receipts survive Manager upgrades and are replayed
            # during triage recovery.  Re-apply the current handoff projection
            # before defense-in-depth validation so an older receipt cannot
            # leak Manager-owned WorkItem identity into the business action.
            report = {
                **report,
                "work_items": submission_work_items(
                    report.get("work_items")
                ),
            }
            try:
                _validate_skeleton_coder_report(
                    report,
                    expected_module=str(node.payload.get("module_name") or node.payload.get("unit_id") or ""),
                    work_view=work_view,
                )
                _reject_manager_identity_fields(report, owner="Coder output")
            except Exception as exc:
                raise SubmissionInvariantError(
                    f"accepted submit_candidate failed manager defense-in-depth validation: {exc}"
                ) from exc
        status = str(report.get("status") or "candidate_ready").strip().lower()
        report_ref = self.artifacts.put_json(
            report,
            artifact_type="ProducerReportArtifact",
            child_refs=(
                (
                    view_ref.sha256,
                    "module_work_view" if skeleton_manifest else "unit_work_view",
                ),
            ),
        )
        self.role_reports.write_node_journal(
            node,
            owner_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            updates={
                "current_micro_plan": [
                    str(dict(item or {}).get("step") or "")
                    for item in list(
                        dict(report.get("checklist") or {}).get("plan") or []
                    )
                    if str(dict(item or {}).get("status") or "")
                    in {"pending", "in_progress"}
                ],
                "completed_checklist": [
                    str(dict(item or {}).get("step") or "")
                    for item in list(
                        dict(report.get("checklist") or {}).get("plan") or []
                    )
                    if str(dict(item or {}).get("status") or "") == "completed"
                ],
                "files_changed": list(report.get("files_changed") or []),
                "open_questions": [],
                "known_failures": (
                    [str(report.get("summary") or "")]
                    if status != "candidate_ready"
                    else []
                ),
                "last_safe_point": "producer_report_persisted",
            },
        )
        self.role_reports.record_role_turn(
            terminal=terminal,
            invocation_id=invocation_id,
            fencing_token=fencing_token,
            turn_index=_role_session_turn_index(terminal),
            llm_request_ref=prompt_ref.to_dict(),
            llm_response_ref=terminal_ref.to_dict(),
            tool_summary_ref=report_ref.to_dict(),
            **_recorded_role_metrics(terminal),
        )
        return report_ref, status

    def prepare_implementation_inputs(self, node: AggregateSnapshot, repair: bool, skeleton_manifest: Any) -> tuple[Any, Any, Any, Any, Any, Any]:
        view_ref = UnitWorkViewBuilder(self.contracts).build(node)
        work_view = dict(self.artifacts.read_json(view_ref))
        references = {
            "module_work_view" if skeleton_manifest else "unit_work_view": view_ref
        }
        if skeleton_manifest:
            architecture_ref = _ref_from_mapping(
                node.payload.get("architecture_manifest_ref")
            )
            architecture_artifact = self.artifacts.read_json(
                architecture_ref
            )
            task_value = architecture_artifact.get("requirements_ref")
            if not isinstance(task_value, Mapping) or not task_value.get("sha256"):
                raise ValueError(
                    "SWE Coder requires the immutable task ledger as a final fallback"
                )
            references["task"] = _ref_from_mapping(task_value)
        repair_ref = node.payload.get("repair_bill_ref")
        if isinstance(repair_ref, Mapping) and repair_ref.get("sha256"):
            semantic_repair_view = repair_bill_semantic_view(
                self.artifacts,
                repair_ref,
                module_name=str(node.payload.get("module_name") or node.payload.get("unit_id") or ""),
            )
            semantic_repair_view["verifier_tests_are_preinstalled"] = bool(
                node.payload.get("verifier_test_paths")
                or _verification_corpus_files(
                    Path(str(node.payload.get("workspace_path") or "")),
                    dict(
                        dict(node.payload.get("path_policy") or {}).get(
                            "verification_corpus"
                        )
                        or {}
                    ),
                )
            )
            semantic_repair_ref = self.artifacts.put_json(
                semantic_repair_view,
                artifact_type="RepairBillSemanticViewArtifact",
                provenance={"owner": "manager", "audience": "coder"},
                child_refs=((str(repair_ref["sha256"]), "repair_bill"),),
            )
            references["repair_bill"] = semantic_repair_ref
        instruction = (
            "This is a fresh repair cycle. Read the bound RepairBill, reproduce it, make the smallest contract-preserving repair, "
            "run the affected regressions, and submit a new Candidate. An earlier submission does not settle this cycle."
            if repair
            else "Implement the current bound UnitWorkView, complete its compact checklist and focused checks, then submit the Candidate."
        )
        if skeleton_manifest:
            instruction = (
                "Read reference:module_work_view and the bound RepairBill once, then immediately call update_checklist with the complete "
                "repair micro-plan; Manager appends every finding item. Use that checklist as the work driver, make the smallest local "
                "contract-preserving repair, run the affected durable regressions, and submit a new Candidate. Use reference:task only as "
                "the final fallback for exact product intent that the local contract does not resolve. An earlier submission does not settle this cycle."
                if repair
                else "Read reference:module_work_view once, then immediately call update_checklist with the complete implementation "
                "micro-plan and use its next action as the work driver. Implement the current bound Module Protocol from the Accepted "
                "Skeleton, run the minimum focused checks, and submit the Candidate. Use reference:task only as the final fallback for "
                "exact product intent that the local contract does not resolve."
            )
        if work_view.get("execution_mode") == "direct":
            references.update({name: _ref_from_mapping(ref) for name, ref in
                               dict(work_view.get("direct_reference_refs") or {}).items()})
            instruction = (
                "Read reference:task and reference:module_work_view as the complete task and repository binding. "
                "Use a compact checklist, perform the task within its scope, run focused checks and submit_candidate. "
                "Repair every routed finding when present. Report task contradictions with report_task_blocker."
            )
        path_policy = dict(node.payload.get("path_policy") or {})
        developer_test_path = str(
            dict(path_policy.get("developer_tests") or {}).get("path") or ""
        ).strip()
        verification_corpus_path = str(
            dict(path_policy.get("verification_corpus") or {}).get("path") or ""
        ).strip()
        coder_workspace = Path(str(node.payload.get("workspace_path") or ""))
        for corpus_path in (developer_test_path, verification_corpus_path):
            if corpus_path:
                _ensure_workspace_directory(coder_workspace, corpus_path)
        coder_read_only_overlays = [
            *(
                list(path_policy.get("contract_paths") or [])
                if str(path_policy.get("contract_mode") or "review_guarded")
                == "file_frozen"
                else []
            ),
            *([verification_corpus_path] if verification_corpus_path else []),
        ]
        return coder_read_only_overlays, instruction, path_policy, references, view_ref, work_view
