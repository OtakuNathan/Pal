"""A receipt and its actual execution workspace survive source advances."""
from __future__ import annotations

import asyncio
import base64
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from pal.bunshin.contracts import ActionEnvelope, AggregateType
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.role_protocol import RoleAssignmentRequest, stable_hash
from pal.bunshin.semantic_orchestration.attempt_models import RoleAttemptRequest
from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator
from pal.bunshin.semantic_orchestration.role_environment import _bind_role_attempt_sandbox
from pal.bunshin.semantic_orchestration.role_inputs import _role_session_scope
from pal.bunshin.semantic_orchestration.verification_workspace import _prepare_standalone_review_workspace
from pal.bunshin.service import BunshinWorkflowService
from pal.shared import BunshinInvocationPack


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo: Path, value: str) -> str:
    (repo / "facts.txt").write_text(value, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", value)
    return git(repo, "rev-parse", "HEAD")


def setup_workflow(tmp_path, *, review=False):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    base = commit(source, "frozen")
    service = BunshinWorkflowService(tmp_path / "runtime")
    service.create_task({"task_id": "task", "title": "Fixture", "objective": "Summarize facts",
        "profile": "generic", "workspace": {"kind": "existing_repo", "repo_path": str(source)},
        "references": [{"name": "facts", "path": "facts.txt"}]})
    request = {"workflow_id": "wf", "task_id": "task", "operation": "new_requirement",
        "goal": "Summarize facts", "task_spec": {"objective": "Summarize facts"},
        "delivery_binding": {"channel_id": "socket_test", "channel_kind": "socket",
            "reply_target": {"session_id": "test", "request_id": "req"},
            "control_scope_key": "socket:socket_test:test"}}
    if review:
        ref = service.artifacts.put_json({"purpose": "Review fixture"}, artifact_type="CodeSnapshotArtifact")
        request.update(operation="standalone_review", artifact_ref=ref.to_dict())
        request.pop("task_spec")
    service.start_workflow(request)
    return service, source, base


def dispatch(service, aggregate_type, aggregate_id, action, payload=None):
    prior = service.repository.snapshots.read_snapshot(aggregate_type, aggregate_id)
    return service.repository.transitions.dispatch(ActionEnvelope(
        action_type=action, workflow_id="wf", aggregate_type=aggregate_type,
        aggregate_id=aggregate_id, actor="test", expected_version=prior.version if prior else 0,
        payload=payload or {},
    )).snapshot


async def prepared_pack(stages, command, primary):
    stage = await stages.workspace_preparation.execute(command)
    refs = await stages.reference_binding.execute(command, stage)
    pack = BunshinInvocationPack(invocation_id=command.invocation_id,
        workspace={**stage.workspace, "output_policy": {"primary_artifact": primary}},
        metadata={"requirements_brief": {"references": refs.references}})
    with patch("pal.bunshin.sandbox.sandbox_supported_backend", return_value="bwrap"):
        return _bind_role_attempt_sandbox(stages.repository.runtime_root, pack, run_id=stage.run_id,
                                          durable_prompt_reused=False)


def record_receipt(service, command, pack, *, kind, payload, artifact_type, action):
    repo, artifacts = service.repository, service.artifacts
    workflow = repo.snapshots.read_snapshot(AggregateType.WORKFLOW, "wf")
    family = workflow.payload["family_binding_ref"]["sha256"]
    scope, subject = _role_session_scope(command.snapshot, command.activation)
    role, mode = command.activation.role.value, command.activation.mode.value
    repo.role_sessions.ensure_role_session(session_id=command.invocation_id, workflow_id="wf",
        aggregate_type=command.snapshot.aggregate_type, aggregate_id=command.snapshot.aggregate_id,
        role=role, mode=mode, role_profile_id=command.profile, family_binding_sha=family,
        scope_kind=scope, subject_key=subject)
    assignment = repo.role_assignments.create_role_assignment(RoleAssignmentRequest(
        assignment_key="fixture-assignment", session_id=command.invocation_id, workflow_id="wf",
        aggregate_type=command.snapshot.aggregate_type.value, aggregate_id=command.snapshot.aggregate_id,
        role=role, mode=mode, role_profile_id=command.profile, family_binding_sha=family,
        input_fingerprint="fixture-input", required_inputs=(), input_refs={},
        execution_spec=dict(command.effect), submission_kind=kind))
    attempt = repo.role_assignments.claim_role_assignment(assignment["assignment_id"])
    attempt_id = attempt["attempt_id"]
    resource = "assignment:" + assignment["assignment_id"]
    lease = repo.leases.claim_lease(resource, attempt_id)
    prompt_ref = artifacts.put_json(pack.to_dict(), artifact_type="RolePromptPackArtifact")
    repo.role_attempts.start_role_attempt(assignment_id=assignment["assignment_id"], attempt_id_value=attempt_id,
        lease_resource_key=resource, fencing_token=lease.fencing_token, prompt_pack_ref=prompt_ref.to_dict())
    submission = artifacts.put_json(payload, artifact_type=artifact_type)
    repo.role_submissions.record_role_submission(assignment_id=assignment["assignment_id"], attempt_id_value=attempt_id,
        fencing_token=lease.fencing_token, artifact_ref=submission.to_dict(), payload_hash=stable_hash(payload),
        settlement_action={"action_type": action})
    repo.leases.release_lease(resource, attempt_id, lease.fencing_token)
    return assignment["assignment_id"], prompt_ref, submission


@pytest.mark.parametrize("settled,legacy_policy", [(False, False), (True, False), (False, True)],
                         ids=["result-recorded", "settled", "legacy-output-policy"])
def test_exact_receipt_replay_precedes_workspace_preparation(tmp_path, settled, legacy_policy):
    service, source, _ = setup_workflow(tmp_path)
    snapshot = dispatch(service, AggregateType.ARCHITECTURE_REVISION, "arch", "CREATE_ARCHITECTURE_REVISION")
    components = SemanticOrchestrator(service).components
    stages = components.attempt_execution
    command = RoleAttemptRequest(effect={"effect_key": "effect", "effect_id": "effect", "effect_type": "run_architect_role"}, snapshot=snapshot,
        invocation_id="session", lease_resource="arch:arch", fencing_token=1,
        profile=components.workflow_facts.profile_for_role("wf", "architect"),
        activation=RoleActivation(OrchestrationRole.ARCHITECT, RoleMode.AUTHOR), instruction="Fixture",
        reference_refs={}, workspace_override=None, prepare_workspace=True)
    pack = asyncio.run(prepared_pack(stages, command, "architect.yaml"))
    if legacy_policy:
        workspace = dict(pack.workspace)
        workspace.pop("output_policy")
        pack = BunshinInvocationPack.from_dict({**pack.to_dict(), "workspace": workspace})
    payload = {"contract": "fixture"}
    assignment_id, prompt_ref, submission = record_receipt(service, command, pack, kind="contract",
        payload=payload, artifact_type="ContractRoleSubmissionArtifact", action="DATA_ARCHITECT_COMPLETED")
    if settled:
        service.repository.role_submissions.settle_role_assignment(assignment_id=assignment_id,
            submission_payload_hash=stable_hash(payload))
    commit(source, "new source")
    # Restart the orchestration, including its in-memory assignment index.
    stages = SemanticOrchestrator(BunshinWorkflowService(service.runtime_root)).components.attempt_execution
    with patch.object(stages.workspace_preparation, "execute", side_effect=AssertionError("receipt must not prepare a worker")):
        terminal, selected, _ = asyncio.run(stages.run_profile_inner(**replace(command, fencing_token=2).__dict__))
    assert selected == prompt_ref
    assert terminal["payload"]["durable_receipt_replay"]
    assert terminal["payload"]["submission_receipt"] == submission.to_dict()
    assert terminal["payload"]["primary_artifact"]["relative_path"] == "architect.yaml"
    assert (Path(pack.workspace["repo_path"]) / "facts.txt").read_text() == "frozen"


@pytest.mark.parametrize("legacy", [False, True], ids=["canonical", "legacy-isolated"])
def test_standalone_review_publishes_exact_retained_target_and_evidence(tmp_path, legacy):
    service, source, base = setup_workflow(tmp_path, review=True)
    repo = service.repository
    workflow = repo.snapshots.read_snapshot(AggregateType.WORKFLOW, "wf")
    request = service.artifacts.read_json(workflow.payload["request_ref"])
    ref = request["input_artifact_ref"]
    dispatch(service, AggregateType.STANDALONE_REVIEW, "review", "CREATE_STANDALONE_REVIEW", {"review_request_ref": ref})
    dispatch(service, AggregateType.STANDALONE_REVIEW, "review", "QUEUE_REVIEW")
    lease = repo.leases.claim_lease("standalone-review:review", "review-session")
    dispatch(service, AggregateType.STANDALONE_REVIEW, "review", "START_REVIEW", {
        "active_worker_id": "review-session", "fencing_token": lease.fencing_token,
        "lease_resource_key": lease.resource_key})
    effect = {"effect_key": "review-effect", "effect_id": "review-effect", "effect_type": "run_reviewer_role",
              "workflow_id": "wf", "aggregate_type": "standalone_review", "aggregate_id": "review"}
    components = SemanticOrchestrator(service).components
    captured = {}

    async def interrupt_after_receipt(**kwargs):
        command = RoleAttemptRequest(**kwargs)
        if legacy:
            old = dict(command.workspace_override)
            old["workspace_binding"] = "ephemeral_artifact"
            old.pop("standalone_review_target_sha", None)
            command = replace(command, workspace_override=old)
        pack = await prepared_pack(components.attempt_execution, command, "contract_review.json")
        scratch = Path(pack.workspace["review_scratch_dir"])
        (scratch / "result.txt").write_text("Evidence for the original review", encoding="utf-8")
        captured["assignment"], captured["prompt"], _ = record_receipt(service, command, pack,
            kind="standalone_review", payload={"verdict": "PASS", "findings": [], "advisories": []},
            artifact_type="StandaloneReviewRoleSubmissionArtifact", action="REPORT_PRODUCED")
        captured["workspace"] = Path(pack.workspace["repo_path"])
        captured["scratch"] = scratch
        raise RuntimeError("interrupted after receipt")

    with patch.object(components.attempt_execution, "run_profile", side_effect=interrupt_after_receipt):
        with pytest.raises(RuntimeError, match="interrupted after receipt"):
            asyncio.run(components.standalone_review.run_standalone_review(effect))
    assert git(captured["workspace"], "rev-parse", "HEAD") == base
    assert commit(source, "new source") != base
    # The original source is no longer needed, including for a legacy pack.
    source.rename(tmp_path / "source-moved")
    recovered = SemanticOrchestrator(BunshinWorkflowService(service.runtime_root)).components
    with patch.object(recovered.attempt_execution.workspace_preparation, "execute",
                      side_effect=AssertionError("receipt must not prepare a worker")):
        report = asyncio.run(recovered.standalone_review.run_standalone_review(effect))
    with repo.database.read_connection() as connection:
        child = connection.execute("SELECT child_sha256 FROM bunshin_artifact_refs WHERE parent_sha256=? AND relation='test_workspace'",
                                   (report["result_artifact_ref"]["sha256"],)).fetchone()
    evidence = service.artifacts.read_json(child[0])
    assert evidence["candidate_identity"] == base
    assert {item["path"]: base64.b64decode(item["content_base64"]) for item in evidence["files"]} == {
        "result.txt": b"Evidence for the original review"}
    assert repo.role_assignments.read_role_assignment(captured["assignment"])["state"] == "settled"


def test_standalone_provisioning_itself_preserves_the_first_checkout(tmp_path):
    service, source, base = setup_workflow(tmp_path, review=True)
    first = _prepare_standalone_review_workspace(service.runtime_root, "review", source)
    (first[1] / "note.txt").write_text("retained", encoding="utf-8")
    commit(source, "new source")
    source.rename(tmp_path / "source-moved")
    second = _prepare_standalone_review_workspace(service.runtime_root, "review", source)
    assert second == first
    assert git(second[0], "rev-parse", "HEAD") == base
    assert (second[1] / "note.txt").read_text() == "retained"
