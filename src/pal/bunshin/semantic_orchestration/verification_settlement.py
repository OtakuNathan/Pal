from __future__ import annotations
from pal.bunshin.semantic_orchestration.role_inputs import _candidate_tree_fingerprint
from pal.bunshin.semantic_orchestration.verification_workspace import _verification_workspace_changed_paths
from pal.bunshin.semantic_orchestration.verification_workspace import _verification_scratch_paths
from pal.bunshin.semantic_orchestration.verification_workspace import _semantic_path_scope_matches
from pal.bunshin.semantic_orchestration.verification_policy import _resolve_dependency_node_id
from pal.bunshin.semantic_orchestration.verification_policy import _manager_unknown_policy
from pal.bunshin.semantic_orchestration.verification_policy import _verification_repair_scope
from pal.bunshin.swe_verification import infer_repair_target_modules, verification_finding_route_errors
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from pal.bunshin.adapters import SOFTWARE_GIT_ADAPTER
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import AggregateSnapshot, AggregateType, LeaseConflict, StaleFencingToken, SubmissionInvariantError
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.bunshin.graph_executor import FindingClass
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.review_findings import structured_advisories, structured_findings
from pal.bunshin.verification import DefectKind, VerificationService, VerificationStatus, no_progress_detected, verification_correction_count, MAX_VERIFICATION_CORRECTIONS
from pal.bunshin.verification_builder import dominant_verification_defect_kind
from pal.bunshin.semantic_orchestration.role_reports import RoleReports
from pal.bunshin.semantic_orchestration.verifier_tests import VerifierTests


