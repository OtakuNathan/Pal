from __future__ import annotations
from pal.bunshin.semantic_orchestration.workflow_requests import WorkflowRequests
from pal.bunshin.semantic_orchestration.worker_results import _named_json_output
from pal.bunshin.semantic_orchestration.worker_results import _recorded_role_metrics
from pal.bunshin.semantic_orchestration.review_results import _parse_skeleton_review
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping
from pal.bunshin.semantic_orchestration.review_results import _bind_architecture_edit_instruction_for_review
from pal.bunshin.semantic_orchestration.workspace_safety import _lease_is_live
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, SubmissionInvariantError
from pal.bunshin.contract_protocol import CONTRACT_ARTIFACT, software_contract_projection
from pal.bunshin.sessions import architecture_reviewer_session_id
from pal.bunshin.skeleton import SkeletonReviewResult, review_architecture_skeleton
from pal.bunshin.cycle_protocol import CycleSlot
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.replan import architecture_revision_finding_value
from pal.bunshin.work_items import submission_work_items
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.semantic_orchestration.assignment_identity import AssignmentIdentity
from pal.bunshin.semantic_orchestration.attempt_execution import AttemptExecution
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.plan_assignments import PlanAssignments
from pal.bunshin.semantic_orchestration.role_reports import RoleReports
from pal.bunshin.semantic_orchestration.workflow_facts import WorkflowFacts
from pal.bunshin.skeleton import GitBackedSkeletonService


