from __future__ import annotations
import pal.bunshin.v2.execution_values as _dependency_execution_values
from pal.bunshin.v2.semantic_orchestration.verification_workspace import _verification_workspace_changed_paths
from pal.bunshin.v2.semantic_orchestration.verification_workspace import _verification_scratch_paths
from pal.bunshin.v2.semantic_orchestration.verification_workspace import _verification_corpus_files
from pal.bunshin.v2.semantic_orchestration.worker_results import _recorded_role_metrics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.v2.adapters import SOFTWARE_GIT_ADAPTER
from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, SubmissionInvariantError
from pal.bunshin.v2.execution_values import workspace_content_fingerprint
from pal.bunshin.v2.swe_verification import semantic_verification_submission_errors
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.review_findings import structured_advisories, structured_findings
from pal.bunshin.v2.semantic_orchestration.assignment_identity import AssignmentIdentity
from pal.bunshin.v2.semantic_orchestration.role_reports import RoleReports


@dataclass
class VerificationCompletion:
    assignment_identity: AssignmentIdentity
    role_reports: RoleReports
    artifacts: ContentAddressedArtifactStore
    repository: BunshinV2Repository

    def complete_semantic_verifier(
        self,
        *,
        effect: Mapping[str, Any],
        node: AggregateSnapshot,
        invocation_id: str,
        lease_resource: str,
        fencing_token: int,
        candidate_ref: ArtifactRef,
        candidate_digest: str,
        candidate: Mapping[str, Any],
        review_workspace: Path,
        review_scratch: Path,
        execution_adapter: str,
        work_view: Mapping[str, Any],
        submission: Mapping[str, Any],
        terminal: Mapping[str, Any],
        prompt_ref: ArtifactRef,
        terminal_ref: ArtifactRef,
    ) -> Mapping[str, Any]:
        outcome = str(submission.get("outcome") or "").strip()
        scratch_only = execution_adapter != SOFTWARE_GIT_ADAPTER
        changed_paths = (
            _verification_scratch_paths(review_scratch)
            if scratch_only
            else _verification_workspace_changed_paths(review_workspace, candidate_digest)
        )
        corpus_scope = dict(
            dict(node.payload.get("path_policy") or {}).get(
                "verification_corpus"
            )
            or {}
        )
        current_case_paths = (
            _verification_scratch_paths(review_scratch)
            if scratch_only
            else _verification_corpus_files(review_workspace, corpus_scope)
        )
        errors = semantic_verification_submission_errors(
            submission,
            work_view=work_view,
            changed_paths=changed_paths,
            current_case_paths=current_case_paths,
            corpus_scope=corpus_scope,
            scratch_only=scratch_only,
        )
        normalized_submission = dict(submission)
        if errors:
            raise SubmissionInvariantError(
                "semantic verifier submission failed manager validation:\n- "
                + "\n- ".join(errors)
            )
        findings = structured_findings(submission)
        advisories = structured_advisories(submission)
        reason = str(submission.get("reason") or "").strip()
        receipts = [
            dict(item)
            for item in list(submission.get("tool_receipts") or [])
            if isinstance(item, Mapping)
        ]

        settlement = self.assignment_identity.role_submission_settlement(
            effect,
            assignment_id=self.assignment_identity.terminal_role_assignment_id(terminal),
        )
        assignment = self.repository.role_assignments.read_role_assignment(
            settlement["role_assignment_id"]
        )
        if assignment is None:
            raise SubmissionInvariantError("verifier assignment disappeared before quiescing")
        submission_ref = dict(assignment.get("submission_artifact_ref") or {})
        pending_ref = self.publish_pending_verification(
            candidate_digest, candidate_ref, execution_adapter, fencing_token, invocation_id, lease_resource,
            normalized_submission, prompt_ref, review_scratch, review_workspace, settlement, submission_ref,
            terminal_ref,
        )
        self.role_reports.record_role_turn(
            terminal=terminal,
            invocation_id=invocation_id,
            fencing_token=fencing_token,
            turn_index=1,
            llm_request_ref=prompt_ref.to_dict(),
            llm_response_ref=terminal_ref.to_dict(),
            tool_summary_ref=pending_ref.to_dict(),
            **_recorded_role_metrics(terminal),
        )
        current = self.repository.snapshots.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            node.aggregate_id,
        )
        if current is None:
            raise SubmissionInvariantError("verification node disappeared before quiescing")
        self.repository.transitions.dispatch(
            ActionEnvelope(
                action_type="SUBMIT_SEMANTIC_VERIFICATION",
                workflow_id=current.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=current.aggregate_id,
                actor=invocation_id,
                expected_version=current.version,
                idempotency_key=(
                    f"semantic-verification-submit:{current.aggregate_id}:"
                    f"{pending_ref.sha256}"
                ),
                payload={
                    "pending_verification_ref": pending_ref.to_dict(),
                    "role_assignment_id": settlement["role_assignment_id"],
                    "role_submission_payload_hash": settlement[
                        "role_submission_payload_hash"
                    ],
                },
            ),
            **settlement,
        )
        return {
            "provider_request_id": invocation_id,
            "result_artifact_ref": pending_ref.to_dict(),
        }

    def publish_pending_verification(
        self, candidate_digest: str, candidate_ref: ArtifactRef, execution_adapter: str, fencing_token: int,
        invocation_id: str, lease_resource: str, normalized_submission: Any, prompt_ref: ArtifactRef,
        review_scratch: Path, review_workspace: Path, settlement: Any, submission_ref: ArtifactRef,
        terminal_ref: ArtifactRef,
    ) -> Any:
        pending_ref = self.artifacts.put_json(
            {
                "schema_version": "1",
                "submission": normalized_submission,
                "candidate_ref": candidate_ref.to_dict(),
                "implementation_candidate_ref": candidate_ref.to_dict(),
                "candidate_digest": candidate_digest,
                "candidate_git_base": str(
                    candidate_digest
                    if execution_adapter == SOFTWARE_GIT_ADAPTER
                    else ""
                ),
                "submitted_workspace_fingerprint": _dependency_execution_values.workspace_content_fingerprint(
                    review_workspace
                ),
                "review_workspace": str(review_workspace),
                "review_scratch": str(review_scratch),
                "execution_adapter": execution_adapter,
                "invocation_id": invocation_id,
                "lease_resource_key": lease_resource,
                "fencing_token": fencing_token,
                "role_assignment_id": settlement["role_assignment_id"],
                "role_submission_payload_hash": settlement[
                    "role_submission_payload_hash"
                ],
                "submission_ref": submission_ref,
            },
            artifact_type="PendingSemanticVerificationArtifact",
            provenance={"owner": "manager", "source_role": "verifier"},
            child_refs=tuple(
                (str(ref["sha256"]), relation)
                for ref, relation in (
                    (candidate_ref.to_dict(), "candidate"),
                    (submission_ref, "semantic_submission"),
                    (prompt_ref.to_dict(), "prompt_pack"),
                    (terminal_ref.to_dict(), "worker_terminal"),
                )
                if ref.get("sha256")
            ),
        )
        return pending_ref
