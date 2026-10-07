from __future__ import annotations
from pal.bunshin.contract_runtime import ContractArtifactAccess
from pal.bunshin.contracts import AggregateType
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.graph_executor import GraphDiff, NodeReuseKind
from pal.bunshin.graph_protocol import GraphIR
from pal.bunshin.execution_values import _action
from pal.bunshin.replan_workspaces import _retire_removed_module_resources
from pal.bunshin.module_identity_facts import _same_module_identity
from pal.bunshin.execution_graph_facts import _topological_module_order


def _reconcile_data_contract_module_identities(
    *,
    repository: BunshinRepository,
    contracts: ContractArtifactAccess,
    workflow_id: str,
    source_epoch_id: str,
    target_epoch_id: str,
    target_graph: GraphIR,
    graph_diff: GraphDiff,
    actor: str,
) -> tuple[str, ...]:
    """Carry exact immutable artifact modules across a contract revision."""

    snapshots = repository.queries.list_workflow_snapshots(workflow_id)
    source_nodes = {
        str(item.payload.get("module_name") or item.payload.get("unit_id") or ""): item
        for item in snapshots
        if item.aggregate_type == AggregateType.DAG_NODE_RUN
        and str(item.payload.get("epoch_id") or "") == source_epoch_id
        and str(item.payload.get("node_kind") or "") == "unit"
    }
    target_nodes = {
        str(item.payload.get("module_name") or item.payload.get("unit_id") or ""): item
        for item in snapshots
        if item.aggregate_type == AggregateType.DAG_NODE_RUN
        and str(item.payload.get("epoch_id") or "") == target_epoch_id
        and str(item.payload.get("node_kind") or "") == "unit"
    }
    target_dependencies = {
        name: [
            provider
            for provider in target_graph.execution_predecessors(name)
            if provider in target_nodes
        ]
        for name in target_nodes
    }
    carried: list[str] = []
    for name in _topological_module_order(target_dependencies):
        source = source_nodes.get(name)
        target = repository.snapshots.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            target_nodes[name].aggregate_id,
        )
        if source is None or target is None:
            continue
        decision = graph_diff.decisions.get(name)
        if decision is None:
            raise RuntimeError(
                f"GraphDiff is missing reuse decision for projected node {name}"
            )
        if decision.kind != NodeReuseKind.REUSE_ACCEPTED:
            continue
        if (
            source.state != "ACCEPTED"
            or target.state != "BLOCKED_BY_DEPS"
            or not _same_module_identity(source, target)
        ):
            continue
        accepted_dependencies = [
            str(item)
            for item in list(target.payload.get("dependency_node_ids") or [])
            if (
                repository.snapshots.read_snapshot(
                    AggregateType.DAG_NODE_RUN,
                    str(item),
                )
                or target
            ).state
            == "ACCEPTED"
        ]
        if len(accepted_dependencies) != len(
            list(target.payload.get("dependency_node_ids") or [])
        ):
            continue
        candidate_ref = dict(source.payload.get("candidate_ref") or {})
        verification_ref = dict(
            source.payload.get("verification_artifact_ref") or {}
        )
        candidate_digest = str(source.payload.get("candidate_digest") or "")
        if not all((candidate_ref, verification_ref, candidate_digest)):
            continue
        result = repository.transitions.dispatch(
            _action(
                "CARRY_FORWARD_MODULE",
                workflow_id,
                AggregateType.DAG_NODE_RUN,
                target.aggregate_id,
                actor,
                target.version,
                {
                    "candidate_ref": candidate_ref,
                    "candidate_digest": candidate_digest,
                    "verification_artifact_ref": verification_ref,
                    "graph_contract_hash": target_graph.nodes[
                        name
                    ].contract_hash,
                    "accepted_dependency_node_ids": accepted_dependencies,
                    "epoch_frozen": False,
                    "output_hashes": dict(
                        source.payload.get("output_hashes") or {}
                    ),
                    "dependency_output_hashes": dict(
                        source.payload.get("dependency_output_hashes") or {}
                    ),
                    "carried_forward_from_epoch_id": source_epoch_id,
                    "carried_forward_from_node_run_id": source.aggregate_id,
                },
            )
        )
        target_nodes[name] = result.snapshot
        carried.append(result.snapshot.aggregate_id)
    _retire_removed_module_resources(
        repository=repository,
        workflow_id=workflow_id,
        source_nodes=source_nodes,
        target_module_names=set(target_nodes),
    )
    return tuple(carried)
