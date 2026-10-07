"""Compatibility belongs to verified, immutable assignment receipts only."""
from __future__ import annotations

import copy
from dataclasses import replace
from unittest.mock import patch

import pytest

from pal.bunshin.ipc import BunshinManagerRpcError
from pal.bunshin.v2.contracts import SubmissionInvariantError
from pal.bunshin.v2.draft_values import recorded_cases, submission_work_items
from pal.bunshin.v2.role_protocol import stable_hash
from pal.bunshin.v2.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
from pal.bunshin.v2.verification_readiness import verification_corpus_snapshot
from tests.test_bunshin_dependency_repair_capture import case
from tests.test_bunshin_verifier_submission_freshness import managed_verifier
from tests.test_bunshin_verifier_tool_feedback import payload, run_delta, verifier


def submit_legacy(case, *, stale=False):
    submission = case.fx._submission([])
    submission["outcome"] = "pass"
    recorded = submission["recorded_results"][0]
    recorded.update(status="PASS", input_fingerprint="original-verifier-input")
    recorded.pop("case_binding", None)
    recorded.pop("definition_fingerprint", None)
    binding = verification_corpus_snapshot(case.workspace)
    binding.pop("case_revision", None)
    if stale:
        binding["input_fingerprint"] = "different-input"
    submission["tool_receipts"][0].update(ok=True, verification_binding=binding)
    submission.pop("verification_binding", None)
    case.submission = submission
    case.submission_ref = case.artifacts.put_json(submission, artifact_type="VerifierRoleSubmissionArtifact")
    case.repository.role_submissions.record_role_submission(
        assignment_id=case.assignment["assignment_id"], attempt_id_value=case.attempt_id,
        fencing_token=case.attempt_lease.fencing_token, artifact_ref=case.submission_ref.to_dict(),
        payload_hash=stable_hash(submission), settlement_action={"action_type": "SUBMIT_SEMANTIC_VERIFICATION"})


@pytest.mark.parametrize("settled", [False, True])
def test_verified_legacy_capture_preserves_frozen_receipt(case, settled):
    submit_legacy(case)
    if settled:
        case.repository.role_submissions.settle_role_assignment(
            assignment_id=case.assignment["assignment_id"], submission_payload_hash=stable_hash(case.submission))
    before = case.artifacts.read_bytes(case.submission_ref)
    rows = case.rows()
    captured = case.artifacts.read_json(case.capture())
    assert captured["status"] == "PASS"
    assert case.artifacts.read_bytes(case.submission_ref) == before
    assert case.rows() == rows


def test_verified_legacy_capture_rejects_explicit_binding_mismatch(case):
    submit_legacy(case, stale=True)
    with pytest.raises(SubmissionInvariantError, match="fresh validation|fresh.*receipt"):
        case.capture()


def test_legacy_capture_does_not_require_the_stopped_worker_lease(case):
    submit_legacy(case)
    case.repository.leases.release_lease(case.attempt_lease.resource_key, case.attempt_id,
                                        case.attempt_lease.fencing_token)
    original_prompt = case.collector._prompt

    def with_runtime_root(*args):
        prompt, workspace = original_prompt(*args)
        return prompt, {**workspace, "runtime_root": str(case.fx.root)}

    with (patch.object(case.collector, "_prompt", side_effect=with_runtime_root),
          patch.object(SubmissionDraftStore, "read", side_effect=AssertionError("recovery must not read a live draft"))):
        capture = case.artifacts.read_json(case.capture())
    assert capture["status"] == "PASS"


def test_verified_legacy_capture_rejects_changed_corpus(case):
    submit_legacy(case)
    case.path.write_text("def test_changed():\n    assert False\n")
    with pytest.raises(SubmissionInvariantError, match="fresh validation|fresh.*receipt"):
        case.capture()


def test_legacy_capture_requires_exact_authoritative_payload(case):
    submit_legacy(case)
    assignment = case.repository.role_assignments.read_role_assignment(case.assignment["assignment_id"])
    with patch.object(case.repository.role_assignments, "read_role_assignment",
                      return_value={**assignment, "submission_payload_hash": "wrong"}):
        with pytest.raises(SubmissionInvariantError, match="payload hash"):
            case.capture()


