"""Runtime refinement traces for OrchestrationLifecycle.EnterWorkflowTriage.

These check real durable cycle actions and transaction boundaries. Process
retirement is covered separately by the public outbox tests; a PAUSED child
snapshot here represents that completed cleanup, as in the TLA abstraction.
"""
from dataclasses import replace
from unittest.mock import patch

import pytest

from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.bunshin.cycle_protocol import AssignmentKind, CycleSlot, NodeCycleState
from pal.bunshin.orchestration import (
    BunshinV2OutboxProcessor, _reconcile_cycle_control_projection,
    _workflow_pauses_cycles,
)
from pal.bunshin.service import BunshinV2WorkflowService
from pal.bunshin.storage.cycles import CyclesStore
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from tests.test_bunshin_produced_dependency_gates import _compile_graph


@pytest.fixture
def case(tmp_path):
    service = BunshinV2WorkflowService(tmp_path)
    repository = service.repository
    graph = _compile_graph()
    coordinator = WorkflowCoordinator(repository)
    coordinator.install_graph(workflow_id=graph.graph_id, graph=graph)
    for action in ("CREATE_WORKFLOW", "START_WORKFLOW"):
        repository.transitions.dispatch(ActionEnvelope(
            action_type=action, workflow_id=graph.graph_id,
            aggregate_type=AggregateType.WORKFLOW, aggregate_id=graph.graph_id,
            actor="test", idempotency_key=action,
        ))
    return service, coordinator, graph.graph_id


def enter_triage(case, *, project=True):
    service, _, workflow_id = case
    action = ActionEnvelope(
        action_type="ENTER_TRIAGE", workflow_id=workflow_id,
        aggregate_type=AggregateType.WORKFLOW, aggregate_id=workflow_id,
        actor="test", idempotency_key="triage",
    )
    with service.repository.transaction() as transaction:
        workflow = transaction.transitions.dispatch(action).snapshot
        if project:
            BunshinV2OutboxProcessor(service)._sync_cycle_triage(action, unit_of_work=transaction)
    return workflow


@pytest.mark.parametrize("slot", [CycleSlot.PRODUCER, CycleSlot.CHECKER])
@pytest.mark.parametrize("restart", [False, True])
def test_triage_freeze_clears_slot_only_after_cleanup_and_resumes_boundary(case, slot, restart):
    service, coordinator, workflow_id = case
    coordinator.start_assignment(
        workflow_id=workflow_id, node_name="manifest_model", slot=CycleSlot.PRODUCER,
        kind=AssignmentKind.INITIAL, input_fingerprint="original-producer",
    )
    if slot == CycleSlot.CHECKER:
        coordinator.producer_submitted(
            workflow_id=workflow_id, node_name="manifest_model", product_ref="immutable-candidate",
        )
        coordinator.start_assignment(
            workflow_id=workflow_id, node_name="manifest_model", slot=slot,
            kind=AssignmentKind.INITIAL, input_fingerprint="original-checker",
        )
    before = coordinator.execution(workflow_id=workflow_id).cycles["manifest_model"]
    workflow = enter_triage(case, project=not restart)
    if restart:
        assert coordinator.execution(workflow_id=workflow_id).cycles["manifest_model"] == before
        _reconcile_cycle_control_projection(service.repository, workflow, [])
    requested = coordinator.execution(workflow_id=workflow_id).cycles["manifest_model"]
    assert requested.state == NodeCycleState.PAUSE_REQUESTED
    assert requested.active_assignment == before.active_assignment
    assert requested.product_ref == before.product_ref
    coordinator.resume_workflow(workflow_id=workflow_id)
    assert coordinator.execution(workflow_id=workflow_id).cycles["manifest_model"] == requested

    child = AggregateSnapshot(
        aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id="manifest-node",
        workflow_id=workflow_id, state="PAUSED", version=1,
        payload={"module_name": "manifest_model"}, created_at="now", updated_at="now",
    )
    _reconcile_cycle_control_projection(service.repository, workflow, [child])
    paused = coordinator.execution(workflow_id=workflow_id).cycles["manifest_model"]
    assert paused.state == NodeCycleState.PAUSED
    assert paused.active_assignment is None
    assert paused.product_ref == before.product_ref
    _reconcile_cycle_control_projection(service.repository, workflow, [child])
    assert coordinator.execution(workflow_id=workflow_id).cycles["manifest_model"] == paused

    _reconcile_cycle_control_projection(service.repository, replace(workflow, state="ACTIVE"), [child])
    resumed = coordinator.execution(workflow_id=workflow_id).cycles["manifest_model"]
    assert resumed.state == (NodeCycleState.PRODUCER_READY if slot == CycleSlot.PRODUCER else NodeCycleState.CHECKER_READY)
    assert resumed.active_assignment is None
    assert resumed.generation == before.generation
    assert resumed.product_ref == before.product_ref
    coordinator.start_assignment(
        workflow_id=workflow_id, node_name="manifest_model", slot=slot,
        kind=AssignmentKind.INITIAL, input_fingerprint="new-admission",
    )
    with pytest.raises(RuntimeError, match="different .* assignment"):
        coordinator.start_assignment(
            workflow_id=workflow_id, node_name="manifest_model", slot=slot,
            kind=AssignmentKind.INITIAL, input_fingerprint="foreign-admission",
        )


