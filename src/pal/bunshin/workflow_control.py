"""Project durable pause, resume and cancellation through the workflow and its graph."""
from __future__ import annotations

from typing import Any

from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.bunshin.machine_dsl import ControlDisposition, ControlIntent
from pal.bunshin.machines import machine_spec_for
from pal.bunshin.workflow_runtime import WorkflowCoordinator, node_pause_must_drain, role_pause_must_drain, workflow_pauses_cycles as _workflow_pauses_cycles


# A durable control effect is a wake-up for the current owner, not authority
# to reinstate the intent which originally enqueued it.
CONTROL_EFFECT_SOURCES = {
    "propagate_pause": (AggregateType.WORKFLOW, "PAUSE_REQUESTED"),
    "propagate_cancel": (AggregateType.WORKFLOW, "CANCEL_REQUESTED"),
    "propagate_resume": (AggregateType.WORKFLOW, "ACTIVE"),
    "freeze_workflow_children": (AggregateType.WORKFLOW, "TRIAGE_REQUIRED"),
    "pause_epoch_nodes": (AggregateType.EXECUTION_EPOCH, "PAUSE_REQUESTED"),
    "cancel_epoch_nodes": (AggregateType.EXECUTION_EPOCH, "CANCEL_REQUESTED"),
    "resume_epoch_nodes": (AggregateType.EXECUTION_EPOCH, "RUNNING"),
    "freeze_epoch_nodes": (AggregateType.EXECUTION_EPOCH, "TRIAGE_REQUIRED"),
    "suspend_stale_node_assignments": (AggregateType.DAG_NODE_RUN, "STALE"),
}
CONTROL_FREEZE_ORIGINS = {
    "freeze_workflow_children": frozenset({"ACTIVE"}),
    "freeze_epoch_nodes": frozenset({"STARTING", "RUNNING", "REPLAN_COLLECTING", "REPLAN_REQUIRED", "FINALIZING"}),
}


def control_effect_is_current(effect_type: str, snapshot: AggregateSnapshot) -> bool:
    if CONTROL_EFFECT_SOURCES[effect_type] != (snapshot.aggregate_type, snapshot.state):
        return False
    origins = CONTROL_FREEZE_ORIGINS.get(effect_type)
    return origins is None or str(snapshot.payload.get("triage_resume_state") or "") in origins


