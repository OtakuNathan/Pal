"""Preserve an invalidated role's evidence without settling its old verdict.

The caller owns process quiescence, the submission cut and the exclusive
workspace lock. This collector never starts a role, dispatches a transition,
claims/releases a lease, or applies a semantic result.
"""
from __future__ import annotations

import hashlib

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from pal.bunshin.adapters import ARTIFACT_BUNDLE_ADAPTER, SOFTWARE_GIT_ADAPTER
from pal.bunshin.artifacts import ArtifactRef
from pal.bunshin.contracts import AggregateSnapshot, AggregateType, SubmissionInvariantError
from pal.bunshin.dependency_repair_protocol import RepairIncarnation
from pal.bunshin.execution_values import workspace_content_fingerprint
from pal.bunshin.review_findings import structured_findings
from pal.bunshin.role_protocol import RoleAssignmentState, stable_hash
from pal.bunshin.role_gateway import role_submission_artifact_type
from pal.bunshin.semantic_orchestration.role_checkpoints import RoleCheckpoints
from pal.bunshin.semantic_orchestration.verification_policy import _verification_repair_scope
from pal.bunshin.semantic_orchestration.verification_settlement import VerificationSettlement, _publish_repair_evidence
from pal.bunshin.semantic_orchestration.verification_workspace import (
    _semantic_path_scope_matches, _verification_corpus_files,
    _verification_scratch_paths, _verification_workspace_changed_paths,
)
from pal.bunshin.semantic_orchestration.workspace_safety import _git_output
from pal.bunshin.swe_verification import semantic_verification_submission_errors, verification_finding_route_errors
from pal.bunshin.verification import DefectKind
from pal.bunshin.verification_readiness import verification_corpus_snapshot


