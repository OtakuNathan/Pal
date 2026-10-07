"""Live-style worker tools through the authenticated Manager and durable receipt."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import replace
from unittest.mock import patch

import pytest

from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
from pal.bunshin.contracts import ActionEnvelope, AggregateType
from pal.bunshin.role_gateway import RoleAssignmentGateway
from pal.bunshin.role_protocol import RoleAssignmentRequest
from pal.bunshin.service import BunshinV2WorkflowService
from pal.bunshin.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
from pal.bunshin.semantic_evidence import recorded_cases
from pal.bunshin.verification_readiness import verification_corpus_snapshot
from pal.bunshin.work_items import submission_work_items
from pal.shared.tool_protocol import new_tool_call
from tests.test_bunshin_verifier_tool_feedback import payload, verifier


@pytest.mark.parametrize("verifier", [True], indirect=True)
def test_manager_rejects_stale_required_case_then_accepts_fresh_replays(verifier):
    fixture, workspace, probe, _, original_runtime, view = verifier
    service = BunshinV2WorkflowService(fixture.runtime_root)
    repo = service.repository
    workflow, node = "manager-freshness", "manager-node"
    view_ref = service.artifacts.put_json(view, artifact_type="ModuleWorkViewArtifact")
    repo.transitions.dispatch(ActionEnvelope(action_type="CREATE_WORKFLOW", workflow_id=workflow,
        aggregate_type=AggregateType.WORKFLOW, aggregate_id=workflow, actor="test", expected_version=0))
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace["repo_path"], text=True).strip()
    repo.transitions.dispatch(ActionEnvelope(action_type="CREATE_NODE_RUN", workflow_id=workflow,
        aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node, actor="test", expected_version=0,
        payload={"epoch_id": "epoch", "module_name": "router", "unit_contract_ref": {"sha256": "contract"},
                 "unit_work_view_ref": view_ref.to_dict(), "candidate_digest": candidate,
                 "path_policy": {"verification_corpus": view["verification_corpus"]}}))
    repo.role_sessions.ensure_role_session(session_id="freshness-session", workflow_id=workflow,
        aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node, role="verifier", mode="module",
        role_profile_id="software_engineering.v2_verifier", family_binding_sha="binding", scope_kind="module", subject_key="router")
    assignment = repo.role_assignments.create_role_assignment(RoleAssignmentRequest(
        assignment_key="manager-freshness", session_id="freshness-session", workflow_id=workflow,
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
                return gateway.call(method, {"access_token": token, **params})
    remote = Remote()
    runtime = BunshinScopedExecutionRuntime(fixture.adapter, original_runtime.allowed_capabilities, workspace)
    index = 0
    def call(tool_name, **args):
        nonlocal index
        index += 1
        return asyncio.run(runtime.execute_tool_async(new_tool_call(name=tool_name, args=args, call_id=f"manager-call-{index}")))
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind="verification")
    command = f"{sys.executable} tests/router/verifier/probe.py"
    try:
        with patch("pal.bunshin.role_gateway_client.role_gateway_client_from_env", return_value=remote):
            payload(call("update_checklist", plan=[{"step": "audit", "status": "completed"}]))
            for name in ("finding_first", "finding_second"):
                payload(call("run_verification_historical_regression", name=name, command=command))
            payload(call("run_verification_diff_risk", name="current delta", command=command))
            probe.write_text("assert False\n")
            payload(call("run_verification_diff_risk", name="unrelated", command="true"))
            store = SubmissionDraftStore(fixture.runtime_root)
            snap = store.read(context)
            work = store.read(replace(context, draft_kind="work_items"))
            draft_submission = {"schema_version": "4", "outcome": "pass", "findings": [], "advisories": [],
                "work_items": submission_work_items(work.payload["items"]), "recorded_results": recorded_cases(snap.payload),
                "tool_receipts": workspace["review_tool_evidence_refs"], "verification_binding": verification_corpus_snapshot(workspace)}
            with pytest.raises(ValueError, match="stale recorded cases"):
                remote.request_sync("draft_submit", {"context": context.to_dict(), "expected_version": snap.version,
                    "expected_work_item_version": work.version, "submission": draft_submission})
            assert not repo.role_assignments.read_role_assignment(assignment["assignment_id"])["submission_artifact_ref"]
            failed = payload(call("run_verification_historical_regression", name="finding_first", command=command))
            assert failed["case"]["status"] == "FAIL"
            probe.write_text("assert 2 + 2 == 4\n")
            for name in ("finding_first", "finding_second"):
                payload(call("run_verification_historical_regression", name=name, command=command))
            payload(call("remove_verification_case", name="unrelated", reason="Not relevant coverage"))
            payload(call("run_verification_diff_risk", name="current delta", command=command))
            assert payload(call("submit_verification_pass"))["submitted"]
            accepted = repo.role_assignments.read_role_assignment(assignment["assignment_id"])
            assert accepted["state"] == "result_recorded"
            sealed = service.artifacts.read_json(accepted["submission_artifact_ref"])
            probe.write_text("assert False\n")
            receipt = remote.request_sync("draft_submit", {"context": context.to_dict(), "submission": sealed})
            assert receipt["submission_payload_hash"] == accepted["submission_payload_hash"]
            from pal.bunshin.swe_verification import semantic_verification_submission_errors
            errors = semantic_verification_submission_errors(sealed, work_view=view, changed_paths=[],
                current_case_paths=["tests/router/verifier/probe.py"], corpus_scope=view["verification_corpus"],
                scratch_only=False, workspace=workspace)
            assert any("current corpus" in error or "stale recorded cases" in error for error in errors)
    finally:
        runtime.base_runtime.runtime.shutdown()
