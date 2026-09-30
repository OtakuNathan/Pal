from __future__ import annotations
from pal.bunshin.v2.semantic_orchestration.role_inputs import _candidate_tree_fingerprint
from pal.bunshin.v2.semantic_orchestration.verification_workspace import _verification_workspace_changed_paths
from pal.bunshin.v2.semantic_orchestration.verification_workspace import _verification_scratch_paths
from pal.bunshin.v2.semantic_orchestration.verification_workspace import _semantic_path_scope_matches
from pal.bunshin.v2.semantic_orchestration.verification_policy import _resolve_dependency_node_id
from pal.bunshin.v2.semantic_orchestration.verification_policy import _manager_unknown_policy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.v2.adapters import SOFTWARE_GIT_ADAPTER
from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.contracts import AggregateSnapshot, AggregateType, LeaseConflict, StaleFencingToken, SubmissionInvariantError
from pal.bunshin.v2.workflow_runtime import WorkflowCoordinator
from pal.bunshin.v2.graph_executor import FindingClass
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.review_findings import structured_advisories, structured_findings
from pal.bunshin.v2.verification import DefectKind, VerificationService, VerificationStatus, no_progress_detected
from pal.bunshin.v2.verification_builder import dominant_verification_defect_kind
from pal.bunshin.v2.semantic_orchestration.role_reports import RoleReports
from pal.bunshin.v2.semantic_orchestration.verifier_tests import VerifierTests


