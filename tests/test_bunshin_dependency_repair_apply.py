"""Offline atomic projection and immutable-receipt replay for repair cohorts."""
from dataclasses import replace
from unittest.mock import patch

import pytest

from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, SubmissionInvariantError
from pal.bunshin.cycle_protocol import NodeCycleState
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.semantic_orchestration.dependency_repair_apply import apply_dependency_repair_cohort
from pal.bunshin.storage.transitions import TransitionsStore
from pal.bunshin.verification import repair_bill_semantic_view
from tests.test_bunshin_dependency_repair_protocol import _execution, _member, _intent, _closure


class Case:
    def __init__(self, root, *, peer_status="FAIL", stale_provider=False, join_peer=False):
        self.repository = BunshinRepository(root)
        self.artifacts = ContentAddressedArtifactStore(root, self.repository.artifacts)
        execution = _execution()
        if stale_provider:
            execution = replace(execution, cycles={**execution.cycles, "P": replace(execution.cycles["P"],
                state=NodeCycleState.STALE, accepted_product_ref="")})
        self.candidates, self.pendings, self.packets, self.captures = {}, {}, {}, {}
        for name in execution.cycles:
            self.candidates[name] = self.put({"candidate_digest": f"tree-{name}"}, "CandidateArtifact")
        self.old = self.packet("P", "old-own-P")
        self.producer_old = self.packet("X", "old-own-X")
        for name in ("C", "D", "X"):
            member = _member(name)
            candidate = self.candidates[name]
            pending, report, packet = {}, {}, {}
            if name != "X":
                pending = self.put({"candidate_ref": candidate, "role_assignment_id": member.role_assignment_id,
                                    "candidate_digest": f"tree-{name}"}, "PendingSemanticVerificationArtifact")
                self.pendings[name] = pending
                report = self.put({"status": "FAIL" if name == "C" else peer_status,
                                   "source_pending_verification_ref": pending}, "VerificationArtifact")
                if name == "C":
                    packet = self.packet(name, "source-provider-defect", targets=["P"], mixed=True)
                elif peer_status == "FAIL":
                    packet = self.packet(name, "peer-local-D", targets=["Q"] if join_peer else None)
                self.packets[name] = packet
            captured = self.put({"candidate_digest": f"preserved-{name}"}, "CandidateArtifact")
            capture = {"schema_version": "1", "node_name": name, "node_run_id": member.aggregate_id,
                       "workflow_id": "workflow", "incarnation_key": member.key, "slot": member.slot,
                       "source_assignment_id": member.role_assignment_id,
                       "source_payload_hash": f"submission-{name}", "source_pending_ref": pending,
                       "finding_fingerprint": f"finding-fingerprint-{name}",
                       "source_candidate_ref": candidate, "source_candidate_digest": f"tree-{name}",
                       "candidate_ref": captured, "candidate_digest": f"preserved-{name}",
                       "report_ref": report, "repair_packet_ref": packet,
                       "target_modules": ["P"] if name == "C" else ["Q"] if name == "D" and join_peer else [], "routing_errors": [],
                       "status": "FAIL" if name == "C" else peer_status if name == "D" else "producer_preserved",
                       "invocation_id": f"owner-{name}", "lease_resource_key": member.lease_resource,
                       "fencing_token": member.fencing_token,
                       "defect_kind": "dependency_defect" if name == "C" or (name == "D" and join_peer) else "module_defect",
                       "historical_repair_bill_refs": [],
                       "repair_bill_ref": self.producer_old if name == "X" else {}}
            self.captures[member.key] = self.put(capture, "DependencyRepairCaptureArtifact")
        intent = replace(_intent(execution), candidate_ref=self.candidates["C"],
                         packet_ref=self.packets["C"], pending_verification_ref=self.pendings["C"])
        pending = execution.register_dependency_repair(intent, frontier=tuple(_member(n) for n in ("C", "D", "X")))
        if join_peer:
            peer = replace(_intent(execution, source="D", providers=("Q",)),
                           candidate_ref=self.candidates["D"], packet_ref=self.packets["D"],
                           pending_verification_ref=self.pendings["D"])
            pending = pending.register_dependency_repair(peer)
        self.repository.cycles.store_graph_generation(workflow_id="workflow", graph=pending.graph)
        self.repository.cycles.store_graph_execution(workflow_id="workflow", execution=pending)
        self.unclosed = pending
        for member in pending.dependency_repairs.pending.frontier.values():
            refs = [self.captures[member.key]]
            if member.node_name == "C" or (member.node_name == "D" and join_peer):
                refs.append(self.packets[member.node_name])
            pending = pending.close_dependency_repair(_closure(member, *refs))
        self.execution = pending
        self.repository.cycles.store_graph_execution(workflow_id="workflow", execution=pending)
        self.key = pending.dependency_repairs.pending.key
        with self.repository.database.write_connection() as connection:
            for name in pending.cycles:
                state = "STALE" if name in {"C", "D", "X"} or (name == "P" and stale_provider) else "ACCEPTED"
                payload = {"module_name": name, "unit_id": name, "epoch_id": "epoch", "graph_generation": 1,
                           "candidate_ref": self.candidates[name], "candidate_digest": f"tree-{name}",
                           "historical_repair_bill_refs": [], "repair_bill_ref": self.old if name == "P" else {},
                           "pending_verification_ref": self.pendings.get(name, {})}
                node = AggregateSnapshot(AggregateType.DAG_NODE_RUN, f"aggregate-{name}", "workflow", state, 1, payload, "", "")
                self.repository.snapshots.write_snapshot_locked(connection, None, node)

    def put(self, value, kind):
        return self.artifacts.put_json(value, artifact_type=kind).to_dict()

    def packet(self, name, finding_id, targets=None, mixed=False):
        targets = targets or [name]
        findings = [{"finding_id": finding_id, "summary": finding_id, "finding_kind": "dependency_defect" if targets != [name] else "module_defect"}]
        owners = {finding_id: targets}
        if mixed:
            findings.append({"finding_id": "source-local-C", "summary": "source-local-C", "finding_kind": "module_defect"})
            owners["source-local-C"] = [name]
        return self.put({"artifact_kind": "semantic_repair_packet", "module_name": name,
                         "findings": findings, "finding_targets": owners, "target_modules": targets,
                         "changed_test_paths": [f"tests/{name}.py"]}, "RepairPacketArtifact")

    def apply(self, **kwargs):
        return apply_dependency_repair_cohort(repository=self.repository, artifacts=self.artifacts,
                                             graph_execution=self.execution, cohort_key=self.key,
                                             capture_refs=self.captures, **kwargs)

    def node(self, name):
        return self.repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, f"aggregate-{name}")

    def graph(self):
        return self.repository.cycles.read_graph_execution(workflow_id="workflow")


