"""Public control recovery keeps the exact admitted dependency-repair frontier.

All aggregate/cycle/artifact/outbox operations are real persisted operations.
Only physical role teardown is stubbed; no provider or subprocess is started.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pal.bunshin.v2.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, SubmissionInvariantError
from pal.bunshin.v2.cycle_protocol import NodeCycleState
from pal.bunshin.v2.graph_executor import GraphExecutionState
from pal.bunshin.v2.semantic_orchestration.dependency_repair_recovery import pending_repair_incarnation
from pal.bunshin.v2.semantic_orchestration.dependency_repair_runtime import DependencyRepairRuntime
from pal.bunshin.v2.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.v2.semantic_orchestration.node_control import NodeControl
from pal.bunshin.v2.service import BunshinV2WorkflowService
from pal.bunshin.v2.workflow_runtime import WorkflowCoordinator
from pal.bunshin.v2.workspace_resources import WorkspaceLockRegistry
from tests.test_bunshin_dependency_repair_apply import Case
from tests.test_bunshin_dependency_repair_protocol import _execution, _intent, _member


class RecoveryCase(Case):
    def __init__(self, root):
        self.service = BunshinV2WorkflowService(root)
        self.repository, self.artifacts = self.service.repository, self.service.artifacts
        self.coordinator = WorkflowCoordinator(self.repository)
        execution = _execution()
        self.candidates = {name: self.put({"candidate_digest": f"tree-{name}"}, "CandidateArtifact")
                           for name in execution.cycles}
        self.pendings, self.captures, self.packets = {}, {}, {}
        self.old, self.producer_old = self.packet("P", "old-own-P"), self.packet("X", "old-own-X")
        with self.repository.database.write_connection() as connection:
            for name in execution.cycles:
                payload = {"module_name": name, "unit_id": name, "epoch_id": "epoch", "graph_generation": 1,
                           "candidate_ref": self.candidates[name], "candidate_digest": f"tree-{name}",
                           "historical_repair_bill_refs": [], "repair_bill_ref": self.old if name == "P" else {}}
                node = AggregateSnapshot(AggregateType.DAG_NODE_RUN, f"aggregate-{name}", "workflow", "ACCEPTED", 1, payload, "", "")
                self.repository.snapshots.write_snapshot_locked(connection, None, node)
        self.originals = {}
        for name in ("C", "D", "X"):
            node = self.node(name)
            member = _member(name)
            workspace = root / f"workspace-{name}"
            workspace.mkdir()
            (workspace / "preserved.txt").write_text(f"original-{name}")
            if name != "X":
                self.pendings[name] = self.put({
                    "candidate_ref": self.candidates[name], "candidate_digest": f"tree-{name}",
                    "role_assignment_id": member.role_assignment_id,
                    "role_submission_payload_hash": f"submission-{name}",
                }, "PendingSemanticVerificationArtifact")
            payload = {**node.payload, "graph_generation": 1, "node_kind": "unit",
                       "workspace_path": str(workspace), "role_assignment_id": member.role_assignment_id,
                       "role_submission_payload_hash": f"submission-{name}",
                       "pending_verification_ref": self.pendings.get(name, {}),
                       "verification_correction_attempts": 2, "effect_failure_count": 3,
                       "failure_history": [{"finding_fingerprint": "earlier-finding", "candidate_tree_hash": "earlier-tree"}]}
            original = replace(node, state="PRODUCING" if name == "X" else "REVIEW_SNAPSHOTTING", payload=payload)
            self.originals[name] = original
            frozen = self.put({"schema_version": "1", "incarnation": member.to_dict(),
                               "workflow_id": node.workflow_id, "aggregate_id": node.aggregate_id,
                               "state": original.state, "version": original.version, "payload": payload,
                               "pending_verification_ref": self.pendings.get(name, {})},
                              "DependencyRepairIncarnationArtifact")
            packet = self.packet(name, "source-provider-defect", targets=["P"], mixed=True) if name == "C" else self.packet(name, f"local-{name}")
            self.packets[name] = packet
            capture = {"schema_version": "1", "node_name": name, "node_run_id": node.aggregate_id,
                       "workflow_id": "workflow", "incarnation_key": member.key, "slot": member.slot,
                       "source_assignment_id": member.role_assignment_id, "source_payload_hash": f"submission-{name}",
                       "source_pending_ref": self.pendings.get(name, {}),
                       "source_candidate_ref": self.candidates[name], "source_candidate_digest": f"tree-{name}",
                       "candidate_ref": self.candidates[name], "candidate_digest": f"tree-{name}",
                       "report_ref": self.put({"status": "FAIL", "source_pending_verification_ref": self.pendings[name]}, "VerificationArtifact") if name != "X" else {},
                       "repair_packet_ref": packet, "target_modules": ["P"] if name == "C" else [],
                       "defect_kind": "dependency_defect" if name == "C" else "module_defect",
                       "finding_fingerprint": f"finding-{name}",
                       "routing_errors": [], "status": "FAIL" if name != "X" else "producer_preserved",
                       "invocation_id": member.lease_owner, "lease_resource_key": member.lease_resource,
                       "fencing_token": member.fencing_token, "historical_repair_bill_refs": [],
                       "repair_bill_ref": self.producer_old if name == "X" else {}}
            self.captures[member.key] = self.put(capture, "DependencyRepairCaptureArtifact")
            self.write_node(name, state="REVIEW_SNAPSHOTTING" if name == "C" else "CANCEL_REQUESTED",
                            payload={**payload, "dependency_repair_incarnation_ref": frozen,
                                     "dependency_repair_capture_ref": self.captures[member.key],
                                     **({"cancel_target": "STALE"} if name != "C" else {})})
        intent = replace(_intent(execution), candidate_ref=self.candidates["C"], packet_ref=self.packets["C"],
                         pending_verification_ref=self.pendings["C"])
        execution = execution.register_dependency_repair(intent, frontier=tuple(_member(name) for name in ("C", "D", "X")))
        cohort = execution.dependency_repairs.pending
        for member in cohort.frontier.values():
            refs = (self.captures[member.key], self.packets["C"]) if member.node_name == "C" else (self.captures[member.key],)
            execution = execution.capture_dependency_repair(member.key, receipt_refs=refs)
        self.repository.cycles.store_graph_generation(workflow_id="workflow", graph=execution.graph)
        self.repository.cycles.store_graph_execution(workflow_id="workflow", execution=execution)
        self.key = execution.dependency_repairs.pending.key
        workflow = AggregateSnapshot(AggregateType.WORKFLOW, "workflow", "workflow", "ACTIVE", 1, {}, "", "")
        with self.repository.database.write_connection() as connection:
            self.repository.snapshots.write_snapshot_locked(connection, None, workflow)
        self.cleanup = SimpleNamespace(retire_incarnation=AsyncMock(return_value=""),
                                       release_managed_lsp_workspace=AsyncMock())
        self.locks = WorkspaceLockRegistry()
        self.runtime = DependencyRepairRuntime(self.artifacts, self.repository, EffectReads(self.repository),
            self.cleanup, SimpleNamespace(release_business_lease=Mock()), self.locks,
            SimpleNamespace(capture=Mock(side_effect=AssertionError("saved captures must be reused"))))
        self.admission = SimpleNamespace(admit_node_worker=Mock(side_effect=AssertionError("must not admit a new role")))
        self.control = NodeControl(effect_reads=EffectReads(self.repository), implementation_snapshot=None,
            node_admission=self.admission, role_cleanup=self.cleanup, verification_snapshot=None,
            artifacts=self.artifacts, repository=self.repository, workspace_locks=self.locks,
            dependency_repairs=self.runtime)

    def write_node(self, name, *, state=None, payload=None):
        node = self.node(name)
        updated = replace(node, state=state or node.state, version=node.version + 1,
                          payload=payload if payload is not None else node.payload)
        with self.repository.database.write_connection() as connection:
            self.repository.snapshots.write_snapshot_locked(connection, node, updated)
        return updated

    def triage(self, name="C"):
        node = self.node(name)
        with self.repository.transaction() as work:
            work.transitions.dispatch(ActionEnvelope(action_type="ENTER_TRIAGE", workflow_id="workflow",
                aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node.aggregate_id, actor="manager",
                expected_version=node.version, payload={"blocker": {"kind": "effect_failed"}}))
            self.coordinator.require_node_triage(workflow_id="workflow", node_name=name, unit_of_work=work)

    def resolve(self, name="C"):
        return self.service.resolve_triage(workflow_id="workflow", actor="operator", source_channel="test",
            resolution="Retry only the original dependency cleanup after inspecting the failure.", subject=f"module:{name}")

    def effect(self, name="C", kind="reconcile_semantic_state"):
        with self.repository.database.read_connection() as connection:
            row = connection.execute("SELECT * FROM bunshin_v2_outbox WHERE aggregate_id = ? AND effect_type = ? "
                "ORDER BY rowid DESC LIMIT 1", (f"aggregate-{name}", kind)).fetchone()
        assert row is not None
        effect = dict(row)
        effect["payload"] = json.loads(effect.pop("payload_json"))
        return effect

    def counts(self):
        with self.repository.database.read_connection() as connection:
            return tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in (
                "bunshin_v2_domain_events", "bunshin_v2_outbox", "bunshin_v2_role_assignments", "bunshin_v2_role_attempts"))


@pytest.mark.parametrize("name", ["C", "D", "X"])
def test_public_resolve_restores_exact_cursor_then_reconcile_applies(tmp_path, name):
    case = RecoveryCase(tmp_path)
    original = case.graph().cycles[name]
    case.triage(name)
    assert case.graph().cycles[name].active_assignment is None
    assert case.graph().cycles[name].state == NodeCycleState.TRIAGE_REQUIRED
    before = case.node(name)
    roles = case.counts()[-2:]
    result = case.resolve(name)
    resumed = case.graph().cycles[name]
    assert result["status"] == "triage_resolved"
    assert resumed.active_assignment.input_fingerprint == original.active_assignment.input_fingerprint
    assert resumed.active_assignment.slot == original.active_assignment.slot
    assert resumed.state == original.state
    assert resumed.resume_state == original.resume_state
    assert resumed.last_verdict == original.last_verdict
    assert case.node(name).payload["effect_failure_count"] == before.payload["effect_failure_count"]
    assert case.node(name).payload["verification_correction_attempts"] == before.payload["verification_correction_attempts"]
    assert case.node(name).payload["failure_history"] == before.payload["failure_history"]
    assert case.graph().dependency_repairs.pending.key == case.key
    assert case.counts()[-2:] == roles
    effect = case.effect(name)
    asyncio.run(case.control.reconcile_node(effect))
    assert case.graph().dependency_repairs.pending is None
    assert case.graph().dependency_repairs.history[-1].status == "applied"
    assert case.node("P").state == "REPAIR_QUEUED"
    assert all(case.node(member).state == "STALE" for member in ("C", "D", "X"))
    assert case.cleanup.retire_incarnation.await_count == 3
    case.admission.admit_node_worker.assert_not_called()
    assert case.counts()[-2:] == roles


@pytest.mark.parametrize("tamper", ["candidate", "digest", "pending", "capture", "incarnation", "role", "generation", "product"])
def test_public_resolve_rejects_changed_authority_atomically(tmp_path, tamper):
    case = RecoveryCase(tmp_path)
    case.triage()
    node = case.node("C")
    payload = dict(node.payload)
    if tamper == "product":
        execution = case.graph()
        execution = replace(execution, cycles={**execution.cycles, "C": replace(execution.cycles["C"], product_ref="replacement-product")})
        case.repository.cycles.store_graph_execution(workflow_id="workflow", execution=execution)
    elif tamper == "candidate":
        payload["candidate_ref"] = case.candidates["D"]
    elif tamper == "digest":
        payload["candidate_digest"] = "changed-tree"
    elif tamper == "pending":
        payload["pending_verification_ref"] = case.pendings["D"]
    elif tamper == "capture":
        payload["dependency_repair_capture_ref"] = case.captures[_member("D").key]
    elif tamper == "incarnation":
        payload["dependency_repair_incarnation_ref"] = case.node("D").payload["dependency_repair_incarnation_ref"]
    elif tamper == "role":
        payload["role_assignment_id"] = "replacement-role"
    elif tamper == "generation":
        payload["graph_generation"] = 2
    case.write_node("C", payload=payload)
    before, graph, counts = case.node("C"), case.graph(), case.counts()
    with pytest.raises((SubmissionInvariantError, ValueError)):
        case.resolve()
    assert case.node("C") == before
    assert case.graph() == graph
    assert case.counts() == counts
    case.cleanup.retire_incarnation.assert_not_called()


def test_public_resolve_requires_current_frontier_and_keeps_cancel_dominant(tmp_path):
    case = RecoveryCase(tmp_path)
    case.triage()
    case.service.control_workflow(workflow_id="workflow", command="cancel", actor="operator", source_channel="test")
    graph = case.graph()
    assert graph.state == GraphExecutionState.CANCELLED
    assert graph.dependency_repairs.pending is None
    assert graph.dependency_repairs.history[-1].status == "superseded"
    before, counts = case.node("C"), case.counts()
    with pytest.raises(SubmissionInvariantError, match="terminal graph control"):
        case.resolve()
    assert case.node("C") == before
    assert case.counts() == counts
    assert case.graph() == graph


def test_coordinator_rejects_unvalidated_generic_pending_cursor(tmp_path):
    case = RecoveryCase(tmp_path)
    case.triage()
    before = case.graph()
    with pytest.raises(ValueError, match="frozen graph cursor"):
        case.coordinator.resolve_triage(workflow_id="workflow", node_name="C",
            pending_checker_generation=1, pending_checker_input_fingerprint="pending-checker-settlement:wrong")
    assert case.graph() == before


@pytest.mark.parametrize("name", ["C", "D"])
def test_public_resolve_cannot_waive_terminal_no_progress_receipt(tmp_path, name):
    case = RecoveryCase(tmp_path)
    case.triage(name)
    node = case.node(name)
    capture = case.artifacts.read_json(case.captures[_member(name).key])
    case.write_node(name, payload={**node.payload, "blocker": {"kind": "no_progress"},
                                  "verification_artifact_ref": capture["report_ref"]})
    before, graph, counts = case.node(name), case.graph(), case.counts()
    with pytest.raises(SubmissionInvariantError, match="no-progress|committed verdict"):
        case.resolve(name)
    assert case.node(name) == before
    assert case.graph() == graph
    assert case.counts() == counts


def test_terminal_no_progress_receipt_survives_later_cleanup_blocker(tmp_path):
    case = RecoveryCase(tmp_path)
    case.triage("D")
    capture = case.artifacts.read_json(case.captures[_member("D").key])
    for payload in (
        {"blocker": {"kind": "no_progress"}, "verification_artifact_ref": capture["report_ref"],
         "source_pending_verification_ref": case.pendings["D"]},
        {"blocker": {"kind": "effect_failed"}},
    ):
        node = case.node("D")
        case.repository.transitions.dispatch(ActionEnvelope(action_type="ENTER_TRIAGE", workflow_id="workflow",
            aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node.aggregate_id, actor="manager",
            expected_version=node.version, payload=payload))
    before, graph, counts = case.node("D"), case.graph(), case.counts()
    with pytest.raises(SubmissionInvariantError, match="committed verdict"):
        case.resolve("D")
    assert case.node("D") == before
    assert case.graph() == graph
    assert case.counts() == counts


def test_pending_pause_resume_preserves_frozen_graph_scope(tmp_path):
    case = RecoveryCase(tmp_path)
    before = case.graph()
    case.service.control_workflow(workflow_id="workflow", command="pause", actor="operator", source_channel="test")
    for name in before.pending_scope:
        assert case.graph().cycles[name] == before.cycles[name]
    for name in ("C", "D", "X"):
        case.coordinator.confirm_node_control(workflow_id="workflow", node_name=name, cancel=name != "C")
    case.coordinator.resume_workflow(workflow_id="workflow")
    for name in before.pending_scope:
        assert case.graph().cycles[name] == before.cycles[name]
    assert case.graph().dependency_repairs == before.dependency_repairs
    assert pending_repair_incarnation(case.repository, case.artifacts, case.node("C")) == _member("C")
    case.admission.admit_node_worker.assert_not_called()


@pytest.fixture
def public_case():
    from tests.test_bunshin_dependency_repair_runtime import RuntimeCase
    value = RuntimeCase()
    try:
        yield value
    finally:
        value.close()


def _exhaust_on_next_claim(case, name="archive_verify"):
    effect = case.stored(name, "reconcile_dependency_repairs")
    with case.repository.database.write_connection() as connection:
        connection.execute("UPDATE bunshin_v2_outbox SET max_attempts = 1 WHERE effect_id = ?", (effect["effect_id"],))
    return case.claim(name, "reconcile_dependency_repairs")


def test_public_node_resolution_recovers_real_exhausted_cleanup(public_case):
    case = public_case

    async def run():
        await case.start_checker("archive_verify")
        await case.start_checker("checksum_verify")
        await case.prepare_source()
        frozen = case.graph().cycles["archive_verify"]
        original = _exhaust_on_next_claim(case)
        cleanup = case.worker.components.node_control.dependency_repairs.cleanup
        with patch.object(cleanup, "retire_incarnation", side_effect=RuntimeError("cleanup failed before closure")):
            assert await case.processor._process_effect(original) == "failed"
        assert case.node().state == "TRIAGE_REQUIRED"
        assert case.graph().cycles["archive_verify"].active_assignment is None
        attempts = case.repository.outbox_claims.list_effect_attempts(original["effect_id"])
        case.service.resolve_triage(workflow_id=case.workflow_id, actor="operator", source_channel="test",
            subject="module:archive_verify", resolution="Original worker cleanup is now available; resume the same receipt.")
        resumed = case.graph().cycles["archive_verify"]
        assert resumed.active_assignment.input_fingerprint == frozen.active_assignment.input_fingerprint
        assert resumed.active_assignment.slot == frozen.active_assignment.slot
        await case.process("archive_verify", "reconcile_semantic_state")
        assert case.graph().dependency_repairs.pending is None
        assert case.node("manifest_model").state == "REPAIR_QUEUED"
        assert case.repository.outbox_claims.list_effect_attempts(original["effect_id"]) == attempts
        assert [attempt["status"] for attempt in attempts] == ["failed"]
    asyncio.run(run())


def test_workflow_resolution_restarts_exhausted_apply_after_all_frontier_closed(public_case):
    case = public_case

    async def run():
        await case.start_checker("archive_verify")
        await case.start_checker("checksum_verify")
        case.submit("checksum_verify", dependency=False)
        await case.prepare_source()
        # RuntimeCase exercises real role/outbox work but normally omits the
        # outer epoch. Workflow-level recovery needs its ordinary lineage.
        epoch = AggregateSnapshot(AggregateType.EXECUTION_EPOCH, "epoch-scope", case.workflow_id,
                                  "RUNNING", 1, {"graph_generation": 1}, "", "")
        with case.repository.database.write_connection() as connection:
            case.repository.snapshots.write_snapshot_locked(connection, None, epoch)
        workflow = case.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, case.workflow_id)
        case.repository.transitions.dispatch(ActionEnvelope(action_type="LINK_EXECUTION_EPOCH", workflow_id=case.workflow_id,
            aggregate_type=AggregateType.WORKFLOW, aggregate_id=case.workflow_id, actor="fixture",
            expected_version=workflow.version, payload={"execution_epoch_id": "epoch-scope"}))
        original = _exhaust_on_next_claim(case)
        with patch("pal.bunshin.v2.semantic_orchestration.dependency_repair_apply.apply_dependency_repair_cohort",
                   side_effect=RuntimeError("apply failed after exact frontier closure")):
            assert await case.processor._process_effect(original) == "failed"
        cohort = case.graph().dependency_repairs.pending
        assert cohort.ready
        assert case.node().state == case.node("checksum_verify").state == "STALE"
        workflow = case.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, case.workflow_id)
        assert workflow.state == "TRIAGE_REQUIRED"
        assert case.node("manifest_model").state == "ACCEPTED"
        attempts = case.repository.outbox_claims.list_effect_attempts(original["effect_id"])
        case.service.resolve_triage(workflow_id=case.workflow_id, actor="operator", source_channel="test",
            subject="phase:workflow", resolution="Resume the preserved, closed repair cohort after fixing apply.")
        with case.repository.database.read_connection() as connection:
            row = connection.execute("SELECT effect_id FROM bunshin_v2_outbox WHERE aggregate_id = ? "
                "AND effect_type = 'reconcile_workflow' ORDER BY rowid DESC LIMIT 1", (case.workflow_id,)).fetchone()
        assert row is not None
        claimed = case.repository.outbox_claims.claim_outbox(case.processor.worker_id, limit=1000, lease_seconds=120)
        effect = next(item for item in claimed if item["effect_id"] == row["effect_id"])
        assert effect["effect_id"] != original["effect_id"]
        result = await case.processor._process_effect(effect)
        with case.repository.database.read_connection() as connection:
            outcome = dict(connection.execute("SELECT * FROM bunshin_v2_outbox WHERE effect_id = ?", (effect["effect_id"],)).fetchone())
        assert result == "completed", outcome["last_error"]
        assert case.graph().dependency_repairs.pending is None
        assert case.graph().dependency_repairs.history[-1].key == cohort.key
        assert case.node("manifest_model").state == "REPAIR_QUEUED"
        assert case.repository.outbox_claims.list_effect_attempts(original["effect_id"]) == attempts
        assert [attempt["status"] for attempt in attempts] == ["failed"]
        case.assert_receipt(effect)
    asyncio.run(run())


def test_public_pause_cleans_only_then_resume_applies_same_frozen_cohort(public_case):
    case = public_case

    async def run():
        await case.start_checker("archive_verify")
        await case.start_checker("checksum_verify")
        case.submit("checksum_verify", dependency=False)
        await case.prepare_source()
        before = case.graph()
        peer_path = case.workspaces["checksum_verify"] / "tests/checksum_verify/verifier/test_contract.py"
        peer_bytes = peer_path.read_bytes()
        provider = case.node("manifest_model")
        case.service.control_workflow(workflow_id=case.workflow_id, command="pause", actor="operator", source_channel="test")
        case.dispatch("archive_verify", "REQUEST_PAUSE")
        await case.process("archive_verify", "pause_role")
        await case.process("checksum_verify", "cancel_role")
        assert case.node().state == "PAUSED"
        assert case.node("checksum_verify").state == "STALE"
        assert case.graph().dependency_repairs == before.dependency_repairs
        assert all(case.graph().cycles[name] == before.cycles[name] for name in before.pending_scope)
        assert peer_path.read_bytes() == peer_bytes
        assert case.node("manifest_model") == provider
        original = case.claim("archive_verify", "reconcile_dependency_repairs")
        assert await case.processor._process_effect(original) == "deferred"
        assert case.graph().dependency_repairs == before.dependency_repairs
        assert case.node("manifest_model") == provider
        workflow = case.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, case.workflow_id)
        if workflow.state == "PAUSE_REQUESTED":
            case.repository.transitions.dispatch(ActionEnvelope(action_type="CHILDREN_PAUSED", workflow_id=case.workflow_id,
                aggregate_type=AggregateType.WORKFLOW, aggregate_id=case.workflow_id, actor="control",
                expected_version=workflow.version))
        case.service.resume_workflow(workflow_id=case.workflow_id, actor="operator", source_channel="test")
        case.dispatch("archive_verify", "RESUME")
        await case.process("archive_verify", "resume_semantic_state")
        assert case.graph().dependency_repairs.pending is None
        assert case.graph().dependency_repairs.history[-1].key == before.dependency_repairs.pending.key
        assert case.node("manifest_model").state == "REPAIR_QUEUED"
        assert peer_path.read_bytes() == peer_bytes
    asyncio.run(run())