@dataclass
class DependencyRepairCapture:
    verification_settlement: VerificationSettlement
    role_checkpoints: RoleCheckpoints

    @property
    def artifacts(self):
        return self.verification_settlement.artifacts

    @property
    def repository(self):
        return self.verification_settlement.repository

    def capture(
        self, *, node: AggregateSnapshot, incarnation: RepairIncarnation,
        role_assignment_id: str = "",
        pending_ref: Mapping[str, Any] | ArtifactRef | None = None,
    ) -> ArtifactRef:
        """Return durable references for the exact stopped incarnation only."""
        _validate_incarnation(node, incarnation, role_assignment_id)
        assignment_id = role_assignment_id or incarnation.role_assignment_id
        assignment = self._assignment(node, incarnation, assignment_id)
        source_pending = _mapping_ref(pending_ref)
        if pending_ref is None and incarnation.slot == "checker" and node.state in {"REVIEW_QUIESCING", "REVIEW_SNAPSHOTTING"}:
            source_pending = dict(node.payload.get("pending_verification_ref") or {})
        history = self._history(node)
        candidate_value = dict(node.payload.get("candidate_ref") or {})
        candidate_ref = self._durable(candidate_value) if candidate_value else None
        candidate = self.artifacts.read_json(candidate_ref) if candidate_ref else {}
        digest = str(node.payload.get("candidate_digest") or "")
        if candidate_ref and str(candidate.get("candidate_digest") or digest) != digest:
            raise SubmissionInvariantError("capture candidate digest does not match its frozen binding")
        submission_ref, submission = self._submission(assignment)
        base = {
            "schema_version": "1", "node_name": incarnation.node_name,
            "node_run_id": node.aggregate_id, "workflow_id": node.workflow_id,
            "incarnation_key": incarnation.key, "slot": incarnation.slot,
            "source_pending_ref": source_pending, "source_assignment_id": assignment_id,
            "source_payload_hash": str(assignment.get("submission_payload_hash") or ""),
            "source_candidate_ref": candidate_value, "source_candidate_digest": digest,
            "candidate_ref": candidate_value, "candidate_digest": digest,
            "submission_ref": submission_ref, "report_ref": {}, "repair_packet_ref": {},
            "status": "no_submission", "defect_kind": "", "routing_errors": [],
            "target_modules": [], "finding_fingerprint": "", **history,
            "invocation_id": str(assignment.get("session_id") or incarnation.lease_owner),
            "lease_resource_key": incarnation.lease_resource,
            "fencing_token": incarnation.fencing_token,
        }
        if incarnation.slot == "producer":
            base.update(status="producer_preserved", producer_receipt_ref=submission_ref)
            return self._publish(base)
        if candidate_ref is None or not digest:
            # A checker stopped during setup may not yet have a Candidate.
            if assignment or source_pending:
                raise SubmissionInvariantError("verifier capture has no frozen candidate")
            return self._publish(base)
        prompt_ref, workspace = self._prompt(node, incarnation, assignment, candidate_ref, digest)
        # A stopped worker's lease is not read authority for receipt recovery.
        # The artifact above was loaded from its verified assignment receipt.
        workspace["verification_case_revision"] = int(
            dict(submission.get("verification_binding") or {}).get("case_revision") or 0
        )
        review_workspace = Path(str(workspace.get("repo_path") or node.payload.get("workspace_path") or ""))
        review_scratch = Path(str(workspace.get("review_scratch_dir") or review_workspace / ".review-scratch"))
        adapter = str(node.payload.get("execution_adapter") or "")
        if adapter not in {SOFTWARE_GIT_ADAPTER, ARTIFACT_BUNDLE_ADAPTER}:
            raise SubmissionInvariantError("capture has no supported bound execution adapter")
        if not review_workspace.is_dir():
            raise SubmissionInvariantError("capture workspace is unavailable")
        corpus = dict(dict(node.payload.get("path_policy") or {}).get("verification_corpus") or {})
        scratch_only = adapter != SOFTWARE_GIT_ADAPTER
        workspace.update(repo_path=str(review_workspace), review_scratch_dir=str(review_scratch),
                         verification_scratch_only=scratch_only, write_path_scopes=[corpus])
        changed = (_verification_scratch_paths(review_scratch) if scratch_only
                   else _verification_workspace_changed_paths(review_workspace, digest))
        if not scratch_only and any(not _semantic_path_scope_matches(path, corpus) for path in changed):
            raise SubmissionInvariantError("capture workspace contains changes outside the bound verifier corpus")
        base["prompt_ref"] = prompt_ref.to_dict() if prompt_ref else {}
        key = stable_hash({"incarnation": incarnation.to_dict(), "node": dict(node.payload),
                           "assignment": assignment_id, "submission_ref": submission_ref,
                           "pending_ref": source_pending})
        prior = self._prior(key, "DependencyRepairCaptureArtifact")
        if prior is not None:
            self._validate_preserved_workspace(prior, workspace, node)
            return self._durable_tree(prior["capture_ref"])
        if not submission_ref:
            if source_pending:
                raise SubmissionInvariantError("pending verifier capture has no durable role submission")
            if not scratch_only and changed:
                preserved_ref, preserved_digest, _ = self.verification_settlement.verifier_tests.checkpoint_verifier_tests(
                    node=node, review_workspace=review_workspace, candidate_ref=candidate_ref,
                    candidate=candidate, candidate_digest=digest, changed_test_paths=changed,
                )
                base.update(candidate_ref=preserved_ref.to_dict(), candidate_digest=preserved_digest)
            elif scratch_only:
                evidence = self.verification_settlement.role_reports.publish_verification_evidence(
                    review_scratch=review_scratch, candidate_identity=digest,
                )
                base["workspace_evidence_ref"] = evidence.to_dict()
            return self._publish(base, key=key, workspace=workspace)
        if prompt_ref is None:
            raise SubmissionInvariantError("submitted verifier capture has no durable prompt")
        pending = self._pending(node, assignment, source_pending, candidate_ref, digest,
                                submission_ref, submission, review_workspace, review_scratch, adapter, incarnation)
        base["source_pending_ref"] = source_pending
        validation_ref = self._validate_submission(key, node, assignment, submission, workspace, changed, corpus)
        if not source_pending:
            recovered = self.artifacts.put_json(
                pending, artifact_type="PendingSemanticVerificationArtifact",
                provenance={"owner": "manager", "source_role": "verifier", "purpose": "dependency_repair_capture"},
                child_refs=((candidate_ref.sha256, "candidate"), (submission_ref["sha256"], "semantic_submission"),
                            (prompt_ref.sha256, "prompt_pack"), (validation_ref.sha256, "capture_validation")),
            )
            source_pending = recovered.to_dict()
        base.update(pending_ref=source_pending, validation_ref=validation_ref.to_dict())
        bound = replace(node, payload={**node.payload, "pending_verification_ref": source_pending,
                                      "active_worker_id": pending["invocation_id"],
                                      "lease_resource_key": pending["lease_resource_key"], "fencing_token": pending["fencing_token"]})
        values = self.verification_settlement.collect_verification_report(
            candidate, digest, candidate_ref, adapter, bound, pending, review_scratch, review_workspace, submission,
        )
        preserved, preserved_digest, preserved_ref, paths, _, findings, _, _, outcome, receipts, receipts_ref, report_ref, _, status = values
        routing_errors = verification_finding_route_errors(findings, _verification_repair_scope(self.repository, bound))
        defect, fingerprint, _, _, repair_ref, targets = _publish_repair_evidence(
            self.artifacts, self.repository, preserved, preserved_digest, candidate_ref, paths, findings,
            bound, outcome, receipts, receipts_ref, report_ref, status, submission, routing_errors=routing_errors,
        )
        base.update(candidate_ref=preserved_ref.to_dict(), candidate_digest=preserved_digest,
                    report_ref=report_ref.to_dict(), repair_packet_ref=repair_ref.to_dict() if repair_ref else {},
                    status=status.value, defect_kind=defect.value, routing_errors=routing_errors,
                    target_modules=targets if defect == DefectKind.DEPENDENCY and not routing_errors else [],
                    finding_fingerprint=fingerprint)
        return self._publish(base, key=key, workspace=workspace)

    def _durable(self, value: Mapping[str, Any]) -> ArtifactRef:
        record = self.repository.artifacts.read_artifact_record(str(value.get("sha256") or ""))
        if record is None or not record.get("durable") or value.get("durable") is False:
            raise SubmissionInvariantError("capture reference is not durable")
        ref = ArtifactRef.from_mapping(record)
        if any(value.get(key, expected) != expected for key, expected in ref.to_dict().items()):
            raise SubmissionInvariantError("capture reference metadata disagrees with durable artifact")
        self.artifacts.read_bytes(ref)
        return ref

    def _durable_tree(self, value):
        root = self._durable(value)
        pending, seen = [root.sha256], set()
        while pending:
            sha = pending.pop()
            if sha in seen:
                continue
            seen.add(sha)
            self._durable({"sha256": sha})
            with self.repository.database.read_connection() as connection:
                pending.extend(row["child_sha256"] for row in connection.execute(
                    "SELECT child_sha256 FROM bunshin_v2_artifact_refs WHERE parent_sha256 = ?", (sha,),
                ))
        return root

    def _assignment(self, node, incarnation, assignment_id):
        if not assignment_id:
            return {}
        assignment = self.repository.role_assignments.read_role_assignment(assignment_id)
        expected = {"assignment_id": assignment_id, "workflow_id": node.workflow_id,
                    "aggregate_type": AggregateType.DAG_NODE_RUN.value, "aggregate_id": node.aggregate_id,
                    "role": "verifier" if incarnation.slot == "checker" else "coder"}
        if assignment is None or any(assignment.get(key) != value for key, value in expected.items()):
            raise SubmissionInvariantError("capture role assignment does not match the original incarnation")
        spec = dict(assignment.get("execution_spec") or {})
        if spec.get("effect_key") and spec["effect_key"] != incarnation.effect_key:
            raise SubmissionInvariantError("capture role assignment belongs to another effect")
        actual = (self.repository.role_attempts.read_role_attempt_business_lease(incarnation.attempt_id)
                  if incarnation.attempt_id else None)
        business = dict((actual or {}).get("business_lease") or {})
        if not business:
            pending_value = dict(node.payload.get("pending_verification_ref") or {})
            pending = (self.artifacts.read_json(pending_value) if pending_value
                       and node.state in {"REVIEW_QUIESCING", "REVIEW_SNAPSHOTTING"} else {})
            business = {"resource_key": pending.get("lease_resource_key") or node.payload.get("lease_resource_key"),
                        "owner_id": pending.get("invocation_id") or node.payload.get("active_worker_id"),
                        "fencing_token": pending.get("fencing_token") or node.payload.get("fencing_token")}
            if not any(business.values()):
                business = dict(spec.get("business_lease") or {})
        if business and any(business.get(key) != value for key, value in {
            "resource_key": incarnation.lease_resource, "owner_id": incarnation.lease_owner,
            "fencing_token": incarnation.fencing_token,
        }.items()):
            raise SubmissionInvariantError("capture assignment has the wrong original business lease")
        session_id = str(assignment.get("session_id") or "")
        expected_session = incarnation.lease_owner or str(node.payload.get("active_worker_id") or "")
        session = self.repository.role_sessions.read_role_session(session_id)
        if (not session_id or (expected_session and session_id != expected_session) or session is None
                or any(session.get(key) != expected[key] for key in ("workflow_id", "aggregate_type", "aggregate_id", "role"))):
            raise SubmissionInvariantError("capture assignment has an invalid original role session")
        if incarnation.attempt_id and assignment.get("active_attempt_id") != incarnation.attempt_id:
            raise SubmissionInvariantError("capture assignment belongs to another attempt")
        if incarnation.slot == "checker" and (assignment.get("mode") != "module" or int(
            dict(assignment.get("execution_spec") or {}).get("evaluation_generation") or 0
        ) != int(node.payload.get("verifier_evaluation_generation") or 0)):
            raise SubmissionInvariantError("capture belongs to another verifier evaluation")
        return assignment

    def _submission(self, assignment):
        ref = dict(assignment.get("submission_artifact_ref") or {})
        recorded = assignment.get("state") in {RoleAssignmentState.RESULT_RECORDED.value, RoleAssignmentState.SETTLED.value}
        if not ref and not recorded and not assignment.get("submission_payload_hash"):
            return {}, {}
        if not recorded or not ref:
            raise SubmissionInvariantError("capture has an inconsistent role submission receipt")
        ref = self._durable(ref).to_dict()
        if ref["artifact_type"] != role_submission_artifact_type(str(assignment.get("submission_kind") or "")):
            raise SubmissionInvariantError("capture role submission artifact has the wrong type")
        self.role_checkpoints.terminal_from_assignment_receipt(
            assignment, primary_artifact_name="verification_submission.json", summary="Preserved invalidated role submission",
        )
        return ref, dict(self.artifacts.read_json(ref))

    def _prompt(self, node, incarnation, assignment, candidate_ref, digest):
        if not assignment:
            return None, {}
        prompt_ref = self.role_checkpoints.durable_assignment_prompt_ref(assignment)
        if prompt_ref is None:
            if assignment.get("submission_artifact_ref"):
                raise SubmissionInvariantError("capture original prompt is unavailable")
            return None, {}
        prompt_ref = self._durable(prompt_ref.to_dict())
        prompt = dict(self.artifacts.read_json(prompt_ref))
        workspace = dict(prompt.get("workspace") or {})
        metadata = dict(prompt.get("metadata") or {})
        binding = {**dict(metadata.get("bunshin_v2") or {}), **dict(workspace.get("bunshin_v2") or {})}
        expected = {"workflow_id": node.workflow_id, "aggregate_type": node.aggregate_type.value,
                    "aggregate_id": node.aggregate_id, "role": "verifier", "mode": "module",
                    "invocation_id": assignment.get("active_attempt_id"),
                    "authoring_input_fingerprint": assignment.get("input_fingerprint")}
        if any(binding.get(key) != value for key, value in expected.items()) or dict(
            metadata.get("agent_session") or {}
        ).get("session_id") != assignment.get("session_id"):
            raise SubmissionInvariantError("capture durable prompt has the wrong assignment binding")
        attempt = self.repository.role_attempts.read_role_attempt(str(assignment.get("active_attempt_id") or ""))
        if (attempt is None or attempt.get("assignment_id") != assignment.get("assignment_id")
                or dict(attempt.get("prompt_pack_ref") or {}).get("sha256") != prompt_ref.sha256
                or binding.get("lease_resource_key") != attempt.get("lease_resource_key")
                or int(binding.get("fencing_token") or 0) != int(attempt.get("fencing_token") or 0)):
            raise SubmissionInvariantError("capture durable prompt has the wrong attempt binding")
        inputs = dict(assignment.get("input_refs") or {})
        diff_ref = self._durable(dict(inputs.get("candidate_diff") or {}))
        diff = self.artifacts.read_json(diff_ref)
        if diff.get("target_sha") or diff_ref.artifact_type == "CandidateSemanticViewArtifact":
            with self.repository.database.read_connection() as connection:
                linked = connection.execute(
                    "SELECT 1 FROM bunshin_v2_artifact_refs WHERE parent_sha256 = ? AND child_sha256 = ? AND relation = ?",
                    (diff_ref.sha256, candidate_ref.sha256, "candidate" if diff_ref.artifact_type == "CandidateSemanticViewArtifact" else "checkpoint"),
                ).fetchone()
            if linked is None:
                raise SubmissionInvariantError("capture review range lacks its original candidate reference")
        elif diff_ref.sha256 != candidate_ref.sha256:
            raise SubmissionInvariantError("capture candidate input does not identify the original candidate")
        if diff_ref.artifact_type != "CandidateSemanticViewArtifact" and str(diff.get("target_sha") or diff.get("candidate_digest") or "") != digest:
            raise SubmissionInvariantError("capture assignment candidate binding is stale")
        view_ref = self._durable(dict(inputs.get("module_work_view") or {}))
        original_view = dict(node.payload.get("unit_work_view_ref") or {})
        if original_view.get("sha256") != view_ref.sha256:
            raise SubmissionInvariantError("capture assignment work view is stale")
        if str(node.payload.get("execution_adapter") or "") == SOFTWARE_GIT_ADAPTER and (
            workspace.get("workspace_binding") != "canonical" or
            Path(str(workspace.get("repo_path") or "")).resolve() != Path(str(node.payload.get("workspace_path") or "")).resolve()
        ):
            raise SubmissionInvariantError("capture prompt is not bound to the original canonical workspace")
        workspace["bunshin_v2"] = binding
        return prompt_ref, workspace

    def _pending(self, node, assignment, source, candidate_ref, digest, submission_ref, submission, workspace, scratch, adapter, incarnation):
        pending = dict(self.artifacts.read_json(self._durable(source))) if source else {
            "schema_version": "1", "submission": submission,
            "candidate_ref": candidate_ref.to_dict(), "implementation_candidate_ref": candidate_ref.to_dict(),
            "candidate_digest": digest, "candidate_git_base": digest if adapter == SOFTWARE_GIT_ADAPTER else "",
            "review_workspace": str(workspace), "review_scratch": str(scratch), "execution_adapter": adapter,
            "invocation_id": assignment["session_id"], "lease_resource_key": incarnation.lease_resource,
            "fencing_token": incarnation.fencing_token,
            "role_assignment_id": assignment["assignment_id"], "role_submission_payload_hash": assignment["submission_payload_hash"],
            "submission_ref": submission_ref,
        }
        expected = {"candidate_digest": digest, "execution_adapter": adapter, "invocation_id": assignment["session_id"],
                    "role_assignment_id": assignment["assignment_id"], "role_submission_payload_hash": assignment["submission_payload_hash"]}
        if incarnation.lease_resource:
            expected.update(lease_resource_key=incarnation.lease_resource, fencing_token=incarnation.fencing_token)
        if (any(pending.get(key) != value for key, value in expected.items())
                or dict(pending.get("candidate_ref") or {}).get("sha256") != candidate_ref.sha256
                or dict(pending.get("implementation_candidate_ref") or candidate_ref.to_dict()).get("sha256") != candidate_ref.sha256
                or (adapter == SOFTWARE_GIT_ADAPTER and pending.get("candidate_git_base", digest) != digest)
                or dict(pending.get("submission_ref") or {}).get("sha256") != submission_ref["sha256"]
                or pending.get("submission") != submission
                or Path(str(pending.get("review_workspace") or "")).resolve() != workspace.resolve()
                or Path(str(pending.get("review_scratch") or "")).resolve() != scratch.resolve()):
            raise SubmissionInvariantError("capture pending verification does not match its immutable receipt")
        for name in ("candidate_ref", "implementation_candidate_ref", "submission_ref"):
            if pending.get(name):
                self._durable(pending[name])
        fingerprint = workspace_content_fingerprint(workspace) if adapter == SOFTWARE_GIT_ADAPTER else ""
        if source and pending.get("submitted_workspace_fingerprint") and fingerprint != pending["submitted_workspace_fingerprint"]:
            raise SubmissionInvariantError("capture verifier workspace changed after submission")
        if not source:
            pending["submitted_workspace_fingerprint"] = fingerprint
        return pending

    def _validate_submission(self, key, node, assignment, submission, workspace, changed, corpus):
        previous = self._prior(key, "DependencyRepairValidationArtifact")
        if previous is not None:
            self._validate_preserved_workspace(previous, workspace, node)
            return self._durable(previous["capture_ref"])
        scope = _verification_repair_scope(self.repository, node)
        workspace["bunshin_v2"]["swe_verification_tool_contract"] = {
            **dict(workspace["bunshin_v2"].get("swe_verification_tool_contract") or {}), **scope,
        }
        view = self.artifacts.read_json(self._durable(dict(dict(assignment.get("input_refs") or {}).get("module_work_view") or {})))
        receipt = self._durable(dict(assignment.get("submission_artifact_ref") or {}))
        if (assignment.get("state") not in {RoleAssignmentState.RESULT_RECORDED.value, RoleAssignmentState.SETTLED.value}
                or stable_hash(submission) != assignment.get("submission_payload_hash")
                or self.artifacts.read_json(receipt) != dict(submission)):
            raise SubmissionInvariantError("capture submission differs from its authoritative assignment receipt")
        errors = semantic_verification_submission_errors(
            submission, work_view=view, changed_paths=changed,
            current_case_paths=(_verification_scratch_paths(Path(workspace["review_scratch_dir"])) if workspace["verification_scratch_only"]
                                else _verification_corpus_files(Path(workspace["repo_path"]), corpus)),
            corpus_scope=corpus, scratch_only=workspace["verification_scratch_only"], workspace=workspace,
            accepted_legacy_receipt="verification_binding" not in submission,
        )
        routes = set(verification_finding_route_errors(structured_findings(submission), scope))
        errors = tuple(error for error in errors if error not in routes)
        if errors:
            raise SubmissionInvariantError("capture verifier submission failed manager validation:\n- " + "\n- ".join(errors))
        return self._publish({"schema_version": "1", "submission_ref": dict(assignment["submission_artifact_ref"])},
                             key=key, workspace=workspace, artifact_type="DependencyRepairValidationArtifact")

    def _history(self, node):
        refs = [dict(ref) for ref in node.payload.get("historical_repair_bill_refs") or []]
        current = dict(node.payload.get("repair_bill_ref") or {})
        if current and current not in refs:
            refs.append(current)
        for ref in refs:
            self._durable(ref)
        producer = dict(node.payload.get("producer_report_ref") or {})
        if producer:
            self._durable(producer)
        return {"historical_repair_bill_refs": refs, "repair_bill_ref": current, "producer_report_ref": producer}

    def _prior(self, key, artifact_type):
        with self.repository.database.read_connection() as connection:
            rows = connection.execute(
                "SELECT sha256 FROM bunshin_v2_artifacts WHERE artifact_type = ? AND json_extract(metadata_json, '$.capture_key') = ?",
                (artifact_type, key),
            ).fetchall()
        if len(rows) > 1:
            raise SubmissionInvariantError("capture has conflicting durable artifacts")
        if not rows:
            return None
        ref = self._durable({"sha256": rows[0]["sha256"]})
        return {**dict(self.artifacts.read_json(ref)), "capture_ref": ref.to_dict()}

    def _validate_preserved_workspace(self, previous, workspace, node):
        if not workspace["verification_scratch_only"]:
            _validate_checkpoint_head(node, Path(workspace["repo_path"]), previous)
        current = verification_corpus_snapshot(workspace)
        expected = dict(previous["corpus_snapshot"])
        # A Manager verifier checkpoint changes HEAD, never the authored corpus.
        current.pop("candidate_digest", None)
        expected.pop("candidate_digest", None)
        if "case_revision" not in expected:
            # Older Manager capture artifacts predate the draft-revision field.
            current.pop("case_revision", None)
        if current != expected or (previous.get("workspace_fingerprint") and previous["workspace_fingerprint"] != workspace_content_fingerprint(Path(workspace["repo_path"]))):
            raise SubmissionInvariantError("capture workspace changed after evidence preservation")

    def _publish(self, payload, *, key="", workspace=None, artifact_type="DependencyRepairCaptureArtifact"):
        value = dict(payload)
        if workspace is not None:
            value["corpus_snapshot"] = verification_corpus_snapshot(workspace)
            value["workspace_fingerprint"] = (workspace_content_fingerprint(Path(workspace["repo_path"]))
                                              if not workspace["verification_scratch_only"] else "")
        refs = [(str(ref["sha256"]), name) for name, raw in value.items()
                for ref in (raw if isinstance(raw, list) else [raw])
                if isinstance(ref, Mapping) and ref.get("sha256")]
        return self.artifacts.put_json(value, artifact_type=artifact_type,
                                       provenance={"owner": "manager", "purpose": "dependency_repair_capture"},
                                       metadata={"capture_key": key} if key else {}, child_refs=tuple(refs))


