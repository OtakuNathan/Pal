"""Both verifier submission formats recheck freshness at receipt acceptance."""
from __future__ import annotations

import subprocess
from dataclasses import replace
from unittest.mock import patch

import pytest

from pal.bunshin.ipc import BunshinManagerRpcError
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateType
from pal.bunshin.role_gateway import RoleAssignmentGateway
from pal.bunshin.role_protocol import RoleAssignmentRequest
from pal.bunshin.service import BunshinWorkflowService
from pal.bunshin.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
from pal.bunshin.submission_errors import SubmissionValidationError, role_gateway_error_kind
from pal.bunshin.verification_builder import _submit
from pal.shared.tool_protocol import new_tool_call
from tests.test_bunshin_verifier_tool_feedback import payload, run_delta, verifier


def submit_generic(workspace):
    return _submit(new_tool_call(name="op_bunshin_verification_submit", args={}, call_id="generic-submit"),
                   workspace, [])


def assert_local_drafts_open(fixture, workspace):
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="verification")
    store = SubmissionDraftStore(fixture.runtime_root)
    for kind in ("verification", "work_items"):
        assert store.read(replace(context, draft_kind=kind)).status == "active"


def test_generic_local_submit_rejects_stale_cases(verifier):
    fixture, workspace, probe, _, _, _ = verifier
    payload(run_delta(verifier))
    original = probe.read_text()
    probe.write_text("assert False\n")

    with pytest.raises(ValueError, match="stale|corpus|fresh"):
        submit_generic(workspace)
    assert_local_drafts_open(fixture, workspace)

    probe.write_text(original)
    assert submit_generic(workspace).ok


def test_generic_local_receipt_rechecks_corpus(verifier, monkeypatch):
    fixture, workspace, probe, _, _, _ = verifier
    payload(run_delta(verifier))
    original = probe.read_text()
    put_json = ContentAddressedArtifactStore.put_json
    edited = []

    def edit_after_preflight(self, value, **kwargs):
        ref = put_json(self, value, **kwargs)
        if kwargs.get("artifact_type") == "VerifierRoleSubmissionArtifact":
            edited.append(True)
            probe.write_text("assert False\n")
        return ref

    with monkeypatch.context() as scoped:
        scoped.setattr(ContentAddressedArtifactStore, "put_json", edit_after_preflight)
        with pytest.raises(SubmissionValidationError, match="stale|corpus|fresh"):
            submit_generic(workspace)
    assert edited == [True], "The race must happen after generic preflight passed"
    assert_local_drafts_open(fixture, workspace)

    probe.write_text(original)
    assert submit_generic(workspace).ok


