from __future__ import annotations
from pal.bunshin.workspace_resources import exclusive_workspace_lock as exclusive_workspace_lock
from dataclasses import dataclass
from pal.bunshin.machine_dsl import ControlDisposition, ControlIntent
from pal.bunshin.machines import machine_spec_for
from pal.bunshin.coroutine_runtime import NodeSlot
from pathlib import Path
from pal.bunshin.adapters import SOFTWARE_GIT_ADAPTER
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.cycle_protocol import CycleSlot, NodeCycleState
from pal.bunshin.graph_executor import GraphExecution
from pal.bunshin.workflow_runtime import node_pause_must_drain
from pal.bunshin.unit_of_work import BunshinUnitOfWork
from pal.bunshin.execution_values import _action
from pal.bunshin.execution_graph_facts import accepted_node_receipt, dependency_fingerprint, graph_node_bindings
from pal.bunshin.dependency_baselines import prepare_node_dependency_baseline
from pal.bunshin.dependency_baselines import prepare_node_verification_baseline


@dataclass
class NodeScheduler:
    """Materialize runnable NodeCycle work and manage its node resource slot."""

    repository: BunshinRepository

    def control_nodes(self, intent: ControlIntent | None, epoch: AggregateSnapshot, *, unit_of_work: BunshinUnitOfWork) -> None:
        snapshots = unit_of_work.queries.list_workflow_snapshots(epoch.workflow_id)
        nodes = [
            item
            for item in snapshots
            if item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == epoch.aggregate_id
        ]
        node_machine = machine_spec_for(AggregateType.DAG_NODE_RUN)
        execution = unit_of_work.cycles.read_graph_execution(workflow_id=epoch.workflow_id) if intent == ControlIntent.PAUSE else None
        for node in nodes:
            if intent is None:
                action_type = "RESUME"
                if action_type not in unit_of_work.transitions.legal_actions(
                    AggregateType.DAG_NODE_RUN,
                    node.state,
                ):
                    continue
            else:
                if intent == ControlIntent.PAUSE and node_pause_must_drain(node, execution, unit_of_work.queries):
                    continue
                if (
                    node_machine.control_disposition(intent, node.state)
                    != ControlDisposition.REQUEST
                ):
                    continue
                action_type = node_machine.control_policies[intent].request_action
            unit_of_work.transitions.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="bunshin-control",
                    expected_version=node.version,
                    idempotency_key=f"control:{epoch.aggregate_id}:{epoch.version}:{node.aggregate_id}:{node.version}:{action_type}",
                )
            )

    def predecessor_settled(self, assignment_id: str) -> bool:
        assignment = self.repository.role_assignments.read_role_assignment(assignment_id)
        return assignment is not None and assignment["state"] == "settled"

    def retain_node_slot(self, slot: NodeSlot) -> bool:
        epoch = self.repository.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, slot.epoch_id)
        if epoch is None or epoch.state != "RUNNING":
            return False
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, slot.workflow_id)
        if workflow is not None and workflow.state not in {"CREATED", "ACTIVE"}:
            return False
        node = self.repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, slot.node_id)
        if (node is None or node.workflow_id != slot.workflow_id
                or node.payload.get("epoch_id") != slot.epoch_id):
            return False
        graph = self.repository.cycles.read_graph_execution(workflow_id=slot.workflow_id)
        if graph is None or graph.graph.generation != slot.generation or graph.state.value != "RUNNING":
            return False
        name = str(node.payload.get("module_name") or node.payload.get("unit_id") or "")
        if self.repository.queries.node_effect_in_backoff(slot.node_id):
            return False
        if name not in graph.cycles:
            return False
        return graph.schedule.retain_slot(name)

    def schedule_ready_nodes(
        self,
        *,
        workflow_id: str,
        epoch_id: str,
        actor: str = "bunshin-scheduler",
    ) -> tuple[str, ...]:
        # Read both sides of the atomic receipt commit in one database view.
        # Do not keep the transaction open during Git/workspace preparation.
        with self.repository.transaction() as view:
            epoch = view.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
            if epoch is None or epoch.state != "RUNNING" or epoch.workflow_id != workflow_id:
                return ()
            workflow = view.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
            if workflow is not None and workflow.state not in {"CREATED", "ACTIVE"}:
                return ()
            execution = view.cycles.read_graph_execution(workflow_id=workflow_id)
            if execution is None:
                raise RuntimeError("workflow has no installed GraphExecution")
            if int(epoch.payload.get("graph_generation") or execution.graph.generation) != execution.graph.generation:
                return ()  # A historical epoch cannot dispatch the active graph.
            node_by_id = {
                item.aggregate_id: item
                for item in view.queries.list_workflow_snapshots(workflow_id)
                if item.aggregate_type == AggregateType.DAG_NODE_RUN
                and str(item.payload.get("epoch_id") or "") == epoch_id
            }
            accepted_ids, ready_checkers, ready_producers = self.ready_assignments(execution, node_by_id)
        scheduled: list[str] = []
        # Queue durable work here. Role admission acquires or reuses the
        # node's slot when the assignment is ready to materialize.
        for node in ready_producers:
            producer_dependencies = {
                str(item)
                for item in list(
                    node.payload.get("producer_dependency_node_ids") or []
                )
            }
            baseline = (
                prepare_node_dependency_baseline(
                    node,
                    node_by_id,
                    apply_candidates=True,
                    artifacts=ContentAddressedArtifactStore(self.repository.runtime_root, self.repository.artifacts),
                )
                if producer_dependencies
                else {}
            )
            payload = {
                "accepted_producer_dependency_node_ids": sorted(
                    producer_dependencies & accepted_ids
                ),
                "epoch_frozen": False,
                **baseline,
            }
            if node.state == "BLOCKED_BY_DEPS":
                action_type = "DEPENDENCIES_ACCEPTED"
            else:
                action_type = "REQUEUE_STALE"
            if node.state == "STALE":
                payload.update(
                    {
                        "unit_contract_ref": node.payload.get("unit_contract_ref"),
                        "dependency_fingerprint": dependency_fingerprint(node, node_by_id),
                    }
                )
            self.repository.transitions.dispatch(
                _action(
                    action_type,
                    workflow_id,
                    AggregateType.DAG_NODE_RUN,
                    node.aggregate_id,
                    actor,
                    node.version,
                    payload,
                )
            )
            scheduled.append(node.aggregate_id)
        for node in ready_checkers:
            artifacts = ContentAddressedArtifactStore(
                self.repository.runtime_root,
                self.repository.artifacts,
            )

            def assemble_and_publish(current: AggregateSnapshot) -> bool:
                if current.state != "REVIEW_BLOCKED_BY_DEPS":
                    return False
                baseline = prepare_node_verification_baseline(
                    current,
                    node_by_id,
                    artifacts=artifacts,
                )
                self.repository.transitions.dispatch(
                    _action(
                        "VERIFICATION_DEPENDENCIES_ACCEPTED",
                        workflow_id,
                        AggregateType.DAG_NODE_RUN,
                        current.aggregate_id,
                        actor,
                        current.version,
                        {
                            "accepted_dependency_node_ids": sorted(
                                set(
                                    current.payload.get("dependency_node_ids")
                                    or []
                                )
                                & accepted_ids
                            ),
                            "epoch_frozen": False,
                            **baseline,
                        },
                    )
                )
                return True

            if str(node.payload.get("execution_adapter") or "") == SOFTWARE_GIT_ADAPTER:
                workspace = Path(str(node.payload.get("workspace_path") or ""))
                with exclusive_workspace_lock(workspace):
                    current = self.repository.snapshots.read_snapshot(
                        AggregateType.DAG_NODE_RUN,
                        node.aggregate_id,
                    )
                    if current is None or not assemble_and_publish(current):
                        continue
            elif not assemble_and_publish(node):
                continue
            scheduled.append(node.aggregate_id)
        return tuple(scheduled)

    def ready_assignments(self, execution: GraphExecution, node_by_id: dict[str, AggregateSnapshot]) -> tuple[set[str], list[AggregateSnapshot], list[AggregateSnapshot]]:
        bindings = graph_node_bindings(execution.graph, node_by_id.values())
        accepted_ids = {
            bindings[name].aggregate_id for name, cycle in execution.cycles.items()
            if cycle.state == NodeCycleState.ACCEPTED
        }
        # A dependency baseline consumes receipts, not the historical state
        # label. Refuse a mismatched projection instead of scheduling from it.
        for name, cycle in execution.cycles.items():
            if cycle.state == NodeCycleState.ACCEPTED:
                accepted_node_receipt(bindings[name], cycle)
        ready_producers: list[AggregateSnapshot] = []
        ready_checkers: list[AggregateSnapshot] = []
        for name in execution.runnable_nodes():
            node = bindings[name]
            work = execution.cycles[name].pending_work
            assert work is not None
            if work.slot == CycleSlot.PRODUCER and node.state in {"BLOCKED_BY_DEPS", "STALE"}:
                predecessors = execution.graph.producer_predecessors(name)
                key = "producer_dependency_node_ids"
                ready = ready_producers
            elif work.slot == CycleSlot.CHECKER and node.state == "REVIEW_BLOCKED_BY_DEPS":
                predecessors = execution.graph.checker_predecessors(name)
                key = "dependency_node_ids"
                ready = ready_checkers
            else:
                # The cycle is runnable but its durable dispatch is already
                # queued, or a process/receipt effect is still settling.
                continue
            expected = {bindings[dependency].aggregate_id for dependency in predecessors}
            projected = {str(item) for item in node.payload.get(key, [])}
            if projected != expected:
                raise RuntimeError(f"projected {work.slot.value} dependencies disagree with GraphIR: {name}")
            ready.append(node)
        # Preserve previous stable dispatch ordering by node identity.
        return accepted_ids, sorted(ready_checkers, key=lambda node: node.aggregate_id), sorted(ready_producers, key=lambda node: node.aggregate_id)


# Historical import used by external callers and older tests. This is the
# same scheduler, not a second scheduler or a second persisted state machine.
DagScheduler = NodeScheduler