def test_workflow_triage_and_cycle_intent_roll_back_together(case):
    service, coordinator, workflow_id = case
    coordinator.start_assignment(
        workflow_id=workflow_id, node_name="manifest_model", slot=CycleSlot.PRODUCER,
        kind=AssignmentKind.INITIAL, input_fingerprint="original",
    )
    graph = coordinator.execution(workflow_id=workflow_id)
    workflow = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
    with patch.object(CyclesStore, "store_graph_execution", side_effect=RuntimeError("storage fault")):
        with pytest.raises(RuntimeError, match="storage fault"):
            enter_triage(case)
    assert service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id) == workflow
    assert coordinator.execution(workflow_id=workflow_id) == graph
    with service.repository.database.read_connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bunshin_v2_outbox WHERE effect_type = 'freeze_workflow_children'",
        ).fetchone()[0] == 0


@pytest.mark.parametrize("plan", [False, True], ids=["node", "plan"])
@pytest.mark.parametrize("slot", [CycleSlot.PRODUCER, CycleSlot.CHECKER])
def test_workflow_freeze_preserves_existing_child_triage_cursor(case, plan, slot):
    service, coordinator, workflow_id = case
    args = {"workflow_id": workflow_id}
    if not plan:
        args["node_name"] = "manifest_model"
    start = coordinator.start_plan_assignment if plan else coordinator.start_assignment
    submit = coordinator.submit_plan_product if plan else coordinator.producer_submitted
    triage = coordinator.require_plan_triage if plan else coordinator.require_node_triage
    read = (lambda: service.repository.cycles.read_plan_cycle(workflow_id=workflow_id)) if plan else (
        lambda: coordinator.execution(workflow_id=workflow_id).cycles["manifest_model"]
    )
    start(**args, slot=CycleSlot.PRODUCER, kind=AssignmentKind.INITIAL, input_fingerprint="original")
    if slot == CycleSlot.CHECKER:
        submit(**args, product_ref="immutable-product")
        start(**args, slot=slot, kind=AssignmentKind.INITIAL, input_fingerprint="checker")
    triage(**args)
    before = read()
    expected_ready = "PRODUCER_READY" if slot == CycleSlot.PRODUCER else "CHECKER_READY"
    assert before.resume_state.value == expected_ready
    workflow = enter_triage(case)
    assert read() == before
    _reconcile_cycle_control_projection(service.repository, workflow, [])
    _reconcile_cycle_control_projection(service.repository, replace(workflow, state="ACTIVE"), [])
    assert read() == before
    coordinator.resolve_triage(**args, plan=plan)
    assert read().state.value == expected_ready
    assert read().product_ref == before.product_ref
    assert read().active_assignment is None


@pytest.mark.parametrize("resume", ["CREATED", "CANCEL_REQUESTED", "RESTARTING", "", "unknown"])
def test_triage_outside_pause_protocol_does_not_replace_cycle_control(case, resume):
    service, coordinator, workflow_id = case
    coordinator.start_assignment(
        workflow_id=workflow_id, node_name="manifest_model", slot=CycleSlot.PRODUCER,
        kind=AssignmentKind.INITIAL, input_fingerprint="original",
    )
    if resume in {"CANCEL_REQUESTED", "RESTARTING"}:
        coordinator.request_workflow_cancel(workflow_id=workflow_id)
    before = coordinator.execution(workflow_id=workflow_id)
    workflow = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
    workflow = replace(workflow, state="TRIAGE_REQUIRED", payload={"triage_resume_state": resume})
    assert not _workflow_pauses_cycles(workflow)
    _reconcile_cycle_control_projection(service.repository, workflow, [])
    assert coordinator.execution(workflow_id=workflow_id) == before