@pytest.fixture
def managed_verifier(verifier):
    fixture, workspace, _, call, _, view = verifier
    service = BunshinWorkflowService(fixture.runtime_root)
    repo = service.repository
    workflow, node = "submission-freshness", "submission-freshness-node"
    view_ref = service.artifacts.put_json(view, artifact_type="ModuleWorkViewArtifact")
    repo.transitions.dispatch(ActionEnvelope(action_type="CREATE_WORKFLOW", workflow_id=workflow,
        aggregate_type=AggregateType.WORKFLOW, aggregate_id=workflow, actor="test", expected_version=0))
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace["repo_path"], text=True).strip()
    repo.transitions.dispatch(ActionEnvelope(action_type="CREATE_NODE_RUN", workflow_id=workflow,
        aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node, actor="test", expected_version=0,
        payload={"epoch_id": "epoch", "module_name": "router", "unit_contract_ref": {"sha256": "contract"},
                 "unit_work_view_ref": view_ref.to_dict(), "candidate_digest": candidate,
                 "path_policy": {"verification_corpus": view["verification_corpus"]}}))
    repo.role_sessions.ensure_role_session(session_id="submission-freshness-session", workflow_id=workflow,
        aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node, role="verifier", mode="module",
        role_profile_id="software_engineering.v2_verifier", family_binding_sha="binding", scope_kind="module", subject_key="router")
    assignment = repo.role_assignments.create_role_assignment(RoleAssignmentRequest(
        assignment_key="submission-freshness", session_id="submission-freshness-session", workflow_id=workflow,
        aggregate_type=AggregateType.DAG_NODE_RUN.value, aggregate_id=node, role="verifier", mode="module",
        role_profile_id="software_engineering.v2_verifier", family_binding_sha="binding", input_fingerprint="freshness-input",
        required_inputs=(), input_refs={"module_work_view": view_ref.to_dict()},
        execution_spec={"effect_type": "run_verifier_role"}, submission_kind="verification"))
    attempt = repo.role_assignments.claim_role_assignment(assignment["assignment_id"])
    resource = "assignment:" + assignment["assignment_id"]
    fence = repo.leases.claim_lease(resource, attempt["attempt_id"], ttl_seconds=120).fencing_token
    workspace["bunshin_v2"].update(workflow_id=workflow, invocation_id=attempt["attempt_id"],
        lease_resource_key=resource, fencing_token=fence, authoring_input_fingerprint="freshness-input")
    workspace["invocation_id"] = attempt["attempt_id"]
    prompt = service.artifacts.put_json({"workspace": workspace}, artifact_type="RolePromptPackArtifact")
    repo.role_attempts.start_role_attempt(assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
        lease_resource_key=resource, fencing_token=fence, prompt_pack_ref=prompt.to_dict())
    token = repo.role_access.issue_role_attempt_access_token(assignment_id=assignment["assignment_id"],
        attempt_id_value=attempt["attempt_id"], fencing_token=fence)
    gateway = RoleAssignmentGateway(service)

    class Remote:
        def request_sync(self, method, params):
            with patch("pal.bunshin.role_gateway_client.role_gateway_client_from_env", return_value=None):
                try:
                    return gateway.call(method, {"access_token": token, **params})
                except Exception as exc:
                    raise BunshinManagerRpcError(str(exc), kind=role_gateway_error_kind(exc)) from exc

    with patch("pal.bunshin.role_gateway_client.role_gateway_client_from_env", return_value=Remote()):
        payload(call("update_checklist", plan=[{"step": "audit", "status": "completed"}]))
        yield verifier, service, assignment


def race_after_manager_preflight(monkeypatch, service, probe):
    put_json = service.artifacts.put_json
    edited = []

    def edit_after_preflight(value, **kwargs):
        ref = put_json(value, **kwargs)
        if kwargs.get("artifact_type") == "VerifierRoleSubmissionArtifact":
            edited.append(True)
            probe.write_text("assert False\n")
        return ref

    monkeypatch.setattr(service.artifacts, "put_json", edit_after_preflight)
    return edited


def assert_manager_drafts_open(fixture, workspace, service, assignment):
    assert not service.repository.role_assignments.read_role_assignment(assignment["assignment_id"])["submission_artifact_ref"]
    assert_local_drafts_open(fixture, workspace)


def test_generic_manager_receipt_rechecks_corpus(managed_verifier, monkeypatch):
    current, service, assignment = managed_verifier
    fixture, workspace, probe, _, _, _ = current
    payload(run_delta(current))
    original = probe.read_text()

    with monkeypatch.context() as scoped:
        edited = race_after_manager_preflight(scoped, service, probe)
        with pytest.raises(BunshinManagerRpcError, match="stale|corpus|fresh") as rejected:
            submit_generic(workspace)
    assert edited == [True], "The race must happen after the Manager's initial validation"
    assert rejected.value.kind == "submission_validation"
    assert_manager_drafts_open(fixture, workspace, service, assignment)

    probe.write_text(original)
    assert submit_generic(workspace).ok
    assert service.repository.role_assignments.read_role_assignment(assignment["assignment_id"])["submission_artifact_ref"]


def test_swe_manager_receipt_race_is_correctable_validation(managed_verifier, monkeypatch):
    current, service, assignment = managed_verifier
    fixture, workspace, probe, call, _, _ = current
    payload(run_delta(current))
    original = probe.read_text()

    with monkeypatch.context() as scoped:
        edited = race_after_manager_preflight(scoped, service, probe)
        rejected = call("submit_verification_pass")
    assert edited == [True], "The race must happen after the Manager's initial validation"
    assert not rejected.ok
    assert rejected.structured["error_code"] == "verification_validation"
    assert rejected.structured["effect"] == "not_started"
    assert rejected.structured["retry"] == "correct_input"
    assert "stale" in rejected.llm_text or "corpus" in rejected.llm_text
    assert_manager_drafts_open(fixture, workspace, service, assignment)

    probe.write_text(original)
    assert payload(call("submit_verification_pass"))["submitted"]