@dataclass
class VerificationSettlement:
    role_reports: RoleReports
    verifier_tests: VerifierTests
    artifacts: ContentAddressedArtifactStore
    repository: BunshinRepository
    dependency_repair_registration: Callable[[AggregateSnapshot, ArtifactRef], Mapping[str, Any]] | None = None

    def prepared_dependency_result(self, node: AggregateSnapshot) -> Mapping[str, Any] | None:
        pending = dict(node.payload.get("pending_verification_ref") or {})
        if not pending.get("sha256"):
            return None
        ref = self.repository.queries.read_dependency_repair_capture_ref(node.aggregate_id, str(pending["sha256"]))
        if not ref.get("sha256"):
            return None
        capture = self.artifacts.read_json(ref)
        if dict(capture.get("source_pending_ref") or {}).get("sha256") != pending["sha256"]:
            raise SubmissionInvariantError("prepared repair capture belongs to another submission")
        return {"provider_request_id": str(capture.get("invocation_id") or ""),
                "result_artifact_ref": dict(capture["report_ref"])}

    def settled_verification_result(self, node: AggregateSnapshot) -> Mapping[str, Any] | None:
        """A committed receipt wins over replay of its immutable snapshot effect."""

        pending_value = dict(node.payload.get("pending_verification_ref") or {})
        if not pending_value.get("sha256"):
            return None
        report_value = self.repository.queries.read_verification_settlement_ref(node.aggregate_id, str(pending_value["sha256"]))
        if not report_value.get("sha256"):
            return None
        report = self.artifacts.read_json(report_value)
        if dict(report.get("source_pending_verification_ref") or {}).get("sha256") != pending_value["sha256"]:
            return None
        return {"provider_request_id": str(report.get("invocation_id") or ""), "result_artifact_ref": report_value}

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
        settled = self.settled_verification_result(node)
        if settled is not None:
            return settled
        current = self.repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
        if current is None or current.version != node.version or current.state != "REVIEW_SNAPSHOTTING":
            raise SubmissionInvariantError("verification snapshot no longer owns the current node version")
        if candidate_digest != str(current.payload.get("candidate_digest") or ""):
            raise SubmissionInvariantError("verification snapshot candidate no longer matches the current node")
        accepted_candidate, accepted_candidate_digest, accepted_candidate_ref, changed_paths, fencing_token, findings, invocation_id, lease_resource, outcome, receipts, receipts_ref, report_ref, scratch_only, status = self.collect_verification_report(
            candidate, candidate_digest, candidate_ref, execution_adapter, node, pending, review_scratch,
            review_workspace, submission,
        )
        routing_errors = verification_finding_route_errors(findings, _verification_repair_scope(self.repository, node))
        defect_kind, fingerprint, module_node_id, repair_node_ids, repair_ref, target_modules = _publish_repair_evidence(
            self.artifacts, self.repository, accepted_candidate, accepted_candidate_digest, candidate_ref, changed_paths, findings, node, outcome,
            receipts, receipts_ref, report_ref, status, submission, routing_errors=routing_errors,
        )

        current = self.repository.snapshots.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            node.aggregate_id,
        )
        if current is None:
            raise SubmissionInvariantError("verification node disappeared before verdict")
        if current.version != node.version:
            raise SubmissionInvariantError("verification node changed before verdict commit")
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
            and (no_progress_detected(failure_history) or (
                bool(routing_errors) and verification_correction_count(current) >= MAX_VERIFICATION_CORRECTIONS
            ))
        )
        if (self.dependency_repair_registration is not None and status == VerificationStatus.FAIL
                and defect_kind == DefectKind.DEPENDENCY and not routing_errors and not blocking_no_progress):
            assert repair_ref is not None
            capture_ref = self.artifacts.put_json({
                "schema_version": "1", "node_name": node_name, "node_run_id": node.aggregate_id,
                "workflow_id": node.workflow_id, "slot": "checker",
                "source_pending_ref": dict(node.payload.get("pending_verification_ref") or {}),
                "source_assignment_id": str(pending.get("role_assignment_id") or ""),
                "source_payload_hash": str(pending.get("role_submission_payload_hash") or ""),
                "source_candidate_ref": candidate_ref.to_dict(), "source_candidate_digest": candidate_digest,
                "candidate_ref": accepted_candidate_ref.to_dict(), "candidate_digest": accepted_candidate_digest,
                "report_ref": report_ref.to_dict(), "repair_packet_ref": repair_ref.to_dict(),
                "status": status.value, "defect_kind": defect_kind.value, "routing_errors": [],
                "target_modules": target_modules, "finding_fingerprint": fingerprint,
                "historical_repair_bill_refs": list(node.payload.get("historical_repair_bill_refs") or []),
                "prior_repair_bill_ref": dict(node.payload.get("repair_bill_ref") or {}),
                "invocation_id": invocation_id, "lease_resource_key": lease_resource, "fencing_token": fencing_token,
            }, artifact_type="DependencyRepairCaptureArtifact", child_refs=(
                (report_ref.sha256, "verification"), (repair_ref.sha256, "repair_packet"),
                (accepted_candidate_ref.sha256, "preserved_candidate"),
            ))
            return self.dependency_repair_registration(node, capture_ref)
        try:
            self.commit_verification_result(
                accepted_candidate, accepted_candidate_digest, accepted_candidate_ref, blocking_no_progress,
                blocking_unknown, candidate_digest, coordinator, current, defect_kind, fingerprint, invocation_id,
                module_node_id, node, node_name, repair_node_ids, repair_ref, report_ref, scratch_only, status,
                target_modules, unknown_policy,
                routing_errors=routing_errors,
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
        *, routing_errors: list[str] | None = None,
    ) -> None:
        with self.repository.transaction() as connection:
            direct_requirement = (node.payload.get("execution_mode") == "direct"
                and status == VerificationStatus.FAIL
                and defect_kind in {DefectKind.CONTRACT, DefectKind.ARCHITECTURE, DefectKind.REQUIREMENTS})
            if blocking_unknown or blocking_no_progress or direct_requirement:
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
                    dependency_nodes=tuple(target_modules) if defect_kind == DefectKind.DEPENDENCY else (),
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
                correction_errors=routing_errors or (),
            )

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
                "source_pending_verification_ref": dict(node.payload.get("pending_verification_ref") or {}),
                "invocation_id": invocation_id,
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

