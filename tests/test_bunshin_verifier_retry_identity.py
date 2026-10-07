"""Real preparation and pause/resume must preserve unsubmitted verifier drafts."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pal.bunshin.contracts import ActionEnvelope, AggregateType, SubmissionInvariantError
from pal.bunshin.cycle_protocol import AssignmentKind, CycleSlot
from pal.bunshin.orchestration import reconcile_control_requests
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.semantic_orchestration.attempt_models import RoleAttemptRequest
from pal.bunshin.semantic_evidence import record_unavailable_evidence, recorded_cases
from pal.bunshin.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
from pal.bunshin.work_items import edit_finding_tool_result, findings_from_work_items
from pal.bunshin.verification_builder import _remove_case
from pal.bunshin.swe_verification import verification_outcome_readiness
from pal.shared.tool_protocol import new_tool_call
from tests import test_bunshin_v2_verifier_scope_recovery as scope_fixture


VERIFIER = RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE)


class PauseResumeVerifier:
    def __init__(self, monkeypatch):
        self.fx = scope_fixture.VerifierScopeRecoveryTests()
        self.fx.setUp()
        self.service, self.repo, self.artifacts = self.fx.service, self.fx.repository, self.fx.artifacts
        self.worker, self.workflow = self.fx.worker, self.fx.workflow_id
        self.stages = self.worker.components.attempt_execution
        self.sequence = 0
        self.binding = self.service.catalog.publish_family_binding("software_engineering.v2_coder")
        request = self.artifacts.put_json({"workspace": {"kind": "existing_repo", "repo_path": str(self.fx.repo)},
            "references": []}, artifact_type="WorkflowRequestArtifact")
        self.dispatch(AggregateType.WORKFLOW, self.workflow, "CREATE_WORKFLOW", {
            "family_binding_ref": self.binding.to_dict(), "request_ref": request.to_dict()})
        self.dispatch(AggregateType.WORKFLOW, self.workflow, "START_WORKFLOW")
        graph = self.artifacts.put_json(self.fx.graph.to_dict(), artifact_type="GraphIRArtifact")
        self.dispatch(AggregateType.EXECUTION_EPOCH, "epoch-scope", "CREATE_EXECUTION_EPOCH", {
            "architecture_manifest_ref": graph.to_dict(), "topology_ref": graph.to_dict()})
        self.dispatch(AggregateType.EXECUTION_EPOCH, "epoch-scope", "START_EXECUTION")
        self.dispatch(AggregateType.EXECUTION_EPOCH, "epoch-scope", "NODES_COMPILED", {
            "node_ids": [self.fx._node_id(name) for name in self.fx.graph.nodes]})
        self.dispatch(AggregateType.WORKFLOW, self.workflow, "LINK_EXECUTION_EPOCH", {"execution_epoch_id": "epoch-scope"})
        self.fx._accept_provider()
        for action, payload in (
            ("DEPENDENCIES_ACCEPTED", {"accepted_producer_dependency_node_ids": [], "epoch_frozen": False}),
            ("START_PRODUCING", {"fencing_token": 1}),
            ("SUBMIT_CANDIDATE", {"fencing_token": 1}),
            ("QUIESCE_COMPLETED", {"fencing_token": 1, "process_group_reaped": True,
                "exclusive_workspace_lock": True, "workspace_fingerprint": "candidate-tree"}),
            ("CANDIDATE_SNAPSHOTTED", {"candidate_ref": self.fx.candidate_ref.to_dict(),
                "candidate_digest": self.fx.digest, "workspace_fingerprint": "candidate-tree"}),
            ("VERIFICATION_DEPENDENCIES_ACCEPTED", {"accepted_dependency_node_ids": [], "epoch_frozen": False}),
        ):
            self.fx._dispatch("archive_verify", action, payload)
        self.fx.coordinator.start_assignment(workflow_id=self.workflow, node_name="archive_verify",
            slot=CycleSlot.PRODUCER, kind=AssignmentKind.INITIAL, input_fingerprint="producer")
        self.fx.coordinator.producer_submitted(workflow_id=self.workflow, node_name="archive_verify",
                                              product_ref=self.fx.candidate_ref.sha256)
        # Optional environment integrations are the only preparation test doubles.
        monkeypatch.setattr("pal.bunshin.semantic_orchestration.attempt_workspace_preparation.prewarm_workspace_lsp",
                            lambda **kwargs: {"status": "unavailable", "servers": []})
        monkeypatch.setattr(self.worker.components.role_cleanup, "release_managed_lsp_workspace",
                            AsyncMock(return_value={"status": "unavailable"}))
        self.view = self.artifacts.put_json({"module_name": "archive_verify", "requirements": {},
            "verification_corpus": {"kind": "directory", "path": "tests/archive_verify/verifier"}},
            artifact_type="ModuleWorkViewArtifact")
        self.diff = self.artifacts.put_json({"candidate_digest": self.fx.digest, "changed_paths": []},
                                           artifact_type="CandidateDiffArtifact")

    def dispatch(self, kind, identity, action, payload=None):
        current = self.repo.snapshots.read_snapshot(kind, identity)
        self.sequence += 1
        return self.repo.transitions.dispatch(ActionEnvelope(action_type=action, workflow_id=self.workflow,
            aggregate_type=kind, aggregate_id=identity, actor="regression", expected_version=current.version if current else 0,
            idempotency_key=f"retry-regression:{self.sequence}", payload=payload or {})).snapshot

    async def admit(self):
        node = self.fx._node()
        with self.repo.database.read_connection() as connection:
            row = connection.execute(
                "SELECT o.* FROM bunshin_v2_outbox o JOIN bunshin_v2_domain_events e ON e.event_id = o.event_id "
                "WHERE o.aggregate_id = ? AND o.effect_type IN ('admit_verifier_role', 'resume_semantic_state') "
                "ORDER BY e.aggregate_version DESC LIMIT 1", (node.aggregate_id,),
            ).fetchone()
        effect = dict(row)
        effect["payload"] = json.loads(effect.pop("payload_json"))
        await self.worker.components.node_control.resume_node(effect)
        node = self.fx._node()
        assert node.state == "REVIEWING"
        effect = self.fx._stored_effect(node, effect_type="run_verifier_role")
        workspace = {"kind": "existing_repo", "repo_path": str(self.fx.repo), "workspace_binding": "canonical",
                     "write_path_scopes": [{"kind": "directory", "path": "tests/archive_verify/verifier"}]}
        command = RoleAttemptRequest(effect=effect, snapshot=node, invocation_id=node.payload["active_worker_id"],
            lease_resource=node.payload["lease_resource_key"], fencing_token=node.payload["fencing_token"],
            profile="software_engineering.v2_verifier", activation=VERIFIER, instruction="Continue the bounded verification.",
            reference_refs={"module_work_view": self.view, "candidate_diff": self.diff},
            workspace_override=workspace, prepare_workspace=False)
        prepared = await self.stages.workspace_preparation.execute(command)
        verifier_context = await self.stages.verifier_context.execute(command, prepared)
        refs = await self.stages.reference_binding.execute(command, prepared)
        prompt = await self.stages.prompt_construction.execute(command, refs, verifier_context, prepared)
        reuse = await self.stages.assignment_reuse.execute(command, refs, SimpleNamespace(pack=prompt.pack), prepared)
        session = await self.stages.role_session.execute(command, reuse, prompt, refs, prepared)
        assignment = session.assignment
        attempt = self.repo.role_assignments.claim_role_assignment(assignment["assignment_id"])
        resource = "assignment:" + assignment["assignment_id"]
        lease = self.repo.leases.claim_lease(resource, attempt["attempt_id"], ttl_seconds=300)
        context = SubmissionDraftContext(workflow_id=self.workflow, invocation_id=attempt["attempt_id"],
            lease_resource_key=resource, fencing_token=lease.fencing_token, role="verifier", mode="module",
            draft_kind="verification", input_fingerprint=prompt.input_fingerprint)
        workspace = {**prepared.workspace, "runtime_root": str(self.fx.root), "bunshin_v2": {
            **prompt.pack.metadata["bunshin_v2"], **context.to_dict(), "authoring_input_fingerprint": context.input_fingerprint}}
        prompt_ref = self.artifacts.put_json({**prompt.pack.to_dict(), "workspace": workspace}, artifact_type="RolePromptPackArtifact")
        self.repo.role_attempts.start_role_attempt(assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
            lease_resource_key=resource, fencing_token=lease.fencing_token, prompt_pack_ref=prompt_ref.to_dict())
        return context, workspace, assignment, prepared

    async def pause_resume(self):
        self.service.control_workflow(workflow_id=self.workflow, command="pause", actor="operator", source_channel="test")
        reconcile_control_requests(self.repo, self.workflow)
        for name in self.fx.graph.nodes:
            node = self.fx._node(name)
            if node.state == "PAUSE_REQUESTED":
                await self.worker.components.node_control.stop_node_worker(
                    self.fx._stored_effect(node, effect_type="pause_role"), cancel=False)
        reconcile_control_requests(self.repo, self.workflow)
        assert self.repo.snapshots.read_snapshot(AggregateType.WORKFLOW, self.workflow).state == "PAUSED"
        assert self.fx._node().state == "PAUSED"
        resumed = self.service.resume_workflow(workflow_id=self.workflow, actor="operator", source_channel="test")
        assert resumed["status"] == "resumed"
        reconcile_control_requests(self.repo, self.workflow)
        assert self.fx._node().state == "REVIEW_QUEUED"


@pytest.fixture
def lifecycle(monkeypatch):
    fixture = PauseResumeVerifier(monkeypatch)
    try:
        yield fixture
    finally:
        fixture.fx.doCleanups()


def test_real_pause_resume_preparation_change_preserves_current_editable_drafts(lifecycle):
    async def scenario():
        first, workspace, assignment, prepared = await lifecycle.admit()
        finding = edit_finding_tool_result(new_tool_call(name="op_bunshin_update_finding", call_id="current-finding",
            args={"finding_id": "current-defect", "expected_revision": 0, "finding_kind": "module_defect",
                  "priority": "p1", "summary": "This current concern still requires verification."}), workspace)
        assert finding.ok, finding.llm_text
        case = record_unavailable_evidence(new_tool_call(name="op_bunshin_verification_check_unavailable", call_id="current-case",
            args={"name": "current probe", "obligation": "platform_probe", "reason": "Offline fixture omits this environment."}),
            workspace=workspace, draft_kind="verification")
        assert case.ok, case.llm_text
        source = SubmissionDraftStore(lifecycle.fx.root).read(first)
        source_work = SubmissionDraftStore(lifecycle.fx.root).read(replace(first, draft_kind="work_items"))
        (lifecycle.fx.repo / "tests/archive_verify/verifier/new_case.py").write_text("def regression():\n    return True\n")
        await lifecycle.pause_resume()
        assert lifecycle.repo.role_assignments.read_role_assignment(assignment["assignment_id"])["state"] == "cancelled"
        second, resumed, replacement, current_preparation = await lifecycle.admit()
        assert replacement["assignment_id"] != assignment["assignment_id"]
        assert replacement["session_id"] == assignment["session_id"]
        assert second.input_fingerprint == first.input_fingerprint
        assert prepared.bound_reference_refs["workspace_preparation"] != current_preparation.bound_reference_refs["workspace_preparation"]
        differing = {key for key in assignment["input_refs"] if assignment["input_refs"][key] != replacement["input_refs"][key]}
        assert differing == {"workspace_preparation"}
        store = SubmissionDraftStore(lifecycle.fx.root)
        inherited = store.read(second)
        findings = findings_from_work_items(resumed)
        assert ([item["finding_id"] for item in findings], [item["name"] for item in recorded_cases(inherited.payload)]) == (
            ["current-defect"], ["current probe"])
        readiness = verification_outcome_readiness(resumed, inherited.payload, outcome="pass")
        assert not readiness["ready"]
        assert "PASS requires an empty finding Draft" in readiness["blockers"]
        revised = edit_finding_tool_result(new_tool_call(name="op_bunshin_update_finding", call_id="correct-current",
            args={"finding_id": "current-defect", "expected_revision": 1, "finding_kind": "verification_defect",
                  "priority": "p1", "summary": "Corrected the current probe diagnosis."}), resumed)
        assert revised.ok and revised.structured["revision"] == 2
        removed = _remove_case(new_tool_call(name="op_bunshin_verification_remove_case", call_id="remove-current-case",
            args={"name": "current probe", "reason": "Replace this unsubmitted probe."}), resumed, draft_kind="verification")
        assert removed.ok and removed.structured["removed"]
        # Source rows remain intact; only the owned recovery copy changes.
        with store._transaction() as connection:
            saved = connection.execute("SELECT payload_json FROM bunshin_v2_submission_drafts WHERE draft_key = ?",
                                       (first.draft_key,)).fetchone()
            saved_work = connection.execute("SELECT payload_json FROM bunshin_v2_submission_drafts WHERE draft_key = ?",
                                            (replace(first, draft_kind="work_items").draft_key,)).fetchone()
        assert json.loads(saved[0]) == dict(source.payload)
        assert json.loads(saved_work[0]) == dict(source_work.payload)
    asyncio.run(scenario())


@pytest.fixture
def continued(lifecycle):
    async def scenario():
        first, workspace, assignment, _ = await lifecycle.admit()
        authored = edit_finding_tool_result(new_tool_call(name="op_bunshin_update_finding", call_id="source-concern",
            args={"finding_id": "current-defect", "expected_revision": 0, "finding_kind": "module_defect",
                  "priority": "p1", "summary": "Unsubmitted current concern"}), workspace)
        assert authored.ok, authored.llm_text
        await lifecycle.pause_resume()
        second, workspace, replacement, _ = await lifecycle.admit()
        assert first.input_fingerprint == second.input_fingerprint
        return first, assignment, second, workspace, replacement
    return asyncio.run(scenario())


def set_prior_field(lifecycle, assignment, field, value):
    # Negative tests inject mismatched/corrupted stored metadata only after the
    # supported lifecycle produced both assignments. The positive path never does.
    allowed = {"session_id", "workflow_id", "aggregate_type", "aggregate_id", "role", "mode",
               "input_fingerprint", "submission_kind", "input_refs_json", "execution_spec_json"}
    assert field in allowed
    with lifecycle.repo.database.write_connection() as connection:
        connection.execute(f"UPDATE bunshin_v2_role_assignments SET {field} = ? WHERE assignment_id = ?",
                           (value, assignment["assignment_id"]))


@pytest.mark.parametrize("field,value", [
    ("session_id", "foreign-session"), ("workflow_id", "foreign-workflow"),
    ("aggregate_type", AggregateType.STANDALONE_REVIEW.value), ("aggregate_id", "foreign-node"),
    ("role", "reviewer"), ("mode", "standalone"), ("input_fingerprint", "different-input"),
    ("submission_kind", "standalone_review"),
])
def test_foreign_scalar_identity_remains_protected_history(lifecycle, continued, field, value):
    first, prior, second, _, _ = continued
    if field == "session_id":
        lifecycle.repo.role_sessions.ensure_role_session(session_id=value, workflow_id=lifecycle.workflow,
            aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=lifecycle.fx._node_id("backup_cli"),
            role="verifier", mode="module", role_profile_id="software_engineering.v2_verifier",
            family_binding_sha=lifecycle.binding.sha256, scope_kind="module", subject_key="backup_cli")
    set_prior_field(lifecycle, prior, field, value)
    snapshot = SubmissionDraftStore(lifecycle.fx.root).read(replace(second, draft_kind="work_items"))
    assert not [item for item in snapshot.payload["items"] if item["kind"] == "finding"]
    assert "current-defect" in json.dumps(snapshot.payload["history"])


@pytest.mark.parametrize("changed", ["candidate_diff", "verification_policy", "unknown_semantic_input"])
def test_semantic_ref_mismatch_stays_history_even_with_equal_fingerprint(lifecycle, continued, changed):
    first, prior, second, _, replacement = continued
    assert prior["input_fingerprint"] == replacement["input_fingerprint"]
    references = dict(prior["input_refs"])
    references[changed] = lifecycle.artifacts.put_json({"changed": changed}, artifact_type="ChangedSemanticInputArtifact").to_dict()
    set_prior_field(lifecycle, prior, "input_refs_json", json.dumps(references))
    snapshot = SubmissionDraftStore(lifecycle.fx.root).read(replace(second, draft_kind="work_items"))
    assert not [item for item in snapshot.payload["items"] if item["kind"] == "finding"]
    assert "current-defect" in json.dumps(snapshot.payload["history"])


def test_json_formatting_and_missing_legacy_generation_do_not_change_identity(lifecycle, continued):
    _, prior, second, workspace, _ = continued
    references = dict(reversed(list(prior["input_refs"].items())))
    set_prior_field(lifecycle, prior, "input_refs_json", json.dumps(references, indent=3))
    execution = dict(prior["execution_spec"])
    execution.pop("evaluation_generation")
    set_prior_field(lifecycle, prior, "execution_spec_json", json.dumps(execution))
    assert [item["finding_id"] for item in findings_from_work_items(workspace)] == ["current-defect"]


def test_changed_evaluation_generation_stays_history(lifecycle, continued):
    _, prior, second, _, _ = continued
    set_prior_field(lifecycle, prior, "execution_spec_json", json.dumps({**prior["execution_spec"], "evaluation_generation": 1}))
    snapshot = SubmissionDraftStore(lifecycle.fx.root).read(replace(second, draft_kind="work_items"))
    assert not [item for item in snapshot.payload["items"] if item["kind"] == "finding"]
    assert "current-defect" in json.dumps(snapshot.payload["history"])


@pytest.mark.parametrize("field,value", [
    ("input_refs_json", "not json"), ("input_refs_json", "null"), ("input_refs_json", "[]"),
    ("input_refs_json", '{"candidate_diff": "not an object"}'),
    ("input_refs_json", '{"candidate_diff": [["sha256", "x"]]}'),
    ("input_refs_json", '{"workspace_preparation": null}'),
    ("execution_spec_json", "not json"), ("execution_spec_json", "null"), ("execution_spec_json", "[]"),
    *[("execution_spec_json", json.dumps({"evaluation_generation": value})) for value in (True, "0", None, -1, 0.5)],
])
def test_malformed_stored_identity_blocks_and_rolls_back_inheritance(lifecycle, continued, field, value):
    first, prior, second, _, _ = continued
    set_prior_field(lifecycle, prior, field, value)
    target = replace(second, draft_kind="work_items")
    with pytest.raises(SubmissionInvariantError, match="source identity is malformed"):
        SubmissionDraftStore(lifecycle.fx.root).read(target)
    with lifecycle.repo.database.read_connection() as connection:
        assert connection.execute("SELECT 1 FROM bunshin_v2_submission_drafts WHERE draft_key = ?", (target.draft_key,)).fetchone() is None
        source = connection.execute("SELECT payload_json FROM bunshin_v2_submission_drafts WHERE draft_key = ?",
                                    (replace(first, draft_kind="work_items").draft_key,)).fetchone()
    assert "current-defect" in source[0]


def test_local_submitted_receipt_remains_history_despite_preparation_only_rebind(lifecycle):
    async def scenario():
        first, workspace, _, _ = await lifecycle.admit()
        created = edit_finding_tool_result(new_tool_call(name="op_bunshin_update_finding", call_id="frozen-concern",
            args={"finding_id": "frozen-defect", "expected_revision": 0, "finding_kind": "module_defect",
                  "priority": "p1", "summary": "Immutable submitted concern"}), workspace)
        assert created.ok, created.llm_text
        store = SubmissionDraftStore(lifecycle.fx.root)
        draft = store.read(first)
        receipt = lifecycle.artifacts.put_json({"findings": findings_from_work_items(workspace)}, artifact_type="VerifierRoleSubmissionArtifact")
        store.mark_submitted(first, expected_version=draft.version, submission_artifact_ref=receipt.to_dict(), submission_payload_hash=receipt.sha256)
        frozen = store.read_submitted(first.draft_key)
        await lifecycle.pause_resume()
        second, resumed, _, _ = await lifecycle.admit()
        assert first.input_fingerprint == second.input_fingerprint
        inherited = store.read(replace(second, draft_kind="work_items"))
        assert not findings_from_work_items(resumed)
        assert "frozen-defect" in json.dumps(inherited.payload["history"])
        assert store.read_submitted(first.draft_key) == frozen
        assert lifecycle.artifacts.read_json(receipt)["findings"][0]["finding_id"] == "frozen-defect"
    asyncio.run(scenario())