@pytest.mark.parametrize("stale_provider", [False, True])
def test_apply_projects_all_participants_and_preserves_owned_findings(tmp_path, stale_provider):
    case = Case(tmp_path, stale_provider=stale_provider)
    result = case.apply()
    assert result.dependency_repairs.pending is None
    assert result.dependency_repairs.history[-1].status == "applied"
    assert case.node("P").state == "REPAIR_QUEUED"
    assert {case.node(name).state for name in ("Q", "C", "D", "X", "S")} == {"STALE"}
    assert case.node("U").version == case.node("V").version == 1
    view = repair_bill_semantic_view(case.artifacts, case.node("P").payload["repair_bill_ref"], module_name="P")
    assert {finding["summary"] for finding in view["findings"]} == {"old-own-P", "source-provider-defect"}
    assert {finding["summary"] for finding in view["related_findings"]} == {"source-local-C"}
    for name, packet in (("P", case.old), ("C", case.packets["C"]), ("D", case.packets["D"]), ("X", case.producer_old)):
        history = case.node(name).payload["historical_repair_bill_refs"]
        assert packet in history
    for name in ("C", "D", "X"):
        node = case.node(name)
        history = [case.artifacts.read_json(ref) for ref in node.payload["historical_repair_bill_refs"]]
        assert any(item.get("candidate_digest") == f"preserved-{name}" for item in history)
        assert node.payload["candidate_digest"] == f"preserved-{name}"


@pytest.mark.parametrize("peer_status", ["PASS", "FAIL", "UNKNOWN"])
def test_reports_receive_invalidated_receipts_never_old_binding_pass(tmp_path, peer_status):
    case = Case(tmp_path, peer_status=peer_status)
    case.apply()
    for name in ("C", "D"):
        ref = case.repository.queries.read_verification_settlement_ref(f"aggregate-{name}", case.pendings[name]["sha256"])
        receipt = case.artifacts.read_json(ref)
        assert receipt["status"] == receipt["settlement_status"] == "invalidated"
        assert receipt["source_pending_verification_ref"] == case.pendings[name]
        capture = case.artifacts.read_json(case.captures[_member(name).key])
        assert receipt["verification_artifact_ref"] == capture["report_ref"]
    assert case.node("D").state == "STALE"


