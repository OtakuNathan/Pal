from __future__ import annotations
from typing import Any
from pal.bunshin.workspace_resources import exclusive_workspace_lock as exclusive_workspace_lock
from dataclasses import dataclass
from pathlib import Path
from pal.bunshin.adapters import SOFTWARE_GIT_ADAPTER
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.bunshin.execution_values import _action
from pal.bunshin.execution_graph_facts import dependency_fingerprint
from pal.bunshin.dependency_baselines import prepare_node_dependency_baseline
from pal.bunshin.dependency_baselines import prepare_node_verification_baseline


@dataclass
class DagScheduler:
    repository: BunshinV2Repository

    def schedule_ready_nodes(
        self,
        *,
        workflow_id: str,
        epoch_id: str,
        actor: str = "bunshin-scheduler",
    ) -> tuple[str, ...]:
        snapshots = self.repository.queries.list_workflow_snapshots(workflow_id)
        epoch = next(
            (
                item
                for item in snapshots
                if item.aggregate_type == AggregateType.EXECUTION_EPOCH and item.aggregate_id == epoch_id
            ),
            None,
        )
        if epoch is None or epoch.state != "RUNNING":
            return ()
        node_by_id = {
            item.aggregate_id: item
            for item in snapshots
            if item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == epoch_id
        }
        accepted_ids, ready_checkers, ready_producers = self.ready_assignments(epoch, node_by_id, workflow_id)
        scheduled: list[str] = []
        # Readiness creates durable logical coroutine work. It does not reserve
        # an OS process slot: the process shell owns the only execution
        # semaphore and acquires it immediately before materialization.
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

    def ready_assignments(self, epoch: AggregateSnapshot, node_by_id: dict[str, AggregateSnapshot], workflow_id: str) -> tuple[set[str], list[AggregateSnapshot], list[AggregateSnapshot]]:
        accepted_ids = {node_id for node_id, item in node_by_id.items() if item.state == "ACCEPTED"}
        graph_execution = WorkflowCoordinator(self.repository).execution(
            workflow_id=workflow_id,
            generation=int(epoch.payload.get("graph_generation") or 0) or None,
        )
        runnable_assignments = WorkflowCoordinator(
            self.repository
        ).runnable_assignments(
            workflow_id=workflow_id,
            generation=graph_execution.graph.generation,
        )
        runnable_producers = {
            assignment.node_name
            for assignment in runnable_assignments
            if assignment.slot.value == "producer"
        }
        runnable_checkers = {
            assignment.node_name
            for assignment in runnable_assignments
            if assignment.slot.value == "checker"
        }
        node_id_by_name = {
            str(item.payload.get("module_name") or item.payload.get("unit_id") or ""): node_id
            for node_id, item in node_by_id.items()
        }
        ready_producers: list[AggregateSnapshot] = []
        ready_checkers: list[AggregateSnapshot] = []
        for node_id in sorted(node_by_id):
            node = node_by_id[node_id]
            module_name = str(
                node.payload.get("module_name")
                or node.payload.get("unit_id")
                or ""
            )
            if (
                node.state in {"BLOCKED_BY_DEPS", "STALE"}
                and module_name in runnable_producers
            ):
                expected = {
                    node_id_by_name[name]
                    for name in graph_execution.graph.producer_predecessors(module_name)
                }
                projected = {
                    str(item)
                    for item in list(
                        node.payload.get("producer_dependency_node_ids") or []
                    )
                }
                if projected != expected:
                    raise RuntimeError(
                        "projected producer dependencies disagree with GraphIR: "
                        f"{module_name}"
                    )
                if not expected <= accepted_ids:
                    raise RuntimeError(
                        "GraphExecution marked a producer runnable before its "
                        f"dependencies were accepted: {module_name}"
                    )
                ready_producers.append(node)
            if (
                node.state == "REVIEW_BLOCKED_BY_DEPS"
                and module_name in runnable_checkers
            ):
                expected = {
                    node_id_by_name[name]
                    for name in graph_execution.graph.checker_predecessors(module_name)
                }
                projected = {
                    str(item)
                    for item in list(node.payload.get("dependency_node_ids") or [])
                }
                if projected != expected:
                    raise RuntimeError(
                        "projected verification dependencies disagree with GraphIR: "
                        f"{module_name}"
                    )
                if not expected <= accepted_ids:
                    raise RuntimeError(
                        "GraphExecution marked a checker runnable before its "
                        f"dependencies were accepted: {module_name}"
                    )
                ready_checkers.append(node)
        return accepted_ids, ready_checkers, ready_producers
