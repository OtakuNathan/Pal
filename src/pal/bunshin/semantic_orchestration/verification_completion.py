from __future__ import annotations
import pal.bunshin.execution_values as _dependency_execution_values
from pal.bunshin.semantic_orchestration.verification_workspace import _verification_workspace_changed_paths
from pal.bunshin.semantic_orchestration.verification_workspace import _verification_scratch_paths
from pal.bunshin.semantic_orchestration.verification_workspace import _verification_corpus_files
from pal.bunshin.semantic_orchestration.worker_results import _recorded_role_metrics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.adapters import SOFTWARE_GIT_ADAPTER
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, SubmissionInvariantError
from pal.bunshin.swe_verification import semantic_verification_submission_errors, verification_finding_route_errors
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.review_findings import structured_advisories, structured_findings
from pal.bunshin.role_protocol import RoleAssignmentState, stable_hash
from pal.bunshin.semantic_orchestration.assignment_identity import AssignmentIdentity
from pal.bunshin.semantic_orchestration.role_reports import RoleReports
from pal.bunshin.semantic_orchestration.verification_policy import _verification_repair_scope


@dataclass
class VerificationCompletion:
    assignment_identity: AssignmentIdentity
    role_reports: RoleReports
    artifacts: ContentAddressedArtifactStore
    repository: BunshinRepository

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
        prompt_pack = self.artifacts.read_json(prompt_ref)
        receipt_workspace = dict(prompt_pack.get("workspace") or {})
        receipt_workspace.update(repo_path=str(review_workspace), review_scratch_dir=str(review_scratch),
                                 verification_scratch_only=scratch_only)
        receipt_workspace["bunshin_v2"] = {
            **dict(dict(prompt_pack.get("metadata") or {}).get("bunshin_v2") or {}),
            **dict(receipt_workspace.get("bunshin_v2") or {}),
        }
        receipt_workspace["bunshin_v2"]["swe_verification_tool_contract"] = {
            **dict(receipt_workspace["bunshin_v2"].get("swe_verification_tool_contract") or {}),
            **_verification_repair_scope(self.repository, node),
        }
        accepted_legacy_receipt = self._bind_accepted_receipt(node, submission, terminal, receipt_workspace)
        errors = semantic_verification_submission_errors(
            submission,
            work_view=work_view,
            changed_paths=changed_paths,
            current_case_paths=current_case_paths,
            corpus_scope=corpus_scope,
            scratch_only=scratch_only,
            workspace=receipt_workspace,
            accepted_legacy_receipt=accepted_legacy_receipt,
        )
        # This role already has a durable submission receipt. A legacy pack
        # may have accepted an invalid repair route before the preflight was
        # tightened. Preserve it for snapshot's explicit checker correction;
        # evidence/corpus invariant failures remain manager errors.
        route_errors = set(verification_finding_route_errors(
            structured_findings(submission),
            receipt_workspace["bunshin_v2"]["swe_verification_tool_contract"],
        ))
        errors = tuple(error for error in errors if error not in route_errors)
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

    def _bind_accepted_receipt(self, node, submission, terminal, workspace) -> bool:
        """Only a verified Manager receipt grants legacy validation compatibility."""
        assignment_id = str(dict(terminal.get("payload") or {}).get("role_assignment_id") or "")
        assignment = self.repository.role_assignments.read_role_assignment(assignment_id) if assignment_id else None
        if assignment is None or assignment.get("state") not in {
            RoleAssignmentState.RESULT_RECORDED.value, RoleAssignmentState.SETTLED.value,
        }:
            return False
        receipt_ref = dict(assignment.get("submission_artifact_ref") or {})
        record = self.repository.artifacts.read_artifact_record(str(receipt_ref.get("sha256") or ""))
        identity = {"workflow_id": node.workflow_id, "aggregate_type": node.aggregate_type.value,
                    "aggregate_id": node.aggregate_id, "role": "verifier", "submission_kind": "verification"}
        if (any(assignment.get(key) != value for key, value in identity.items())
                or record is None or not record.get("durable") or receipt_ref.get("durable") is False
                or stable_hash(submission) != assignment.get("submission_payload_hash")
                or self.artifacts.read_json(receipt_ref) != dict(submission)):
            raise SubmissionInvariantError("verifier submission differs from its authoritative assignment receipt")
        workspace["verification_case_revision"] = int(
            dict(submission.get("verification_binding") or {}).get("case_revision") or 0
        )
        return "verification_binding" not in submission

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