@dataclass
class VerificationSettlement:
    role_reports: RoleReports
    verifier_tests: VerifierTests
    artifacts: ContentAddressedArtifactStore
    repository: BunshinV2Repository

    def finalize_semantic_verification(
        self,
        *,
        node: AggregateSnapshot,
        pending: Mapping[str, Any],
        submission: Mapping[str, Any],
        candidate_ref: ArtifactRef,
        candidate_digest: str,
        candidate: Mapping[str, Any],
        review_workspace: Path,
        review_scratch: Path,
        execution_adapter: str,
    ) -> Mapping[str, Any]:
        accepted_candidate, accepted_candidate_digest, accepted_candidate_ref, changed_paths, fencing_token, findings, invocation_id, lease_resource, outcome, receipts, receipts_ref, report_ref, scratch_only, status = self.collect_verification_report(
            candidate, candidate_digest, candidate_ref, execution_adapter, node, pending, review_scratch,
            review_workspace, submission,
        )
        defect_kind, fingerprint, module_node_id, repair_node_ids, repair_ref, target_modules = self.publish_repair_evidence(
            accepted_candidate, accepted_candidate_digest, candidate_ref, changed_paths, findings, node, outcome,
            receipts, receipts_ref, report_ref, status, submission,
        )

        current = self.repository.snapshots.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            node.aggregate_id,
        )
        if current is None:
            raise SubmissionInvariantError("verification node disappeared before verdict")
        unknown_policy = _manager_unknown_policy(node)
        coordinator = WorkflowCoordinator(self.repository)
        node_name = str(
            node.payload.get("module_name")
            or node.payload.get("unit_id")
            or ""
        )
        # The node action below publishes effects which may be consumed by a
        # separate Manager loop immediately.  Advance the graph-cycle cursor
        # before dispatching that action; otherwise a REVIEW_FAILED effect can
        # start repair while the cycle still says CHECKER_READY.  This is a
        # prediction of the same mechanical branch used by VerificationService
        # (unknown policy and no-progress are the only blocking branches), not
        # a second semantic verdict.
        failure_history = list(current.payload.get("failure_history") or [])
        if status == VerificationStatus.FAIL:
            failure_history.append(
                {
                    "finding_fingerprint": fingerprint,
                    "candidate_tree_hash": _candidate_tree_fingerprint(
                        accepted_candidate,
                        fallback=accepted_candidate_digest,
                    ),
                }
            )
        blocking_unknown = (
            status == VerificationStatus.UNKNOWN and not unknown_policy.allows()
        )
        blocking_no_progress = (
            status == VerificationStatus.FAIL
            and no_progress_detected(failure_history)
        )
        try:
            self.commit_verification_result(
                accepted_candidate, accepted_candidate_digest, accepted_candidate_ref, blocking_no_progress,
                blocking_unknown, candidate_digest, coordinator, current, defect_kind, fingerprint, invocation_id,
                module_node_id, node, node_name, repair_node_ids, repair_ref, report_ref, scratch_only, status,
                target_modules, unknown_policy,
            )
        finally:
            try:
                self.repository.leases.release_lease(
                    lease_resource,
                    invocation_id,
                    fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass
        return {
            "provider_request_id": invocation_id,
            "result_artifact_ref": report_ref.to_dict(),
        }

    def commit_verification_result(
        self, accepted_candidate: Any, accepted_candidate_digest: Any, accepted_candidate_ref: Any,
        blocking_no_progress: Any, blocking_unknown: Any, candidate_digest: str, coordinator: Any, current: Any,
        defect_kind: Any, fingerprint: Any, invocation_id: str, module_node_id: Any, node: AggregateSnapshot,
        node_name: Any, repair_node_ids: Any, repair_ref: ArtifactRef | None, report_ref: ArtifactRef,
        scratch_only: Any, status: Any, target_modules: Any, unknown_policy: Any,
    ) -> None:
        with self.repository.transaction() as connection:
            if blocking_unknown or blocking_no_progress:
                coordinator.require_node_triage(
                    workflow_id=node.workflow_id,
                    node_name=node_name,
                    unit_of_work=connection,
                )
            else:
                coordinator.checker_verdict(
                    workflow_id=node.workflow_id,
                    node_name=node_name,
                    accepted=(
                        status in {
                            VerificationStatus.PASS,
                            VerificationStatus.NOT_APPLICABLE,
                        }
                        or status == VerificationStatus.UNKNOWN
                    ),
                    finding_refs=(
                        (repair_ref.sha256,)
                        if repair_ref is not None
                        else ()
                    ),
                    finding_class=(
                        None
                        if status
                        in {
                            VerificationStatus.PASS,
                            VerificationStatus.NOT_APPLICABLE,
                            VerificationStatus.UNKNOWN,
                        }
                        else FindingClass(defect_kind.value)
                    ),
                    dependency_node=(
                        target_modules[0]
                        if target_modules
                        and defect_kind == DefectKind.DEPENDENCY
                        else ""
                    ),
                    accepted_product_ref=(
                        accepted_candidate_ref.sha256
                        if status
                        in {
                            VerificationStatus.PASS,
                            VerificationStatus.NOT_APPLICABLE,
                            VerificationStatus.UNKNOWN,
                        }
                        else ""
                    ),
                    unit_of_work=connection,
                )
            verdict_result = VerificationService(
                self.repository,
                self.artifacts,
            ).submit_verdict(
                node=current,
                verification_ref=report_ref,
                status=status,
                actor=invocation_id,
                unknown_policy=unknown_policy,
                repair_bill_ref=repair_ref,
                finding_fingerprint_value=fingerprint if repair_ref is not None else "",
                candidate_tree_hash=_candidate_tree_fingerprint(
                    accepted_candidate,
                    fallback=accepted_candidate_digest,
                ),
                defect_kind=defect_kind,
                dependency_node_id=(
                    repair_node_ids[0]
                    if repair_node_ids and defect_kind == DefectKind.DEPENDENCY
                    else ""
                ),
                dependency_node_ids=(
                    repair_node_ids
                    if defect_kind == DefectKind.DEPENDENCY
                    else ()
                ),
                module_node_id=module_node_id,
                module_node_ids=(repair_node_ids if module_node_id else ()),
                system_fingerprint="",
                accepted_candidate_ref=(
                    accepted_candidate_ref
                    if not scratch_only
                    and accepted_candidate_digest != candidate_digest
                    else None
                ),
                accepted_candidate_digest=(
                    accepted_candidate_digest
                    if not scratch_only
                    and accepted_candidate_digest != candidate_digest
                    else ""
                ),
                unit_of_work=connection,
            )

    def publish_repair_evidence(
        self, accepted_candidate: Any, accepted_candidate_digest: Any, candidate_ref: ArtifactRef, changed_paths: Any,
        findings: Any, node: AggregateSnapshot, outcome: Any, receipts: Any, receipts_ref: Any,
        report_ref: ArtifactRef, status: Any, submission: Mapping[str, Any],
    ) -> tuple[Any, Any, Any, Any, ArtifactRef | None, Any]:
        routed_defect = dominant_verification_defect_kind(findings)
        defect_kind = {
            "contract_revision": DefectKind.CONTRACT,
            "architecture_revision": DefectKind.ARCHITECTURE,
            "requirements_revision": DefectKind.REQUIREMENTS,
        }.get(
            outcome,
            DefectKind(routed_defect)
            if routed_defect
            else DefectKind.MODULE,
        )
        target_modules = [
            str(item).strip()
            for item in list(submission.get("target_modules") or [])
            if str(item).strip()
        ]
        repair_node_ids = [
            _resolve_dependency_node_id(
                self.repository,
                node,
                dependency_module=module_name,
            )
            for module_name in target_modules
        ]
        module_node_id = ""
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "outcome": outcome,
                    "findings": findings,
                    "changed_test_paths": changed_paths,
                    "receipt_hashes": [str(item.get("output_sha256") or "") for item in receipts],
                    "candidate_tree": _candidate_tree_fingerprint(
                        accepted_candidate,
                        fallback=accepted_candidate_digest,
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        repair_ref: ArtifactRef | None = None
        if status == VerificationStatus.FAIL:
            repair_ref = self.artifacts.put_json(
                {
                    "schema_version": "1",
                    "artifact_kind": "semantic_repair_packet",
                    "module_name": str(
                        node.payload.get("module_name") or node.payload.get("unit_id") or ""
                    ),
                    "route": outcome,
                    "target_modules": target_modules,
                    "findings": findings,
                    "candidate_ref": candidate_ref.to_dict(),
                    "verification_ref": report_ref.to_dict(),
                    "changed_test_paths": changed_paths,
                    "tool_receipts_ref": receipts_ref.to_dict(),
                    "regression_commands": [
                        str(dict(item.get("args") or {}).get("cmd") or "")
                        for item in receipts
                        if item.get("kind") == "command"
                        and str(dict(item.get("args") or {}).get("cmd") or "").strip()
                    ],
                },
                artifact_type="RepairPacketArtifact",
                provenance={"owner": "manager", "source_role": "verifier"},
                child_refs=(
                    (report_ref.sha256, "verification"),
                    (receipts_ref.sha256, "tool_receipts"),
                ),
            )
        return defect_kind, fingerprint, module_node_id, repair_node_ids, repair_ref, target_modules

    def collect_verification_report(
        self, candidate: Mapping[str, Any], candidate_digest: str, candidate_ref: ArtifactRef, execution_adapter: str,
        node: AggregateSnapshot, pending: Mapping[str, Any], review_scratch: Path, review_workspace: Path,
        submission: Mapping[str, Any],
    ) -> tuple[Any, Any, Any, Any, int, Any, str, str, Any, Any, Any, ArtifactRef, Any, Any]:
        invocation_id = str(
            node.payload.get("active_worker_id")
            or pending.get("invocation_id")
            or ""
        )
        lease_resource = str(
            node.payload.get("lease_resource_key")
            or pending.get("lease_resource_key")
            or ""
        )
        fencing_token = int(
            node.payload.get("fencing_token")
            or pending.get("fencing_token")
            or 0
        )
        outcome = str(submission.get("outcome") or "").strip()
        findings = structured_findings(submission)
        advisories = structured_advisories(submission)
        reason = str(submission.get("reason") or "").strip()
        scratch_only = execution_adapter != SOFTWARE_GIT_ADAPTER
        candidate_git_base = str(
            pending.get("candidate_git_base") or candidate_digest or ""
        )
        changed_paths = (
            _verification_scratch_paths(review_scratch)
            if scratch_only
            else _verification_workspace_changed_paths(
                review_workspace,
                candidate_digest,
            )
        )
        corpus_scope = dict(
            dict(node.payload.get("path_policy") or {}).get(
                "verification_corpus"
            )
            or {}
        )
        outside = [] if scratch_only else [
            path
            for path in changed_paths
            if not _semantic_path_scope_matches(path, corpus_scope)
        ]
        if outside:
            raise SubmissionInvariantError(
                "verifier snapshot contains paths outside the bound module corpus: "
                + ", ".join(outside)
            )
        receipts = [
            dict(item)
            for item in list(submission.get("tool_receipts") or [])
            if isinstance(item, Mapping)
        ]
        workspace_evidence_ref = (
            self.role_reports.publish_verification_evidence(
                review_scratch=review_scratch,
                candidate_identity=candidate_digest,
            )
            if scratch_only
            else None
        )
        accepted_candidate_ref = candidate_ref
        accepted_candidate_digest = candidate_digest
        accepted_candidate = dict(candidate)
        if not scratch_only and changed_paths:
            (
                accepted_candidate_ref,
                accepted_candidate_digest,
                accepted_candidate,
            ) = self.verifier_tests.checkpoint_verifier_tests(
                node=node,
                review_workspace=review_workspace,
                candidate_ref=candidate_ref,
                candidate=candidate,
                candidate_digest=candidate_digest,
                changed_test_paths=changed_paths,
            )
        receipts_ref = self.artifacts.put_json(
            {
                "schema_version": "1",
                "candidate_digest": candidate_digest,
                "receipts": receipts,
            },
            artifact_type="VerificationToolReceiptSetArtifact",
            provenance={"owner": "manager", "role": "verifier"},
        )
        status = (
            VerificationStatus.PASS
            if outcome == "pass"
            else VerificationStatus.UNKNOWN
            if outcome == "unknown"
            else VerificationStatus.FAIL
        )
        report_payload = {
                "schema_version": "2",
                "module_name": str(
                    node.payload.get("module_name") or node.payload.get("unit_id") or ""
                ),
                "outcome": outcome,
                "status": status.value,
                "findings": findings,
                "advisories": advisories,
                "unknown_reason": reason,
                "changed_test_paths": changed_paths,
                "candidate_ref": accepted_candidate_ref.to_dict(),
                "implementation_candidate_ref": candidate_ref.to_dict(),
                "tool_receipts_ref": receipts_ref.to_dict(),
            }
        if workspace_evidence_ref is not None:
            report_payload["workspace_evidence_ref"] = workspace_evidence_ref.to_dict()
        report_children = [
            (accepted_candidate_ref.sha256, "candidate"),
            (candidate_ref.sha256, "implementation_candidate"),
            (receipts_ref.sha256, "tool_receipts"),
        ]
        if workspace_evidence_ref is not None:
            report_children.append((workspace_evidence_ref.sha256, "workspace_evidence"))
        report_ref = self.artifacts.put_json(
            report_payload,
            artifact_type="VerificationArtifact",
            provenance={"owner": "manager", "source_role": "verifier"},
            child_refs=tuple(report_children),
        )
        return accepted_candidate, accepted_candidate_digest, accepted_candidate_ref, changed_paths, fencing_token, findings, invocation_id, lease_resource, outcome, receipts, receipts_ref, report_ref, scratch_only, status