def _publish_repair_evidence(
    artifacts: ContentAddressedArtifactStore, repository: BunshinRepository, accepted_candidate: Any, accepted_candidate_digest: Any, candidate_ref: ArtifactRef, changed_paths: Any,
    findings: Any, node: AggregateSnapshot, outcome: Any, receipts: Any, receipts_ref: Any,
    report_ref: ArtifactRef, status: Any, submission: Mapping[str, Any],
    *, routing_errors: list[str] | None = None,
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
    scope = _verification_repair_scope(repository, node)
    if routing_errors:
        defect_kind = DefectKind.VERIFICATION
        target_modules = []
    elif defect_kind == DefectKind.DEPENDENCY:
        target_modules = sorted(set(infer_repair_target_modules(
            [item for item in findings if item.get("finding_kind") == DefectKind.DEPENDENCY.value],
            scope["repair_path_owners"],
        )) - {scope["module_name"]})
    elif defect_kind in {DefectKind.MODULE, DefectKind.SINK}:
        target_modules = [scope["module_name"]]
    else:
        target_modules = []
    repair_node_ids = [
        _resolve_dependency_node_id(
            repository,
            node,
            dependency_module=module_name,
        )
        for module_name in target_modules
    ]
    module_node_id = ""
    fingerprint = _repair_failure_fingerprint(outcome, findings, receipts)
    repair_ref: ArtifactRef | None = None
    if status == VerificationStatus.FAIL:
        finding_targets: dict[str, list[str]] = {}
        if not routing_errors and defect_kind in {DefectKind.MODULE, DefectKind.SINK, DefectKind.DEPENDENCY}:
            for finding in findings:
                identity = str(finding.get("finding_id") or finding.get("finding_key") or "")
                if finding.get("finding_kind") == DefectKind.DEPENDENCY.value:
                    finding_targets[identity] = sorted(set(infer_repair_target_modules(
                        [finding], scope["repair_path_owners"],
                    )) - {scope["module_name"]})
                else:
                    finding_targets[identity] = [scope["module_name"]]
        pending_ref = dict(node.payload.get("pending_verification_ref") or {})
        repair_ref = artifacts.put_json(
            {
                "schema_version": "1",
                "artifact_kind": "semantic_repair_packet",
                "module_name": str(
                    node.payload.get("module_name") or node.payload.get("unit_id") or ""
                ),
                "route": "verification_correction" if routing_errors else outcome,
                "target_modules": target_modules,
                **({
                    "classification": "invalid_verifier_submission",
                    "routing_errors": routing_errors,
                    "original_outcome": outcome,
                    "original_target_modules": list(submission.get("target_modules") or []),
                    "correction_instruction": (
                        "Reevaluate this preserved submission against the bound repair scope. "
                        "Resolve every original finding explicitly; retain valid current-module defects and "
                        "their regression corpus. Do not report intentional contract stubs as failed provider "
                        "implementations. Do not infer PASS from this Manager correction."
                    ),
                } if routing_errors else {}),
                "findings": findings,
                "finding_targets": finding_targets,
                "source_pending_verification_ref": pending_ref,
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
                *(((str(pending_ref["sha256"]), "original_submission"),) if pending_ref.get("sha256") else ()),
            ),
        )
    return defect_kind, fingerprint, module_node_id, repair_node_ids, repair_ref, target_modules


_SNAPSHOT_DELIVERY_HINT = re.compile(r"Output snapshot: \S+")


def _stable_failure_evidence(item: Any) -> dict[str, Any] | None:
    """Stable execution-result content for one failing command/lsp receipt.

    Oversized reruns deliver output through a fresh random snapshot file whose
    path lands in output_text, and structured shell results carry volatile
    bookkeeping such as session identifiers; neither changes failure meaning.
    Return None for checks that succeeded.
    """
    structured = dict(item.get("structured") or {})

    def stable(value: Any) -> str:
        return _SNAPSHOT_DELIVERY_HINT.sub("Output snapshot: <snapshot>", str(value or ""))

    exit_code = structured.get("exit_code")
    if exit_code is None:
        exit_code = structured.get("returncode")
    if item.get("ok") is True and exit_code in (None, 0):
        return None
    output = stable(item.get("output_text"))
    if not output:
        output = stable(structured.get("stdout")) + "\n" + stable(structured.get("stderr"))
    return {
        "tool_name": item.get("tool_name"),
        "exit_code": exit_code,
        "signal": structured.get("signal") or 0,
        "output": output,
    }


def _repair_failure_fingerprint(outcome: str, findings: Any, receipts: Any) -> str:
    """Compare failure meaning, not fresh finding identities or receipt counts.

    Candidate trees are compared separately by no_progress_detected. Full
    receipts remain in the repair packet; successful checks and test writes
    do not establish progress on an unchanged blocking failure. Snapshot
    delivery paths are normalized so an identical rerun of the same failing
    command keeps one identity across rounds.
    """
    def canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    semantic_findings = sorted({canonical({
        "finding_kind": item.get("finding_kind"),
        "summary": item.get("summary"),
        "locations": sorted({canonical(location) for location in item.get("locations") or []}),
    }) for item in findings})
    failures = set()
    for item in receipts:
        if item.get("kind") not in {"command", "lsp"}:
            continue
        evidence = _stable_failure_evidence(item)
        if evidence is None:
            continue
        failures.add(canonical(evidence))
    return hashlib.sha256(canonical({
        "outcome": outcome, "findings": semantic_findings, "failures": sorted(failures),
    }).encode("utf-8")).hexdigest()
