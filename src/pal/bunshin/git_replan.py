from __future__ import annotations
from pal.bunshin.contract_runtime import ContractArtifactAccess
from pal.bunshin.artifacts import ArtifactRef
from pal.bunshin.contracts import AggregateSnapshot, AggregateType
from collections.abc import Sequence
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.graph_executor import GraphDiff, NodeReuseKind
from pal.bunshin.cycle_protocol import NodeCycleState
from pal.bunshin.graph_protocol import GraphIR
from pal.bunshin.execution_values import _action
from pal.bunshin.replan_workspaces import _checkpoint_preserved_module_head
from pal.bunshin.replan_workspaces import _merge_architecture_into_preserved_module
from pal.bunshin.replan_workspaces import _recreate_replaced_module_worktree
from pal.bunshin.replan_workspaces import _retire_removed_module_resources
from pal.bunshin.module_identity_facts import _same_module_identity
from pal.bunshin.replan_workspaces import _snapshot_module_workspace_for_replan
from pal.bunshin.execution_graph_facts import _topological_module_order


def _reconcile_skeleton_module_identities(
    *,
    repository: BunshinRepository,
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
        raise RuntimeError("replan requires the complete target node set")
    staged = repository.cycles.read_graph_execution(workflow_id=workflow_id, generation=target_graph.generation)
    if staged is None:
        raise RuntimeError("replan has no staged graph")
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
        if target_node.state == "ACCEPTED":
            # Preparation replay consumes the committed carry receipt; the
            # activation transaction validates its exact predecessor.
            carried_forward.append(target_node.aggregate_id)
            continue
        if (target_node.payload.get("module_replan_prepared")
                and target_node.payload.get("preserved_from_epoch_id") == source_epoch_id):
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
            and staged.cycles[module_name].state == NodeCycleState.ACCEPTED
            and staged.cycles[module_name].accepted_product_ref == source_candidate_ref.get("sha256")
            and source_node.state == "ACCEPTED"
            and bool(verification_ref)
            and module_head == source_candidate_digest
            and bool(source_node.payload.get("base_sha"))
            and source_node.payload.get("environment_fingerprint") == target_node.payload.get("environment_fingerprint")
            and all(target_nodes[name].state == "ACCEPTED" for name in dependencies[module_name])
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
        target_nodes[module_name] = _carry_verified_module_acceptance(
            repository=repository, contracts=contracts, source=source_node, target=target_node,
            module_head=module_head, actor=actor,
            accepted_dependency_ids=[target_nodes[name].aggregate_id for name in dependencies[module_name]],
        )
        carried_forward.append(target_node.aggregate_id)
    _retire_removed_module_resources(
        repository=repository,
        workflow_id=workflow_id,
        source_nodes=source_nodes,
        target_module_names=set(target_nodes),
    )
    return tuple(carried_forward)


def _carry_verified_module_acceptance(
    *, repository: BunshinRepository, contracts: ContractArtifactAccess,
    source: AggregateSnapshot, target: AggregateSnapshot, module_head: str,
    actor: str, accepted_dependency_ids: list[str],
) -> AggregateSnapshot:
    candidate_ref = _checkpoint_preserved_module_head(
        contracts=contracts, target_node=target,
        source_candidate_ref=dict(source.payload["candidate_ref"]),
        source_candidate_digest=str(source.payload["candidate_digest"]),
        target_contract_ref=dict(target.payload["unit_contract_ref"]), module_head=module_head,
    )
    return repository.transitions.dispatch(_action(
        "CARRY_FORWARD_MODULE", target.workflow_id, AggregateType.DAG_NODE_RUN,
        target.aggregate_id, actor, target.version,
        {
            "candidate_ref": candidate_ref.to_dict(), "candidate_digest": module_head,
            "verification_artifact_ref": source.payload["verification_artifact_ref"],
            "graph_contract_hash": target.payload["graph_contract_hash"],
            "accepted_dependency_node_ids": accepted_dependency_ids, "epoch_frozen": False,
            "output_hashes": dict(source.payload.get("output_hashes") or {}),
            "carried_forward_from_epoch_id": source.payload["epoch_id"],
            "carried_forward_from_node_run_id": source.aggregate_id,
            # Keep the verified product's original integration delta.
            "base_sha": source.payload["base_sha"],
            "base_digest": source.payload.get("base_digest") or source.payload["base_sha"],
            "accepted_dependency_candidate_digests": list(source.payload.get("accepted_dependency_candidate_digests") or []),
            "dependency_output_hashes": {}, "dependency_outputs": {}, "dependency_fingerprint": "",
        },
    )).snapshot


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