def reconcile_control_requests(repository: Any, workflow_id: str) -> None:
    if not workflow_id:
        return
    snapshots = list(repository.queries.list_workflow_snapshots(workflow_id))
    workflow = next(
        (item for item in snapshots if item.aggregate_type == AggregateType.WORKFLOW),
        None,
    )
    if workflow is None:
        return
    _reconcile_cycle_control_projection(repository, workflow, snapshots)
    requested_intent = {
        "PAUSE_REQUESTED": ControlIntent.PAUSE,
        "PAUSED": ControlIntent.PAUSE,
        "CANCEL_REQUESTED": ControlIntent.CANCEL,
        "CANCELLED": ControlIntent.CANCEL,
        "RESTARTING": ControlIntent.CANCEL,
    }.get(workflow.state)
    if requested_intent is not None:
        for child in _active_control_children(snapshots, workflow):
            if requested_intent == ControlIntent.PAUSE and role_pause_must_drain(child, repository.queries):
                continue
            machine = machine_spec_for(child.aggregate_type)
            if (
                machine.control_disposition(requested_intent, child.state)
                != ControlDisposition.REQUEST
            ):
                continue
            action_type = machine.control_policies[requested_intent].request_action
            repository.transitions.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=workflow_id,
                    aggregate_type=child.aggregate_type,
                    aggregate_id=child.aggregate_id,
                    actor="bunshin-control",
                    expected_version=child.version,
                    idempotency_key=(
                        f"reconcile:{workflow_id}:{action_type.lower()}:"
                        f"{child.aggregate_id}:{child.version}"
                    ),
                )
            )
    elif workflow.state == "ACTIVE":
        for child in _active_control_children(snapshots, workflow):
            if child.state != "PAUSED" or "RESUME" not in repository.transitions.legal_actions(
                child.aggregate_type,
                child.state,
            ):
                continue
            repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="RESUME",
                    workflow_id=workflow_id,
                    aggregate_type=child.aggregate_type,
                    aggregate_id=child.aggregate_id,
                    actor="bunshin-control",
                    expected_version=child.version,
                    idempotency_key=(
                        f"reconcile:{workflow_id}:resume:"
                        f"{child.aggregate_id}:{child.version}"
                    ),
                )
            )

    snapshots = list(repository.queries.list_workflow_snapshots(workflow_id))
    for epoch in [
        item
        for item in snapshots
        if item.aggregate_type == AggregateType.EXECUTION_EPOCH
    ]:
        nodes = [
            item
            for item in snapshots
            if item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == epoch.aggregate_id
        ]
        node_machine = machine_spec_for(AggregateType.DAG_NODE_RUN)
        epoch_intent = {
            "PAUSE_REQUESTED": ControlIntent.PAUSE,
            "CANCEL_REQUESTED": ControlIntent.CANCEL,
        }.get(epoch.state)
        if epoch_intent is not None:
            execution = repository.cycles.read_graph_execution(workflow_id=workflow_id) if epoch_intent == ControlIntent.PAUSE else None
            for node in nodes:
                if epoch_intent == ControlIntent.PAUSE and node_pause_must_drain(node, execution, repository.queries):
                    continue
                if (
                    node_machine.control_disposition(epoch_intent, node.state)
                    != ControlDisposition.REQUEST
                ):
                    continue
                action_type = node_machine.control_policies[epoch_intent].request_action
                repository.transitions.dispatch(
                    ActionEnvelope(
                        action_type=action_type,
                        workflow_id=workflow_id,
                        aggregate_type=AggregateType.DAG_NODE_RUN,
                        aggregate_id=node.aggregate_id,
                        actor="bunshin-control",
                        expected_version=node.version,
                        idempotency_key=(
                            f"reconcile:{epoch.aggregate_id}:{action_type.lower()}:"
                            f"{node.aggregate_id}:{node.version}"
                        ),
                    )
                )
            snapshots = list(repository.queries.list_workflow_snapshots(workflow_id))
            nodes = [
                item
                for item in snapshots
                if item.aggregate_type == AggregateType.DAG_NODE_RUN
                and str(item.payload.get("epoch_id") or "") == epoch.aggregate_id
            ]
            epoch = next(
                item
                for item in snapshots
                if item.aggregate_type == AggregateType.EXECUTION_EPOCH
                and item.aggregate_id == epoch.aggregate_id
            )
        if epoch.state == "PAUSE_REQUESTED" and all(
            node_machine.control_disposition(ControlIntent.PAUSE, item.state)
            == ControlDisposition.SETTLED
            for item in nodes
        ):
            repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="NODES_PAUSED",
                    workflow_id=workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH,
                    aggregate_id=epoch.aggregate_id,
                    actor="bunshin-control",
                    expected_version=epoch.version,
                    idempotency_key=f"reconcile:{epoch.aggregate_id}:{epoch.version}:paused",
                )
            )
        elif epoch.state == "CANCEL_REQUESTED" and all(
            node_machine.control_disposition(ControlIntent.CANCEL, item.state)
            == ControlDisposition.SETTLED
            for item in nodes
        ):
            repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="NODES_CANCELLED",
                    workflow_id=workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH,
                    aggregate_id=epoch.aggregate_id,
                    actor="bunshin-control",
                    expected_version=epoch.version,
                    idempotency_key=f"reconcile:{epoch.aggregate_id}:{epoch.version}:cancelled",
                )
            )
        elif epoch.state in {"STARTING", "RUNNING", "FINALIZING"}:
            for node in nodes:
                if node.state != "PAUSED" or "RESUME" not in repository.transitions.legal_actions(
                    AggregateType.DAG_NODE_RUN,
                    node.state,
                ):
                    continue
                repository.transitions.dispatch(
                    ActionEnvelope(
                        action_type="RESUME",
                        workflow_id=workflow_id,
                        aggregate_type=AggregateType.DAG_NODE_RUN,
                        aggregate_id=node.aggregate_id,
                        actor="bunshin-control",
                        expected_version=node.version,
                        idempotency_key=(
                            f"reconcile:{epoch.aggregate_id}:resume:"
                            f"{node.aggregate_id}:{node.version}"
                        ),
                    )
                )

    snapshots = list(repository.queries.list_workflow_snapshots(workflow_id))
    workflow = next(
        (item for item in snapshots if item.aggregate_type == AggregateType.WORKFLOW),
        None,
    )
    if workflow is None:
        return
    children = _active_control_children(snapshots, workflow)
    if workflow.state == "PAUSE_REQUESTED" and all(
        machine_spec_for(item.aggregate_type).control_disposition(
            ControlIntent.PAUSE,
            item.state,
        )
        == ControlDisposition.SETTLED
        for item in children
    ):
        repository.transitions.dispatch(
            ActionEnvelope(
                action_type="CHILDREN_PAUSED",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="bunshin-control",
                expected_version=workflow.version,
                idempotency_key=f"reconcile:{workflow_id}:{workflow.version}:paused",
            )
        )
    elif workflow.state == "CANCEL_REQUESTED" and all(
        machine_spec_for(item.aggregate_type).control_disposition(
            ControlIntent.CANCEL,
            item.state,
        )
        == ControlDisposition.SETTLED
        for item in children
    ):
        repository.transitions.dispatch(
            ActionEnvelope(
                action_type="CHILDREN_CANCELLED",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="bunshin-control",
                expected_version=workflow.version,
                idempotency_key=f"reconcile:{workflow_id}:{workflow.version}:cancelled",
            )
        )

    refreshed = list(repository.queries.list_workflow_snapshots(workflow_id))
    refreshed_workflow = next(
        (
            item
            for item in refreshed
            if item.aggregate_type == AggregateType.WORKFLOW
        ),
        None,
    )
    if refreshed_workflow is not None:
        _reconcile_cycle_control_projection(
            repository,
            refreshed_workflow,
            refreshed,
        )