@dataclass
class ArchitectureReview:
    assignment_identity: AssignmentIdentity
    attempt_execution: AttemptExecution
    effect_reads: EffectReads
    plan_assignments: PlanAssignments
    role_reports: RoleReports
    workflow_facts: WorkflowFacts
    artifacts: ContentAddressedArtifactStore
    repository: BunshinV2Repository
    requests: WorkflowRequests
    skeleton: GitBackedSkeletonService

    async def run_architecture_review(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = self.effect_reads.effect_snapshot(effect)
        if self.workflow_facts.architecture_worker_suppressed(
            revision,
            running_state="REVIEWING",
            start_action="START_ARCHITECTURE_REVIEW",
        ):
            return {"status": "superseded"}
        manifest_ref = _ref_from_mapping(
            revision.payload.get("architecture_manifest_ref")
        )
        record = self.repository.artifacts.read_artifact_record(manifest_ref.sha256)
        if (
            record is None
            or str(record.get("artifact_type") or "") != CONTRACT_ARTIFACT
        ):
            raise SubmissionInvariantError(
                "architecture review requires the current ContractArtifact"
            )
        return await self.run_contract_architecture_review(
            effect, revision, manifest_ref
        )

    async def run_contract_architecture_review(
        self,
        effect: Mapping[str, Any],
        revision: AggregateSnapshot,
        manifest_ref: ArtifactRef,
    ) -> Mapping[str, Any]:
        """Review one immutable ContractArtifact without rebuilding its authoring model."""

        artifact = dict(self.artifacts.read_json(manifest_ref))
        if dict(artifact.get("requirements_ref") or {}) != dict(
            revision.payload.get("requirements_ref") or {}
        ):
            raise ValueError(
                "contract reviewer requirements differ from the Architect input"
            )
        contract = dict(artifact.get("contract") or {})
        contract_schema = str(
            artifact.get("contract_schema") or ""
        )
        invocation_id = architecture_reviewer_session_id(
            revision.workflow_id,
            revision.aggregate_id,
            revision.payload,
        )
        lease_resource = f"architecture:{revision.aggregate_id}:review"
        if revision.state == "REVIEWING":
            active = self.repository.leases.read_lease(lease_resource)
            if (
                active
                and str(active.get("owner_id") or "")
                and _lease_is_live(active)
            ):
                self.plan_assignments.start_plan_cycle_assignment(
                    workflow_id=revision.workflow_id,
                    effect=effect,
                    slot=CycleSlot.CHECKER,
                )
                return {
                    "status": "already_running",
                    "active_worker_id": str(active["owner_id"]),
                }
        lease = self.repository.leases.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": revision.workflow_id,
                "aggregate_id": revision.aggregate_id,
                "stage": "contract_review",
            },
        )
        review_workspace = None
        try:
            revision = self.admit_architecture_review(effect, invocation_id, lease, lease_resource, revision)
            references, requirements_ref = self.publish_review_inputs(artifact, contract, manifest_ref)
            if self.workflow_facts.uses_git_skeleton(revision.workflow_id):
                projected = {
                    **artifact,
                    "submission": software_contract_projection(
                        contract
                    ),
                }
                requirements_payload = self.artifacts.read_json(
                    requirements_ref
                )
                review_workspace = self.skeleton.provision_review_worktree(
                    artifact=projected,
                    workflow_id=revision.workflow_id,
                    review_name=(
                        f"{revision.aggregate_id}-{manifest_ref.sha256[:12]}"
                    ),
                )
                mechanical = review_architecture_skeleton(
                    projected,
                    worktree=review_workspace.worktree,
                    requirements_payload=requirements_payload,
                )
                if mechanical.verdict != "PASS":
                    review_ref = self.artifacts.put_json(
                        mechanical.to_dict(),
                        artifact_type="ContractReviewArtifact",
                        child_refs=((manifest_ref.sha256, "contract"),),
                    )
                    self.dispatch_architecture_review_result(
                        revision,
                        mechanical,
                        review_ref,
                        effect=effect,
                    )
                    return {"result_artifact_ref": review_ref.to_dict()}
                diff_text = subprocess.check_output(
                    [
                        "git",
                        "-C",
                        str(review_workspace.worktree),
                        "diff",
                        "--find-renames",
                        str(artifact.get("base_commit_sha") or ""),
                        str(artifact.get("skeleton_commit_sha") or ""),
                        "--",
                    ],
                    text=True,
                )
                references["contract_diff"] = self.artifacts.put_bytes(
                    diff_text.encode("utf-8"),
                    artifact_type="ContractDeclarationDiffArtifact",
                    media_type="text/x-diff",
                    child_refs=((manifest_ref.sha256, "contract"),),
                )
                workspace_override = {
                    "kind": "existing_repo",
                    "repo_path": str(review_workspace.worktree),
                    "workspace_binding": "canonical",
                    "project_name": "contract-review",
                }
            else:
                workflow = self.repository.snapshots.read_snapshot(
                    AggregateType.WORKFLOW,
                    revision.workflow_id,
                )
                request = self.requests.read(workflow)
                workspace_override = dict(request.get("workspace") or {})
            self.attach_revision_evidence(references, revision)
            return await self.run_and_publish_architecture_review(
                contract_schema, effect, invocation_id, lease, lease_resource, manifest_ref, references, revision,
                workspace_override,
            )
        finally:
            if review_workspace is not None:
                review_workspace.cleanup()
            try:
                self.repository.leases.release_lease(
                    lease_resource,
                    invocation_id,
                    lease.fencing_token,
                )
            except Exception:
                pass

    def dispatch_architecture_review_result(
        self,
        revision: AggregateSnapshot,
        review: SkeletonReviewResult,
        review_ref: ArtifactRef,
        *,
        effect: Mapping[str, Any] | None = None,
        assignment_id: str = "",
    ) -> None:
        current = self.repository.snapshots.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
        if current is None:
            raise ValueError("architecture revision disappeared during review")
        if review.verdict == "PASS":
            action_type = "ARCHITECTURE_REVIEW_PASSED"
            payload = {
                "review_artifact_ref": review_ref.to_dict(),
                "architecture_manifest_ref": current.payload["architecture_manifest_ref"],
            }
        else:
            action_type = "ARCHITECTURE_REVIEW_FAILED"
            payload = {
                "finding_artifact_ref": review_ref.to_dict(),
                "findings": [item.to_dict() for item in review.findings],
            }
        review_generation = int(
            current.payload.get("architecture_review_generation") or 0
        )
        with self.repository.transaction() as connection:
            (connection or self.repository).transitions.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=current.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=current.aggregate_id,
                    actor="bunshin-v2-architecture-reviewer",
                    expected_version=current.version,
                    idempotency_key=(
                        f"architecture-review:{current.aggregate_id}:"
                        f"generation-{review_generation}:{review_ref.sha256}"
                    ),
                    payload=payload,
                ),
                **self.assignment_identity.role_submission_settlement(
                    effect or {},
                    assignment_id=assignment_id,
                ),
            )
            WorkflowCoordinator(self.repository).submit_plan_verdict(
                workflow_id=current.workflow_id,
                accepted=review.verdict == "PASS",
                finding_refs=(
                    ()
                    if review.verdict == "PASS"
                    else (review_ref.sha256,)
                ),
                unit_of_work=connection,
            )

    async def run_and_publish_architecture_review(
        self, contract_schema: Any, effect: Mapping[str, Any], invocation_id: str, lease: Any, lease_resource: str,
        manifest_ref: ArtifactRef, references: dict[str, ArtifactRef], revision: AggregateSnapshot,
        workspace_override: dict[str, Any] | None,
    ) -> Mapping[str, Any]:
        terminal, prompt_ref, terminal_ref = await self.attempt_execution.run_profile(
            effect=effect,
            snapshot=revision,
            invocation_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=lease.fencing_token,
            profile=self.workflow_facts.profile_for_role(
                revision.workflow_id,
                "reviewer",
            ),
            activation=RoleActivation(
                OrchestrationRole.REVIEWER,
                RoleMode.ARCHITECTURE,
            ),
            instruction=(
                "Review the complete immutable contract against the same "
                "ordered task ledger used by the Architect. Use contract "
                "as the module-level semantic truth and, when present, "
                "contract_diff plus public declarations/comments as the "
                "symbol-level truth. Audit every requirement and module "
                "breadth-first, then trace success and material failure "
                "paths through the contract graph. Regress every bound "
                "prior finding and every touched accepted invariant before "
                "reviewing the current diff and its affected semantic "
                "neighborhood for new defects. A prior PASS is context, "
                "never evidence for this Candidate. Reuse unchanged "
                "investigation instead of rereading it. Record all "
                "independent defects with add_finding, complete the "
                "checklist, and call submit_review exactly once. Do not "
                "repair or privately redesign the implementation."
            ),
            reference_refs=references,
            workspace_override=workspace_override,
            prepare_workspace=bool(workspace_override),
        )
        payload = _named_json_output(
            terminal,
            "contract_review.json",
        )
        semantic = _parse_skeleton_review(payload)
        review_ref = self.artifacts.put_json(
            {
                **semantic.to_dict(),
                "contract_schema": contract_schema,
                "work_items": submission_work_items(
                    payload.get("work_items")
                ),
            },
            artifact_type="ContractReviewArtifact",
            child_refs=((manifest_ref.sha256, "contract"),),
        )
        self.dispatch_architecture_review_result(
            revision,
            semantic,
            review_ref,
            effect=effect,
            assignment_id=self.assignment_identity.terminal_role_assignment_id(terminal),
        )
        self.role_reports.record_role_turn(
            terminal=terminal,
            invocation_id=invocation_id,
            fencing_token=lease.fencing_token,
            turn_index=1,
            llm_request_ref=prompt_ref.to_dict(),
            llm_response_ref=terminal_ref.to_dict(),
            tool_summary_ref=review_ref.to_dict(),
            **_recorded_role_metrics(terminal),
        )
        return {
            "provider_request_id": invocation_id,
            "result_artifact_ref": review_ref.to_dict(),
        }

    def publish_review_inputs(self, artifact: Any, contract: Any, manifest_ref: ArtifactRef) -> tuple[dict[str, ArtifactRef], Any]:
        requirements_ref = _ref_from_mapping(artifact["requirements_ref"])
        contract_view_ref = self.artifacts.put_json(
            {
                "schema_version": "1",
                "contract": contract,
            },
            artifact_type="ContractReviewViewArtifact",
            provenance={"owner": "manager", "audience": "reviewer"},
            child_refs=((manifest_ref.sha256, "contract"),),
        )
        references: dict[str, ArtifactRef] = {
            "task": requirements_ref,
            "contract": contract_view_ref,
        }
        workspace_override: dict[str, Any] | None = None
        return references, requirements_ref

    def admit_architecture_review(self, effect: Mapping[str, Any], invocation_id: str, lease: Any, lease_resource: str, revision: AggregateSnapshot) -> AggregateSnapshot:
        action_type = (
            "START_ARCHITECTURE_REVIEW"
            if "START_ARCHITECTURE_REVIEW"
            in self.repository.transitions.legal_actions(
                AggregateType.ARCHITECTURE_REVISION,
                revision.state,
            )
            else "REBIND_ARCHITECTURE_REVIEW"
        )
        with self.repository.transaction() as connection:
            from pal.bunshin.imported_plan import bind_imported_plan_product
            bind_imported_plan_product(repository=self.repository, artifacts=self.artifacts,
                                       revision=revision, unit_of_work=connection)
            revision = (connection or self.repository).transitions.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=revision.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision.aggregate_id,
                    actor=invocation_id,
                    expected_version=revision.version,
                    idempotency_key=(
                        f"effect:{effect['effect_key']}:"
                        f"{action_type.lower()}:{lease.fencing_token}"
                    ),
                    payload={
                        "fencing_token": lease.fencing_token,
                        "active_worker_id": invocation_id,
                        "lease_resource_key": lease_resource,
                        "active_role": OrchestrationRole.REVIEWER.value,
                        "active_role_mode": RoleMode.ARCHITECTURE.value,
                    },
                ),
            ).snapshot
            self.plan_assignments.start_plan_cycle_assignment(
                workflow_id=revision.workflow_id,
                effect=effect,
                slot=CycleSlot.CHECKER,
                unit_of_work=connection,
            )
        return revision

    def attach_revision_evidence(self, references: Any, revision: AggregateSnapshot) -> None:
        _bind_architecture_edit_instruction_for_review(
            references,
            revision,
        )
        finding_value = architecture_revision_finding_value(
            revision.payload
        )
        if finding_value:
            references["prior_finding"] = (
                self.role_reports.publish_architecture_finding_view(
                    finding_value,
                    audience="reviewer",
                )
            )
        root_batch_value = revision.payload.get("replan_finding_batch_ref")
        if root_batch_value:
            references["replan_finding_batch"] = (
                self.role_reports.publish_architecture_finding_view(
                    root_batch_value,
                    audience="reviewer",
                )
            )