def completion_kwargs(case):
    return dict(effect={"effect_key": "original-effect"}, node=case.node,
        invocation_id=case.session, lease_resource=case.lease.resource_key, fencing_token=case.lease.fencing_token,
        candidate_ref=case.fx.candidate_ref, candidate_digest=case.fx.digest,
        candidate=case.artifacts.read_json(case.fx.candidate_ref), review_workspace=case.fx.repo,
        review_scratch=case.scratch, execution_adapter="software_git.v2", work_view={"module_name": "archive_verify"},
        submission=case.submission, terminal={"payload": {"role_assignment_id": case.assignment["assignment_id"]}},
        prompt_ref=case.prompt_ref, terminal_ref=case.artifacts.put_json({}, artifact_type="RoleTerminalArtifact"))


def test_verified_legacy_completion_reaches_settlement_without_rewriting_receipt(case):
    submit_legacy(case)
    before = case.artifacts.read_bytes(case.submission_ref)
    completion = case.fx.worker.components.verification_completion
    with (patch.object(case.repository.snapshots, "read_snapshot", return_value=case.node),
          patch.object(case.repository.transitions, "dispatch"),
          patch.object(completion.role_reports, "record_role_turn")):
        result = completion.complete_semantic_verifier(**completion_kwargs(case))
    pending = case.artifacts.read_json(result["result_artifact_ref"])
    assert pending["submission"] == case.submission
    assert case.artifacts.read_bytes(case.submission_ref) == before


@pytest.mark.parametrize("corruption", ["payload", "durability", "state", "assignment_scope"])
def test_legacy_completion_requires_verified_assignment_receipt(case, corruption):
    submit_legacy(case)
    assignment = case.repository.role_assignments.read_role_assignment(case.assignment["assignment_id"])
    if corruption == "payload":
        assignment = {**assignment, "submission_payload_hash": "wrong"}
    elif corruption == "durability":
        assignment = {**assignment, "submission_artifact_ref": {**assignment["submission_artifact_ref"], "durable": False}}
    elif corruption == "state":
        assignment = {**assignment, "state": "running"}
    else:
        assignment = {**assignment, "aggregate_id": "another-node"}
    with patch.object(case.repository.role_assignments, "read_role_assignment", return_value=assignment):
        with pytest.raises(SubmissionInvariantError):
            case.fx.worker.components.verification_completion.complete_semantic_verifier(**completion_kwargs(case))


def test_new_submission_cannot_claim_legacy_receipt_authority(managed_verifier):
    current, service, assignment = managed_verifier
    fixture, workspace, _, _, _, _ = current
    payload(run_delta(current))
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="verification")
    store = SubmissionDraftStore(fixture.runtime_root)
    draft = store.read(context)
    work = store.read(replace(context, draft_kind="work_items"))
    cases = recorded_cases(draft.payload)
    # Keep authoritative current case projection, but forge the legacy toggle
    # and a receipt without the mandatory current draft revision.
    receipts = copy.deepcopy(workspace["review_tool_evidence_refs"])
    for receipt in receipts:
        receipt["verification_binding"].pop("case_revision", None)
    submission = {"outcome": "pass", "findings": [], "advisories": [],
        "recorded_results": cases, "work_items": submission_work_items(work.payload["items"]),
        "tool_receipts": receipts, "accepted_legacy_receipt": True}
    with pytest.raises(BunshinManagerRpcError, match="fresh|receipt"):
        store.mark_submitted(context, expected_version=draft.version, expected_work_item_version=work.version,
                             submission_payload=submission)
    assert not service.repository.role_assignments.read_role_assignment(assignment["assignment_id"])["submission_artifact_ref"]
    assert store.read(context).status == "active"


def test_legacy_preserved_snapshot_ignores_only_absent_revision_field(case):
    submit_legacy(case)
    capture = case.artifacts.read_json(case.capture())
    previous = copy.deepcopy(capture)
    previous["corpus_snapshot"].pop("case_revision", None)
    workspace = {**case.workspace, "verification_scratch_only": False, "verification_case_revision": 0}
    case.collector._validate_preserved_workspace(previous, workspace, case.node)
    case.path.write_text("def test_later_edit():\n    assert False\n")
    with pytest.raises(SubmissionInvariantError, match="changed after evidence preservation"):
        case.collector._validate_preserved_workspace(previous, workspace, case.node)