def _reconcile_cycle_control_projection(
    repository: Any,
    workflow: AggregateSnapshot,
    snapshots: list[AggregateSnapshot],
) -> None:
    """Repair the crash window between aggregate control and cycle state.

    Aggregate machines project process/effect progress. PlanCycle and
    GraphExecution remain the semantic owners, so this reconciliation is
    one-way and idempotent: committed aggregate intent may repair a missing
    cycle write, never the reverse.
    """

    coordinator = WorkflowCoordinator(repository)
    if _workflow_pauses_cycles(workflow):
        coordinator.request_workflow_pause(workflow_id=workflow.workflow_id)
    elif workflow.state in {"CANCEL_REQUESTED", "CANCELLED"}:
        coordinator.request_workflow_cancel(workflow_id=workflow.workflow_id)
    elif workflow.state == "ACTIVE":
        coordinator.resume_workflow(workflow_id=workflow.workflow_id)

    active_revision_id = str(
        workflow.payload.get("architecture_revision_id") or ""
    )
    for item in snapshots:
        if (
            item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
            and item.aggregate_id == active_revision_id
            and item.state in {"PAUSED", "CANCELLED"}
        ):
            coordinator.confirm_plan_control(
                workflow_id=workflow.workflow_id,
                cancel=item.state == "CANCELLED",
            )
        if (
            item.aggregate_type == AggregateType.DAG_NODE_RUN
            and item.state in {"PAUSED", "CANCELLED"}
        ):
            coordinator.confirm_node_control(
                workflow_id=workflow.workflow_id,
                node_name=str(
                    item.payload.get("module_name")
                    or item.payload.get("unit_id")
                    or ""
                ),
                cancel=item.state == "CANCELLED",
                generation=int(item.payload.get("graph_generation") or 0),
            )


def _active_control_children(
    snapshots: list[AggregateSnapshot],
    workflow: AggregateSnapshot,
) -> list[AggregateSnapshot]:
    active_ids = {
        str(workflow.payload.get(field) or "")
        for field in (
            "architecture_revision_id",
            "execution_epoch_id",
            "standalone_review_id",
        )
        if str(workflow.payload.get(field) or "")
    }
    children = [
        item
        for item in snapshots
        if item.aggregate_type
        in {
            AggregateType.ARCHITECTURE_REVISION,
            AggregateType.EXECUTION_EPOCH,
            AggregateType.STANDALONE_REVIEW,
        }
        and item.aggregate_id in active_ids
    ]
    if active_ids:
        active_epoch = str(workflow.payload.get("execution_epoch_id") or "")
        # A pinned preparation belongs to the current lineage before it
        # becomes the active execution pointer. Include it in control and
        # confirmation, without dragging unrelated historical epochs in.
        children.extend(
            item for item in snapshots
            if item.aggregate_type == AggregateType.EXECUTION_EPOCH
            and item.aggregate_id not in active_ids
            and item.state not in {"SUPERSEDED", "COMPLETED", "CANCELLED"}
            and "source_execution_epoch_id" in item.payload
            and str(item.payload["source_execution_epoch_id"] or "") == active_epoch
        )
        return children
    return [
        item
        for item in snapshots
        if item.aggregate_type
        in {
            AggregateType.ARCHITECTURE_REVISION,
            AggregateType.EXECUTION_EPOCH,
            AggregateType.STANDALONE_REVIEW,
        }
        and item.state
        not in {
            "ACCEPTED",
            "REJECTED",
            "COMPLETED",
            "CANCELLED",
            "STALE",
            "SUPERSEDED",
        }
    ]
