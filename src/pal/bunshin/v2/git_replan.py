from __future__ import annotations
from pal.bunshin.v2.contract_runtime import ContractArtifactAccess
from pal.bunshin.v2.artifacts import ArtifactRef
from pal.bunshin.v2.contracts import AggregateSnapshot, AggregateType
from collections.abc import Sequence
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.graph_executor import GraphDiff, NodeReuseKind
from pal.bunshin.v2.graph_protocol import GraphIR
from pal.bunshin.v2.execution_values import _action
from pal.bunshin.v2.replan_workspaces import _checkpoint_preserved_module_head
from pal.bunshin.v2.replan_workspaces import _merge_architecture_into_preserved_module
from pal.bunshin.v2.replan_workspaces import _recreate_replaced_module_worktree
from pal.bunshin.v2.replan_workspaces import _retire_removed_module_resources
from pal.bunshin.v2.module_identity_facts import _same_module_identity
from pal.bunshin.v2.replan_workspaces import _snapshot_module_workspace_for_replan
from pal.bunshin.v2.execution_graph_facts import _topological_module_order


def _reconcile_skeleton_module_identities(
    *,
    repository: BunshinV2Repository,
    contracts: ContractArtifactAccess,
    workflow_id: str,
    source_epoch_id: str,
    target_epoch_id: str,
    source_manifest_ref: ArtifactRef,
    target_manifest_ref: ArtifactRef,
    target_graph: GraphIR,
    graph_diff: GraphDiff,
    actor: str,
) -> tuple[str, ...]:
    snapshots = repository.queries.list_workflow_snapshots(workflow_id)
    source_nodes, source_contracts = _epoch_module_contracts(snapshots, contracts, source_epoch_id)
    target_nodes, target_contracts = _epoch_module_contracts(snapshots, contracts, target_epoch_id)
    if set(target_graph.nodes) != set(target_nodes):
        return ()
    dependencies = {
        name: [
            provider
            for provider in target_graph.execution_predecessors(name)
            if provider in target_nodes
        ]
        for name in target_nodes
    }
    carried_forward: list[str] = []
    for module_name in _topological_module_order(dependencies):
        source_node = source_nodes.get(module_name)
        target_node = repository.snapshots.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            target_nodes[module_name].aggregate_id,
        )
        if source_node is None or target_node is None:
            continue
        decision = graph_diff.decisions.get(module_name)
        if decision is None:
            raise RuntimeError(
                f"GraphDiff is missing reuse decision for projected node {module_name}"
            )
        if not _same_module_identity(source_node, target_node):
            if decision.kind != NodeReuseKind.CREATE:
                raise RuntimeError(
                    "GraphDiff and Module workspace identity disagree for "
                    + module_name
                )
            _recreate_replaced_module_worktree(
                repository=repository,
                workflow_id=workflow_id,
                source_node=source_node,
                target_node=target_node,
            )
            continue
        source_contract = source_contracts.get(module_name)
        target_contract = target_contracts.get(module_name)
        if source_contract is None or target_contract is None:
            continue
        source_candidate_ref = dict(source_node.payload.get("candidate_ref") or {})
        verification_ref = dict(source_node.payload.get("verification_artifact_ref") or {})
        source_candidate_digest = str(source_node.payload.get("candidate_digest") or "")
        if not source_candidate_ref or not source_candidate_digest:
            (
                source_candidate_ref,
                source_candidate_digest,
            ) = _snapshot_module_workspace_for_replan(
                contracts=contracts,
                source_node=source_node,
                target_node=target_node,
            )
        module_head = _merge_architecture_into_preserved_module(
            source_node=source_node,
            target_node=target_node,
        )
        baseline = {
            "base_sha": module_head,
            "base_digest": module_head,
            "accepted_dependency_candidate_digests": [],
            "dependency_output_hashes": {},
            "dependency_outputs": {},
            "dependency_fingerprint": "",
        }
        can_carry_acceptance = (
            decision.kind == NodeReuseKind.REUSE_ACCEPTED
            and source_node.state == "ACCEPTED"
            and bool(verification_ref)
        )
        if not can_carry_acceptance:
            result = repository.transitions.dispatch(
                _action(
                    "PRESERVE_MODULE_WORKTREE",
                    workflow_id,
                    AggregateType.DAG_NODE_RUN,
                    target_node.aggregate_id,
                    actor,
                    target_node.version,
                    {
                        "preserved_from_epoch_id": source_epoch_id,
                        "preserved_from_node_run_id": source_node.aggregate_id,
                        "parent_candidate_digest": source_candidate_digest,
                        "module_replan_prepared": True,
                        "preserved_workspace_paths": [],
                        "replan_conflict_paths": [],
                        **baseline,
                    },
                )
            )
            target_nodes[module_name] = result.snapshot
            continue
        candidate_ref = _checkpoint_preserved_module_head(
            contracts=contracts,
            target_node=target_node,
            source_candidate_ref=source_candidate_ref,
            source_candidate_digest=source_candidate_digest,
            target_contract_ref=target_contract[0],
            module_head=module_head,
        )
        result = repository.transitions.dispatch(
            _action(
                "CARRY_FORWARD_MODULE",
                workflow_id,
                AggregateType.DAG_NODE_RUN,
                target_node.aggregate_id,
                actor,
                target_node.version,
                {
                    "candidate_ref": candidate_ref.to_dict(),
                    "candidate_digest": module_head,
                    "verification_artifact_ref": verification_ref,
                    "graph_contract_hash": target_graph.nodes[
                        module_name
                    ].contract_hash,
                    "accepted_dependency_node_ids": [],
                    "epoch_frozen": False,
                    "output_hashes": dict(source_node.payload.get("output_hashes") or {}),
                    "carried_forward_from_epoch_id": source_epoch_id,
                    **baseline,
                },
            )
        )
        target_nodes[module_name] = result.snapshot
        carried_forward.append(result.snapshot.aggregate_id)
    _retire_removed_module_resources(
        repository=repository,
        workflow_id=workflow_id,
        source_nodes=source_nodes,
        target_module_names=set(target_nodes),
    )
    return tuple(carried_forward)


def _epoch_module_contracts(snapshots: Sequence[AggregateSnapshot], contracts: ContractArtifactAccess, epoch_id: str):
    nodes = {
        str(item.payload.get("module_name") or item.payload.get("unit_id") or ""): item
        for item in snapshots
        if item.aggregate_type == AggregateType.DAG_NODE_RUN
        and str(item.payload.get("epoch_id") or "") == epoch_id
        and str(item.payload.get("node_kind") or "") == "unit"
    }
    module_contracts = {
        name: (
            dict(node.payload.get("unit_contract_ref") or {}),
            dict(contracts.artifacts.read_json(dict(node.payload.get("unit_contract_ref") or {}))),
        )
        for name, node in nodes.items()
    }
    return nodes, module_contracts