def _mapping_ref(ref):
    return ref.to_dict() if isinstance(ref, ArtifactRef) else dict(ref or {})


def _validate_incarnation(node, incarnation, assignment_id):
    if (node.aggregate_type != AggregateType.DAG_NODE_RUN or incarnation.aggregate_id != node.aggregate_id
            or incarnation.node_name != str(node.payload.get("module_name") or node.payload.get("unit_id") or "")
            or (node.payload.get("graph_generation") and int(node.payload["graph_generation"]) != incarnation.generation)
            or (assignment_id and incarnation.role_assignment_id and assignment_id != incarnation.role_assignment_id)):
        raise SubmissionInvariantError("capture node or assignment differs from the original incarnation")


def _validate_checkpoint_head(node, workspace, previous):
    source = str(node.payload.get("candidate_digest") or "")
    head = _git_output(workspace, "rev-parse", "HEAD")
    if head == source:
        return
    preserved = str(previous.get("candidate_digest") or "")
    if preserved and head != preserved:
        raise SubmissionInvariantError("capture workspace HEAD differs from the preserved candidate")
    tree = _git_output(workspace, "rev-parse", "HEAD^{tree}")
    checkpoint_key = hashlib.sha256(f"verifier-checkpoint-v1:{node.aggregate_id}:{source}:{tree}".encode()).hexdigest()
    if (_git_output(workspace, "rev-parse", "HEAD^") != source
            or f"Pal-Assignment-Key: {checkpoint_key}" not in _git_output(workspace, "show", "-s", "--format=%B", "HEAD").splitlines()):
        raise SubmissionInvariantError("capture workspace moved away from its original verifier checkpoint")