def test_replay_uses_ledger_and_original_pending_receipts_after_new_current_bill(tmp_path):
    case = Case(tmp_path)
    case.apply()
    newer = case.packet("P", "later-provider-bill")
    with case.repository.database.write_connection() as connection:
        for name in ("P", "C", "D"):
            old = case.node(name)
            changed = replace(old, version=old.version + 1, payload={**old.payload, "repair_bill_ref": newer,
                                                                    "pending_verification_ref": {}})
            case.repository.snapshots.write_snapshot_locked(connection, old, changed)
    versions = {name: case.node(name).version for name in ("P", "C", "D")}
    case.apply()
    assert versions == {name: case.node(name).version for name in versions}
    assert case.node("P").payload["repair_bill_ref"] == newer


def test_atomic_rollback_after_partial_aggregate_projection(tmp_path):
    case = Case(tmp_path)
    dispatch = TransitionsStore.dispatch

    def fail_last(store, action, **kwargs):
        if action.aggregate_id == "aggregate-X" and action.action_type == "SETTLE_DEPENDENCY_REPAIR":
            raise RuntimeError("injected atomic projection failure")
        return dispatch(store, action, **kwargs)

    with patch.object(TransitionsStore, "dispatch", fail_last), pytest.raises(RuntimeError, match="injected"):
        case.apply()
    assert case.graph().dependency_repairs.pending.key == case.key
    assert all(case.node(name).version == 1 for name in case.execution.cycles)
    assert not case.repository.queries.read_verification_settlement_ref("aggregate-C", case.pendings["C"]["sha256"])
    case.apply()
    assert case.node("P").state == "REPAIR_QUEUED"


def test_rejects_unclosed_or_replaced_capture_without_projection(tmp_path):
    case = Case(tmp_path)
    wrong = case.put({"incarnation_key": "wrong"}, "DependencyRepairCaptureArtifact")
    case.captures[_member("D").key] = wrong
    with pytest.raises(SubmissionInvariantError, match="exact closure receipt"):
        case.apply()
    assert case.graph().dependency_repairs.pending.key == case.key
    assert case.node("P").version == 1


def test_registration_and_capture_are_not_final_verdict_events(tmp_path):
    case = Case(tmp_path)
    node = case.node("C")
    with case.repository.database.write_connection() as connection:
        reviewing = replace(node, state="REVIEW_SNAPSHOTTING")
        case.repository.snapshots.write_snapshot_locked(connection, node, reviewing)
    incarnation = case.put({"incarnation": _member("C").to_dict()}, "DependencyRepairIncarnationArtifact")
    payload = {"dependency_repair_incarnation_ref": incarnation,
               "dependency_repair_capture_ref": case.captures[_member("C").key],
               "dependency_repair_source_pending_ref": case.pendings["C"]}
    result = case.repository.transitions.dispatch(ActionEnvelope(
        action_type="REGISTER_DEPENDENCY_REPAIR", workflow_id="workflow", aggregate_type=AggregateType.DAG_NODE_RUN,
        aggregate_id="aggregate-C", actor="manager", payload=payload))
    assert result.snapshot.state == "REVIEW_SNAPSHOTTING"
    assert len(result.outbox_effect_ids) == 1
    import json
    with case.repository.database.read_connection() as connection:
        row = connection.execute("SELECT payload_json FROM bunshin_v2_outbox WHERE effect_id = ?",
                                 (result.outbox_effect_ids[0],)).fetchone()
    effect_payload = json.loads(row["payload_json"])
    assert effect_payload["graph_generation"] == 1
    assert effect_payload["dependency_repair_capture_ref"] == payload["dependency_repair_capture_ref"]
    assert effect_payload["dependency_repair_incarnation_ref"] == incarnation
    assert "source_pending_verification_ref" not in effect_payload
    assert "dependency_repair_source_pending_ref" not in effect_payload
    assert not case.repository.queries.read_verification_settlement_ref("aggregate-C", case.pendings["C"]["sha256"])


def test_unclosed_graph_cannot_publish_repairs(tmp_path):
    case = Case(tmp_path)
    # An older caller cannot certify a current frontier that is still open.
    from pal.bunshin.storage.serialization import _json
    from pal.bunshin.storage.serialization import _cycle_payload
    with case.repository.database.write_connection() as connection:
        connection.execute("UPDATE bunshin_v2_graph_generations SET execution_json = ? WHERE graph_id = ?", (
            _json({"state": "RUNNING", "dependency_repairs": case.unclosed.dependency_repairs.to_dict()}), "graph"))
        for cycle in case.unclosed.cycles.values():
            connection.execute("UPDATE bunshin_v2_node_cycles SET state = ?, payload_json = ? WHERE cycle_id = ?",
                               (cycle.state.value, _json(_cycle_payload(cycle)), cycle.cycle_id))
    with pytest.raises(SubmissionInvariantError, match="no exact closure"):
        case.apply()
    assert case.node("P").version == 1


