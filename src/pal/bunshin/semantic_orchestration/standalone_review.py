from __future__ import annotations
from pal.bunshin.artifacts import ArtifactRef
from pal.bunshin.semantic_orchestration.workflow_requests import WorkflowRequests
from pal.bunshin.semantic_orchestration.verification_workspace import _prepare_standalone_review_workspace
from pal.bunshin.semantic_orchestration.worker_results import _named_json_output
from pal.bunshin.semantic_orchestration.worker_results import _recorded_role_metrics
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateType, SubmissionInvariantError
from pal.bunshin.contract_protocol import CONTRACT_ARTIFACT, software_contract_projection
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.verification import VerificationStatus
from pal.bunshin.work_items import submission_work_items
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.semantic_orchestration.architecture_review import ArchitectureReview
from pal.bunshin.semantic_orchestration.assignment_identity import AssignmentIdentity
from pal.bunshin.semantic_orchestration.attempt_execution import AttemptExecution
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.role_leases import RoleLeases
from pal.bunshin.semantic_orchestration.role_reports import RoleReports
from pal.bunshin.semantic_orchestration.workflow_facts import WorkflowFacts
from pal.bunshin.skeleton import GitBackedSkeletonService


@dataclass
class StandaloneReview:
    architecture_review: ArchitectureReview
    assignment_identity: AssignmentIdentity
    attempt_execution: AttemptExecution
    effect_reads: EffectReads
    role_leases: RoleLeases
    role_reports: RoleReports
    workflow_facts: WorkflowFacts
    artifacts: ContentAddressedArtifactStore
    repository: BunshinRepository
    requests: WorkflowRequests
    runtime_root: Path
    skeleton: GitBackedSkeletonService

    async def run_reviewer_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        mode = RoleMode(self.effect_reads.effect_role_mode(effect))
        if mode == RoleMode.ARCHITECTURE:
            return await self.architecture_review.run_architecture_review(effect)
        if mode == RoleMode.STANDALONE:
            return await self.run_standalone_review(effect)
        raise ValueError(f"unsupported reviewer mode: {mode.value}")

    async def run_standalone_review(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        review = self.effect_reads.effect_snapshot(effect)
        review = await self.role_leases.ensure_standalone_review_lease(review)
        invocation_id = str(review.payload.get("active_worker_id") or "")
        lease_resource = str(review.payload.get("lease_resource_key") or "")
        fencing_token = int(review.payload.get("fencing_token") or 0)
        request_ref = _ref_from_mapping(review.payload.get("review_request_ref"))
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, review.workflow_id)
        request = self.requests.read(workflow)
        request_record = self.repository.artifacts.read_artifact_record(request_ref.sha256)
        contract_review = bool(
            request_record
            and str(request_record.get("artifact_type") or "") == CONTRACT_ARTIFACT
        )
        contract_review_workspace = None
        workspace = dict(request.get("workspace") or {})
        if contract_review:
            artifact = dict(self.artifacts.read_json(request_ref))
            requirements_ref = _ref_from_mapping(artifact.get("requirements_ref"))
            contract = dict(artifact.get("contract") or {})
            review_view_ref = self.artifacts.put_json(
                {
                    "schema_version": "1",
                    "contract": contract,
                },
                artifact_type="ContractReviewViewArtifact",
                provenance={"owner": "manager", "audience": "standalone_reviewer"},
                child_refs=((request_ref.sha256, "contract"),),
            )
            reviewer_inputs = {
                "task": requirements_ref,
                "contract": review_view_ref,
            }
            if self.workflow_facts.uses_git_skeleton(review.workflow_id):
                projected = {
                    **artifact,
                    "submission": software_contract_projection(contract),
                }
                contract_review_workspace = (
                    self.skeleton.provision_review_worktree(
                        artifact=projected,
                        review_name=f"standalone-{review.aggregate_id}",
                    )
                )
                review_repo = contract_review_workspace.worktree
                review_scratch = (
                    contract_review_workspace.root / "review-scratch"
                )
                review_scratch.mkdir(parents=True, exist_ok=True)
                base_sha = str(artifact.get("skeleton_commit_sha") or "")
            else:
                repo_path = str(
                    workspace.get("repo_path")
                    or workspace.get("cwd")
                    or self.runtime_root
                )
                review_repo, review_scratch, base_sha = (
                    _prepare_standalone_review_workspace(
                        self.runtime_root,
                        review.aggregate_id,
                        Path(repo_path),
                    )
                )
        else:
            repo_path = str(workspace.get("repo_path") or workspace.get("cwd") or self.runtime_root)
            review_repo, review_scratch, base_sha = _prepare_standalone_review_workspace(
                self.runtime_root,
                review.aggregate_id,
                Path(repo_path),
            )
            reviewer_inputs = {"review_request": request_ref}
        terminal, prompt_ref, terminal_ref = await self.attempt_execution.run_profile(
            effect=effect,
            snapshot=review,
            invocation_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            profile=self.workflow_facts.profile_for_role(review.workflow_id, "reviewer"),
            activation=RoleActivation(OrchestrationRole.REVIEWER, RoleMode.STANDALONE),
            instruction="Perform the requested standalone review. Report evidence-grounded findings and do not modify the target. Repair is a separate explicit workflow.",
            reference_refs=reviewer_inputs,
            workspace_override={
                "kind": "existing_repo",
                "repo_path": str(review_repo),
                "workspace_binding": (
                    "canonical"
                    if contract_review_workspace is not None
                    else "ephemeral_artifact"
                ),
                "project_name": "standalone-review",
                "review_scratch_dir": str(review_scratch),
            },
            prepare_workspace=True,
        )
        report_ref = self.record_standalone_review(base_sha, effect, fencing_token, invocation_id, prompt_ref, request_ref, review, review_scratch, terminal, terminal_ref)
        if contract_review_workspace is not None:
            contract_review_workspace.cleanup()
        self.repository.leases.release_lease(lease_resource, invocation_id, fencing_token)
        return {"result_artifact_ref": report_ref.to_dict()}

    def record_standalone_review(
        self, base_sha: Any, effect: Mapping[str, Any], fencing_token: int, invocation_id: str, prompt_ref: Any,
        request_ref: Any, review: Any, review_scratch: Any, terminal: Any, terminal_ref: Any,
    ) -> ArtifactRef:
        payload = _named_json_output(terminal, "contract_review.json")
        try:
            verdict = str(payload.get("verdict") or "").strip().upper()
            if verdict not in {"PASS", "FAIL"}:
                raise ValueError("review verdict must be PASS or FAIL")
            findings = [
                dict(item)
                for item in list(payload.get("findings") or [])
                if isinstance(item, Mapping)
            ]
            advisories = [
                dict(item)
                for item in list(payload.get("advisories") or [])
                if isinstance(item, Mapping)
            ]
            if verdict == "PASS" and findings:
                raise ValueError("PASS review cannot contain blocking findings")
            if verdict == "FAIL" and not findings:
                raise ValueError("FAIL review requires at least one finding")
        except Exception as exc:
            raise SubmissionInvariantError(
                f"accepted submit_review failed manager defense-in-depth validation: {exc}"
            ) from exc
        test_workspace_ref = self.role_reports.publish_verification_evidence(
            review_scratch=review_scratch,
            candidate_identity=base_sha,
        )
        status = (
            VerificationStatus.PASS
            if verdict == "PASS"
            else VerificationStatus.FAIL
        )
        report_ref = self.artifacts.put_json(
            {
                "schema_version": "1",
                "status": status.value,
                "findings": findings,
                "advisories": advisories,
                "work_items": submission_work_items(payload.get("work_items")),
            },
            artifact_type="StandaloneReviewReportArtifact",
            child_refs=(
                (request_ref.sha256, "review_request"),
                (test_workspace_ref.sha256, "test_workspace"),
            ),
        )
        self.role_reports.record_role_turn(
            terminal=terminal,
            invocation_id=invocation_id,
            fencing_token=fencing_token,
            turn_index=1,
            llm_request_ref=prompt_ref.to_dict(),
            llm_response_ref=terminal_ref.to_dict(),
            tool_summary_ref=report_ref.to_dict(),
            **_recorded_role_metrics(terminal),
        )
        current = self.repository.snapshots.read_snapshot(AggregateType.STANDALONE_REVIEW, review.aggregate_id)
        self.repository.transitions.dispatch(
            ActionEnvelope(
                action_type="REPORT_PRODUCED",
                workflow_id=review.workflow_id,
                aggregate_type=AggregateType.STANDALONE_REVIEW,
                aggregate_id=review.aggregate_id,
                actor=invocation_id,
                expected_version=current.version,
                idempotency_key=f"standalone-report:{report_ref.sha256}",
                payload={"verification_artifact_ref": report_ref.to_dict()},
            ),
            **self.assignment_identity.role_submission_settlement(
                effect,
                assignment_id=self.assignment_identity.terminal_role_assignment_id(terminal),
            ),
        )
        return report_ref
