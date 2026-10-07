"""Keep validated dependency reports on the existing no-progress budget."""
from __future__ import annotations

from typing import Any, Mapping

from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.bunshin.cycle_protocol import CycleAction
from pal.bunshin.graph_executor import GraphExecution
from pal.bunshin.semantic_orchestration.role_inputs import _candidate_tree_fingerprint
from pal.bunshin.unit_of_work import BunshinUnitOfWork
from pal.bunshin.verification import no_progress_detected


def dependency_failure_history(artifacts: ContentAddressedArtifactStore, node: AggregateSnapshot,
                               capture: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidate = artifacts.read_json(dict(capture["candidate_ref"]))
    return [*list(node.payload.get("failure_history") or []), {
        "finding_fingerprint": str(capture["finding_fingerprint"]),
        "candidate_tree_hash": _candidate_tree_fingerprint(candidate, fallback=str(capture["candidate_digest"])),
    }]


def triage_captured_no_progress(*, artifacts: ContentAddressedArtifactStore, work: BunshinUnitOfWork,
        execution: GraphExecution, node: AggregateSnapshot, capture: Mapping[str, Any]) -> bool:
    if (capture.get("status") != "FAIL" or capture.get("defect_kind") != "dependency_defect"
            or capture.get("routing_errors")):
        return False
    history = dependency_failure_history(artifacts, node, capture)
    if not no_progress_detected(history):
        return False
    pending = dict(capture.get("source_pending_ref") or capture.get("pending_ref") or {})
    work.transitions.dispatch(ActionEnvelope(action_type="ENTER_TRIAGE", workflow_id=node.workflow_id,
        aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node.aggregate_id,
        actor="bunshin-v2-manager", expected_version=node.version,
        idempotency_key=f"dependency-repair:no-progress:{capture['incarnation_key']}",
        payload={"source_pending_verification_ref": pending,
                 "verification_artifact_ref": dict(capture["report_ref"]), "verification_status": "FAIL",
                 "repair_bill_ref": dict(capture["repair_packet_ref"]), "failure_history": history,
                 "finding_fingerprint": str(capture["finding_fingerprint"]),
                 "blocker": {"kind": "no_progress", "rounds": 3,
                             "classification": "captured_dependency_failure"}},
    ))
    name = str(capture["node_name"])
    execution = execution.with_cycle(execution.cycles[name].transition(CycleAction.REQUIRE_TRIAGE))
    work.cycles.store_graph_execution(workflow_id=node.workflow_id, execution=execution)
    return True