def test_applied_replay_requires_exact_source_settlement_not_current_packet(tmp_path):
    case = Case(tmp_path)
    case.apply()
    with case.repository.database.write_connection() as connection:
        connection.execute("DELETE FROM bunshin_v2_domain_events WHERE aggregate_id = ? AND event_type = ?",
                           ("aggregate-C", "dag_node_run.settle_dependency_repair"))
    with pytest.raises(SubmissionInvariantError, match="lost its exact settlement receipt"):
        case.apply()


def test_correction_findings_remain_preserved_without_implementation_ownership(tmp_path):
    case = Case(tmp_path)
    correction = case.put({"artifact_kind": "semantic_repair_packet", "module_name": "P",
                           "classification": "invalid_verifier_submission", "route": "verification_correction",
                           "findings": [{"finding_id": "invalid-scope", "summary": "invalid-scope"}],
                           "finding_targets": {}, "target_modules": []}, "RepairPacketArtifact")
    node = case.node("P")
    with case.repository.database.write_connection() as connection:
        case.repository.snapshots.write_snapshot_locked(connection, node,
            replace(node, payload={**node.payload, "historical_repair_bill_refs": [correction]}))
    case.apply()
    provider = case.node("P")
    assert correction in provider.payload["historical_repair_bill_refs"]
    view = repair_bill_semantic_view(case.artifacts, provider.payload["repair_bill_ref"], module_name="P")
    assert "invalid-scope" not in {finding["summary"] for finding in view["findings"]}
    assert "invalid-scope" in {finding["summary"] for finding in view["related_findings"]}


def test_joined_cohort_reopens_all_and_only_validated_providers(tmp_path):
    case = Case(tmp_path, join_peer=True)
    case.apply()
    for provider, expected in (("P", {"old-own-P", "source-provider-defect"}), ("Q", {"peer-local-D"})):
        node = case.node(provider)
        assert node.state == "REPAIR_QUEUED"
        view = repair_bill_semantic_view(case.artifacts, node.payload["repair_bill_ref"], module_name=provider)
        assert {finding["summary"] for finding in view["findings"]} == expected
    assert case.node("V").state == "ACCEPTED"
    assert case.node("V").version == 1


def test_unfrozen_active_consumer_cannot_be_marked_stale(tmp_path):
    case = Case(tmp_path)
    node = case.node("Q")
    with case.repository.database.write_connection() as connection:
        case.repository.snapshots.write_snapshot_locked(connection, node, replace(node, state="PRODUCING"))
    with pytest.raises(SubmissionInvariantError, match="omitted active aggregate Q"):
        case.apply()
    assert case.node("P").version == 1
    assert case.graph().dependency_repairs.pending is not None


def test_user_cancellation_dominates_repair_projection(tmp_path):
    case = Case(tmp_path)
    node = case.node("D")
    with case.repository.database.write_connection() as connection:
        case.repository.snapshots.write_snapshot_locked(connection, node,
            replace(node, state="CANCEL_REQUESTED", payload={**node.payload, "cancel_target": "CANCELLED"}))
    with pytest.raises(SubmissionInvariantError, match="user cancellation"):
        case.apply()
    assert case.node("P").version == 1


def test_preparation_rejects_final_receipt_keys(tmp_path):
    from pal.bunshin.contracts import TransitionGuardError
    case = Case(tmp_path)
    with pytest.raises(TransitionGuardError, match="cannot publish a final verification settlement"):
        case.repository.transitions.dispatch(ActionEnvelope(
            action_type="CAPTURE_DEPENDENCY_REPAIR", workflow_id="workflow", aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="aggregate-C", actor="manager", payload={
                "dependency_repair_capture_ref": case.captures[_member("C").key],
                "source_pending_verification_ref": case.pendings["C"],
                "verification_artifact_ref": case.packets["C"],
            }))


def test_validated_intents_charge_failure_history_once_but_invalidated_peers_do_not(tmp_path):
    case = Case(tmp_path)
    case.apply()
    assert case.node("C").payload["failure_history"] == [{
        "finding_fingerprint": "finding-fingerprint-C", "candidate_tree_hash": "preserved-C",
    }]
    assert not case.node("D").payload.get("failure_history")
    assert not case.node("P").payload.get("failure_history")
    case.apply()
    assert len(case.node("C").payload["failure_history"]) == 1
