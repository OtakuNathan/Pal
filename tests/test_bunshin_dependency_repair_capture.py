"""Offline evidence capture across dependency-repair invalidation boundaries."""
from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from tests import test_bunshin_v2_verifier_scope_recovery as scope_fixture
from pal.bunshin.v2.catalog import BunshinV2Catalog
from pal.bunshin.v2.contracts import AggregateType, SubmissionInvariantError
from pal.bunshin.v2.dependency_repair_protocol import RepairIncarnation
from pal.bunshin.v2.execution_values import workspace_content_fingerprint
from pal.bunshin.v2.role_protocol import RoleAssignmentRequest, stable_hash
from pal.bunshin.v2.semantic_orchestration.dependency_repair_capture import DependencyRepairCapture
from pal.bunshin.v2.verification_readiness import verification_corpus_snapshot


@pytest.fixture
def case():
    fx = scope_fixture.VerifierScopeRecoveryTests()
    fx.setUp()
    try:
        yield CaptureCase(fx)
    finally:
        fx.doCleanups()


class CaptureCase:
    def __init__(self, fx):
        self.fx = fx
        self.repository, self.artifacts = fx.repository, fx.artifacts
        self.collector = DependencyRepairCapture(fx.worker.components.verification_settlement,
                                                fx.worker.components.role_checkpoints)
        self.node = fx._node()
        self.path = fx.repo / "tests/archive_verify/verifier/test_contract.py"
        self.path.write_text("def test_fifo_rejected():\n    assert True\n")
        self.scratch = fx.root / "scratch"
        self.scratch.mkdir()
        self.view_ref = self.artifacts.put_json({"module_name": "archive_verify"}, artifact_type="ModuleWorkViewArtifact")
        self.diff_ref = self.artifacts.put_json({"target_sha": fx.digest}, artifact_type="GitReviewRangeArtifact",
                                               child_refs=((fx.candidate_ref.sha256, "checkpoint"),))
        self.binding = BunshinV2Catalog(fx.root, self.artifacts).publish_family_binding("software_engineering.v2_coder")
        self.session = "capture-verifier-session"
        self.repository.role_sessions.ensure_role_session(
            session_id=self.session, workflow_id=fx.workflow_id, aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id=self.node.aggregate_id, role="verifier", mode="module", role_profile_id="software_engineering.v2_verifier",
            family_binding_sha=self.binding.sha256, scope_kind="module", subject_key="archive_verify",
        )
        self.assignment = self.repository.role_assignments.create_role_assignment(RoleAssignmentRequest(
            assignment_key="capture-original", session_id=self.session, workflow_id=fx.workflow_id,
            aggregate_type=AggregateType.DAG_NODE_RUN.value, aggregate_id=self.node.aggregate_id,
            role="verifier", mode="module", role_profile_id="software_engineering.v2_verifier",
            family_binding_sha=self.binding.sha256, input_fingerprint="original-verifier-input",
            required_inputs=(), input_refs={"candidate_diff": self.diff_ref.to_dict(), "module_work_view": self.view_ref.to_dict()},
            execution_spec={"effect_type": "run_verifier_role", "effect_key": "original-effect", "evaluation_generation": 0},
            submission_kind="verification",
        ))
        self.attempt = self.repository.role_assignments.claim_role_assignment(self.assignment["assignment_id"])
        self.attempt_id = self.attempt["attempt_id"]
        self.attempt_lease = self.repository.leases.claim_lease("capture-attempt", self.attempt_id, ttl_seconds=120)
        self.lease = self.repository.leases.claim_lease("capture-node", self.session, ttl_seconds=120)
        self.workspace = {
            "repo_path": str(fx.repo), "review_scratch_dir": str(self.scratch), "workspace_binding": "canonical",
            "write_path_scopes": [self.node.payload["path_policy"]["verification_corpus"]],
            "bunshin_v2": {"workflow_id": fx.workflow_id, "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                           "aggregate_id": self.node.aggregate_id, "role": "verifier", "mode": "module",
                           "authoring_input_fingerprint": "original-verifier-input", "invocation_id": self.attempt_id,
                           "lease_resource_key": self.attempt_lease.resource_key, "fencing_token": self.attempt_lease.fencing_token},
        }
        self.prompt_ref = self.artifacts.put_json(
            {"workspace": self.workspace, "metadata": {"agent_session": {"session_id": self.session}}},
            artifact_type="RolePromptPackArtifact",
        )
        self.repository.role_attempts.start_role_attempt(
            assignment_id=self.assignment["assignment_id"], attempt_id_value=self.attempt_id,
            lease_resource_key=self.attempt_lease.resource_key, fencing_token=self.attempt_lease.fencing_token,
            prompt_pack_ref=self.prompt_ref.to_dict(),
        )
        self.node = replace(self.node, payload={**self.node.payload, "candidate_ref": fx.candidate_ref.to_dict(),
            "candidate_digest": fx.digest, "unit_work_view_ref": self.view_ref.to_dict(), "active_worker_id": self.session,
            "lease_resource_key": self.lease.resource_key, "fencing_token": self.lease.fencing_token})
        self.incarnation = RepairIncarnation(
            node_name="archive_verify", cycle_id="original-cycle", generation=1, input_fingerprint="graph-checker-input",
            slot="checker", aggregate_id=self.node.aggregate_id, effect_id="original-effect-id", effect_key="original-effect",
            role_assignment_id=self.assignment["assignment_id"], attempt_id=self.attempt_id,
            lease_resource=self.lease.resource_key, lease_owner=self.session, fencing_token=self.lease.fencing_token,
        )
        self.submission_ref = None

    def submit(self, *, findings=None, outcome="module_repair", stale=False):
        submission = self.fx._submission(findings)
        submission["outcome"] = outcome
        submission["recorded_results"][0]["status"] = "PASS" if outcome == "pass" else "FAIL"
        submission["recorded_results"][0]["input_fingerprint"] = "original-verifier-input"
        submission["tool_receipts"][0].update(ok=outcome == "pass", verification_binding=verification_corpus_snapshot(self.workspace))
        if stale:
            submission["tool_receipts"][0]["verification_binding"]["input_fingerprint"] = "stale-input"
        self.submission = submission
        self.submission_ref = self.artifacts.put_json(submission, artifact_type="VerifierRoleSubmissionArtifact")
        self.repository.role_submissions.record_role_submission(
            assignment_id=self.assignment["assignment_id"], attempt_id_value=self.attempt_id,
            fencing_token=self.attempt_lease.fencing_token, artifact_ref=self.submission_ref.to_dict(),
            payload_hash=stable_hash(submission), settlement_action={"action_type": "SUBMIT_SEMANTIC_VERIFICATION"},
        )

    def pending(self, **changes):
        value = {
            "schema_version": "1", "submission": self.submission, "candidate_ref": self.fx.candidate_ref.to_dict(),
            "candidate_digest": self.fx.digest, "candidate_git_base": self.fx.digest,
            "implementation_candidate_ref": self.fx.candidate_ref.to_dict(), "review_workspace": str(self.fx.repo),
            "review_scratch": str(self.scratch), "execution_adapter": "software_git.v2",
            "submitted_workspace_fingerprint": workspace_content_fingerprint(self.fx.repo),
            "invocation_id": self.session, "lease_resource_key": self.lease.resource_key,
            "fencing_token": self.lease.fencing_token, "role_assignment_id": self.assignment["assignment_id"],
            "role_submission_payload_hash": stable_hash(self.submission), "submission_ref": self.submission_ref.to_dict(),
            **changes,
        }
        return self.artifacts.put_json(value, artifact_type="PendingSemanticVerificationArtifact")

    def capture(self, **kwargs):
        return self.collector.capture(node=self.node, incarnation=self.incarnation, **kwargs)

    def rows(self):
        with self.repository.database.read_connection() as connection:
            return {name: [dict(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY 1")]
                    for name in ("bunshin_v2_role_assignments", "bunshin_v2_role_attempts", "bunshin_v2_role_sessions",
                                 "bunshin_v2_domain_events", "bunshin_v2_outbox", "bunshin_v2_leases")}


def test_raw_receipt_preserves_all_findings_without_settling_or_rerunning(case):
    case.submit()
    original_rows = case.rows()
    ref = case.capture()
    capture = case.artifacts.read_json(ref)
    packet = case.artifacts.read_json(capture["repair_packet_ref"])
    assert capture["status"] == "FAIL"
    assert capture["routing_errors"]
    assert capture["target_modules"] == []
    assert capture["source_pending_ref"] == {}
    assert packet["route"] == "verification_correction"
    assert packet["findings"] == case.submission["findings"]
    assert packet["source_pending_verification_ref"] == capture["pending_ref"]
    assert capture["source_candidate_digest"] == case.fx.digest
    assert capture["candidate_digest"] != case.fx.digest
    assert case.artifacts.read_json(capture["candidate_ref"])["verifier_test_paths"] == [str(case.path.relative_to(case.fx.repo))]
    assert "findings" not in capture and "receipts" not in capture
    assert case.rows() == original_rows
    assert case.artifacts.read_json(case.submission_ref) == case.submission
    assert case.capture() == ref


def test_pending_receipt_preserves_original_reference(case):
    case.submit(findings=[case.fx.fifo])
    pending = case.pending()
    captured = case.artifacts.read_json(case.capture(pending_ref=pending))
    assert captured["source_pending_ref"] == pending.to_dict()
    assert captured["pending_ref"] == pending.to_dict()
    assert captured["target_modules"] == []
    assert captured["routing_errors"] == []
    assert captured["defect_kind"] == "module_defect"


def test_pass_is_captured_and_never_applied(case):
    case.submit(findings=[], outcome="pass")
    rows = case.rows()
    captured = case.artifacts.read_json(case.capture())
    assert captured["status"] == "PASS"
    assert captured["repair_packet_ref"] == {}
    assert captured["target_modules"] == []
    assert case.rows() == rows


def test_no_submission_preserves_corpus_without_inventing_report(case):
    rows = case.rows()
    ref = case.capture()
    captured = case.artifacts.read_json(ref)
    assert captured["status"] == "no_submission"
    assert captured["report_ref"] == captured["repair_packet_ref"] == {}
    assert captured["candidate_digest"] != case.fx.digest
    assert case.rows() == rows
    assert case.capture() == ref


@pytest.mark.parametrize("change", ["candidate", "submission", "assignment", "workspace"])
def test_pending_binding_corruption_fails_closed(case, change):
    case.submit()
    changes = {"candidate": {"candidate_digest": "wrong"}, "submission": {"submission": {"outcome": "pass"}},
               "assignment": {"role_assignment_id": "wrong"}, "workspace": {"review_workspace": str(case.fx.root)}}
    pending = case.pending(**changes[change])
    with pytest.raises(SubmissionInvariantError, match="immutable receipt"):
        case.capture(pending_ref=pending)
    assert case.fx._git("rev-parse", "HEAD").strip() == case.fx.digest


@pytest.mark.parametrize("submitted", [True, False])
def test_out_of_corpus_changes_are_not_checkpointed(case, submitted):
    if submitted:
        case.submit()
    product = case.fx.repo / "archive_verify.py"
    product.write_text("unauthorized edit\n")
    with pytest.raises(SubmissionInvariantError, match="outside the bound verifier corpus"):
        case.capture()
    assert product.read_text() == "unauthorized edit\n"
    assert case.fx._git("rev-parse", "HEAD").strip() == case.fx.digest


def test_stale_execution_receipts_are_not_treated_as_routing_corrections(case):
    case.submit(stale=True)
    with pytest.raises(SubmissionInvariantError, match="fresh validation required"):
        case.capture()
    assert case.fx._git("rev-parse", "HEAD").strip() == case.fx.digest


def test_wrong_original_assignment_is_rejected(case):
    case.submit()
    with pytest.raises(SubmissionInvariantError, match="differs from the original incarnation"):
        case.capture(role_assignment_id="another-assignment")


def test_wrong_prompt_session_fails_closed(case):
    case.submit()
    wrong = case.artifacts.put_json({"workspace": case.workspace, "metadata": {"agent_session": {"session_id": "wrong"}}}, artifact_type="RolePromptPackArtifact")
    with patch.object(case.collector.role_checkpoints, "durable_assignment_prompt_ref", return_value=wrong):
        with pytest.raises(SubmissionInvariantError, match="wrong assignment binding"):
            case.capture()


def test_receipt_payload_hash_is_checked(case):
    case.submit()
    original = case.repository.role_assignments.read_role_assignment(case.assignment["assignment_id"])
    with patch.object(case.repository.role_assignments, "read_role_assignment", return_value={**original, "submission_payload_hash": "wrong"}):
        with pytest.raises(SubmissionInvariantError, match="payload hash"):
            case.capture()


def test_undurable_receipt_cannot_be_captured(case):
    case.submit()
    original = case.repository.role_assignments.read_role_assignment(case.assignment["assignment_id"])
    with patch.object(case.repository.role_assignments, "read_role_assignment", return_value={**original, "submission_artifact_ref": {**case.submission_ref.to_dict(), "durable": False}}):
        with pytest.raises(SubmissionInvariantError, match="not durable"):
            case.capture()


def test_replay_rejects_corpus_changed_after_capture(case):
    case.submit()
    case.capture()
    case.path.write_text("def test_different():\n    assert False\n")
    with pytest.raises(SubmissionInvariantError, match="changed after evidence preservation"):
        case.capture()


def test_recovery_after_checkpoint_before_capture_publication(case):
    case.submit()
    real = case.collector.verification_settlement.collect_verification_report
    def interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("injected crash after checkpoint")
    with patch.object(case.collector.verification_settlement, "collect_verification_report", side_effect=interrupted):
        with pytest.raises(RuntimeError, match="injected crash"):
            case.capture()
    captured = case.artifacts.read_json(case.capture())
    assert captured["status"] == "FAIL"
    assert captured["candidate_digest"] != case.fx.digest
    assert case.artifacts.read_json(captured["repair_packet_ref"])["findings"] == case.submission["findings"]


def test_producer_capture_keeps_code_and_old_repair_history(case):
    packet = case.artifacts.put_json({"findings": [case.fx.fifo]}, artifact_type="RepairPacketArtifact")
    report = case.artifacts.put_json({"status": "candidate_ready"}, artifact_type="ProducerReportArtifact")
    product = case.fx.repo / "archive_verify.py"
    product.write_text("unfinished producer code\n")
    node = replace(case.node, payload={**case.node.payload, "repair_bill_ref": packet.to_dict(),
                                      "historical_repair_bill_refs": [packet.to_dict()], "producer_report_ref": report.to_dict()})
    incarnation = replace(case.incarnation, slot="producer", role_assignment_id="", attempt_id="")
    captured = case.artifacts.read_json(case.collector.capture(node=node, incarnation=incarnation))
    assert captured["status"] == "producer_preserved"
    assert captured["repair_bill_ref"] == packet.to_dict()
    assert captured["historical_repair_bill_refs"] == [packet.to_dict()]
    assert captured["producer_report_ref"] == report.to_dict()
    assert captured["report_ref"] == captured["repair_packet_ref"] == {}
    assert product.read_text() == "unfinished producer code\n"
    assert case.fx._git("rev-parse", "HEAD").strip() == case.fx.digest


def test_result_recorded_and_settled_raw_receipts_are_both_recoverable(case):
    case.submit()
    case.repository.role_submissions.settle_role_assignment(
        assignment_id=case.assignment["assignment_id"], submission_payload_hash=stable_hash(case.submission),
    )
    rows = case.rows()
    captured = case.artifacts.read_json(case.capture())
    assert captured["source_assignment_id"] == case.assignment["assignment_id"]
    assert captured["source_payload_hash"] == stable_hash(case.submission)
    assert captured["report_ref"]
    assert case.rows() == rows


def test_dependency_targets_come_only_from_dependency_findings(case):
    case.submit(findings=[case.fx.fifo, case.fx.stub])
    case.node = replace(case.node, payload={**case.node.payload,
        "dependency_node_ids": ["node-manifest_model"],
        "dependency_outputs": {"node-manifest_model": {"candidate_ref": case.fx.candidate_ref.to_dict(), "candidate_digest": case.fx.digest}},
    })
    # Legacy installations without GraphExecution still use the frozen bound
    # products and real module ownership scope, never authored target_modules.
    with patch.object(case.repository.cycles, "read_graph_execution", return_value=None):
        captured = case.artifacts.read_json(case.capture())
    assert captured["defect_kind"] == "dependency_defect"
    assert captured["target_modules"] == ["manifest_model"]
    assert captured["routing_errors"] == []
    packet = case.artifacts.read_json(captured["repair_packet_ref"])
    assert packet["finding_targets"] == {"finding_fifo": ["archive_verify"], "finding_stub": ["manifest_model"]}


def test_undurable_prompt_fails_closed(case):
    case.submit()
    with patch.object(case.collector.role_checkpoints, "durable_assignment_prompt_ref", return_value=None):
        with pytest.raises(SubmissionInvariantError, match="original prompt is unavailable"):
            case.capture()


def test_stale_review_range_fails_closed(case):
    case.submit()
    stale = case.artifacts.put_json({"target_sha": "another-candidate"}, artifact_type="GitReviewRangeArtifact",
                                    child_refs=((case.fx.candidate_ref.sha256, "checkpoint"),))
    original = case.repository.role_assignments.read_role_assignment(case.assignment["assignment_id"])
    with patch.object(case.repository.role_assignments, "read_role_assignment", return_value={
        **original, "input_refs": {**original["input_refs"], "candidate_diff": stale.to_dict()},
    }):
        with pytest.raises(SubmissionInvariantError, match="candidate binding is stale"):
            case.capture()


def test_replay_rejects_unrelated_head_even_with_identical_files(case):
    case.submit()
    case.capture()
    case.fx._git("commit", "--allow-empty", "-qm", "unrelated HEAD move")
    with pytest.raises(SubmissionInvariantError, match="HEAD differs"):
        case.capture()


def test_validation_recovery_rejects_unrelated_commit_with_same_tree(case):
    case.submit()
    with patch.object(case.collector.verification_settlement, "collect_verification_report", side_effect=RuntimeError("before checkpoint")):
        with pytest.raises(RuntimeError, match="before checkpoint"):
            case.capture()
    case.fx._git("add", "-A")
    case.fx._git("commit", "-qm", "unrelated authored commit")
    with pytest.raises(SubmissionInvariantError, match="original verifier checkpoint"):
        case.capture()


def test_pending_original_business_lease_must_match(case):
    case.submit()
    pending = case.pending(fencing_token=case.lease.fencing_token + 1)
    with pytest.raises(SubmissionInvariantError, match="immutable receipt"):
        case.capture(pending_ref=pending)


def test_capture_provenance_links_every_preserved_reference(case):
    case.submit()
    pending = case.pending()
    ref = case.capture(pending_ref=pending)
    capture = case.artifacts.read_json(ref)
    with case.repository.database.read_connection() as connection:
        links = {(row["child_sha256"], row["relation"]) for row in connection.execute(
            "SELECT child_sha256, relation FROM bunshin_v2_artifact_refs WHERE parent_sha256 = ?", (ref.sha256,),
        )}
    for name in ("source_pending_ref", "source_candidate_ref", "candidate_ref", "report_ref", "repair_packet_ref", "submission_ref", "prompt_ref", "validation_ref"):
        assert (capture[name]["sha256"], name) in links


def test_explicit_absent_pending_never_adopts_old_node_pending(case):
    old = case.artifacts.put_json({"submission": "old verifier"}, artifact_type="PendingSemanticVerificationArtifact")
    case.node = replace(case.node, state="REVIEWING", payload={**case.node.payload, "pending_verification_ref": old.to_dict()})
    case.submit()
    captured = case.artifacts.read_json(case.capture(pending_ref={}))
    assert captured["source_pending_ref"] == {}
    assert captured["pending_ref"] != old.to_dict()


def test_capture_rejects_same_role_assignment_from_another_effect(case):
    case.submit()
    original = case.repository.role_assignments.read_role_assignment(case.assignment["assignment_id"])
    with patch.object(case.repository.role_assignments, "read_role_assignment", return_value={
        **original, "execution_spec": {**original["execution_spec"], "effect_key": "another-effect"},
    }):
        with pytest.raises(SubmissionInvariantError, match="another effect"):
            case.capture()


def test_replay_revalidates_preserved_report_content(case):
    case.submit()
    ref = case.capture()
    captured = case.artifacts.read_json(ref)
    record = case.repository.artifacts.read_artifact_record(captured["report_ref"]["sha256"])
    Path(record["storage_path"]).write_text('{"tampered": true}')
    with pytest.raises(OSError, match="digest verification"):
        case.capture()
