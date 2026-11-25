from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Mapping
from pal.bunshin.artifacts import ArtifactRef
from pal.bunshin.contracts import AggregateSnapshot, SubmissionInvariantError
from pal.bunshin.cycle_protocol import NodeCycle, NodeCycleState
from pal.bunshin.graph_protocol import GraphIR


def graph_node_bindings(graph: GraphIR, nodes: Iterable[AggregateSnapshot]) -> dict[str, AggregateSnapshot]:
    """Bind the operational node records to one complete authored generation.

    This is a membership/identity check, not a second readiness calculation.
    In particular, one sink row is not evidence of a fully compiled graph.
    """
    by_name: dict[str, AggregateSnapshot] = {}
    for node in nodes:
        name = str(node.payload.get("module_name") or node.payload.get("unit_id") or "")
        if name not in graph.nodes or name in by_name or node.workflow_id != graph.graph_id:
            raise ValueError(f"invalid or duplicate node binding in graph generation: {name}")
        generation = node.payload.get("graph_generation")
        contract_hash = node.payload.get("graph_contract_hash")
        if generation is not None and int(generation) != graph.generation:
            raise ValueError(f"node belongs to another graph generation: {name}")
        if contract_hash and contract_hash != graph.nodes[name].contract_hash:
            raise ValueError(f"node contract disagrees with GraphIR: {name}")
        if bool(node.payload.get("graph_sink")) != (name == graph.sink):
            raise ValueError(f"node sink binding disagrees with GraphIR: {name}")
        by_name[name] = node
    if set(by_name) != set(graph.nodes):
        raise ValueError(f"graph node bindings are incomplete: {sorted(set(graph.nodes) - set(by_name))}")
    return by_name


@dataclass(frozen=True)
class AcceptedNodeReceipt:
    candidate: ArtifactRef
    candidate_digest: str
    verification: ArtifactRef


def accepted_node_receipt(node: AggregateSnapshot, cycle: NodeCycle) -> AcceptedNodeReceipt:
    """Check the process/effect projection of a semantic acceptance.

    A divergent operational record is a broken commit, not a different
    opinion about whether the node is accepted.
    """
    if cycle.state != NodeCycleState.ACCEPTED or node.state != "ACCEPTED":
        raise SubmissionInvariantError(f"node acceptance receipt is not settled: {cycle.node_name}")
    candidate = ArtifactRef.from_mapping(dict(node.payload.get("candidate_ref") or {}))
    verification = ArtifactRef.from_mapping(dict(node.payload.get("verification_artifact_ref") or {}))
    digest = str(node.payload.get("candidate_digest") or "")
    if not digest or not candidate.sha256 or not verification.sha256 or candidate.sha256 != cycle.accepted_product_ref:
        raise SubmissionInvariantError(f"accepted node receipt disagrees with NodeCycle: {cycle.node_name}")
    return AcceptedNodeReceipt(candidate, digest, verification)


def _topological_module_order(dependencies: Mapping[str, list[str]]) -> list[str]:
    pending = {unit_id: set(values) for unit_id, values in dependencies.items()}
    result: list[str] = []
    while pending:
        ready = sorted(unit_id for unit_id, values in pending.items() if not values)
        if not ready:
            raise ValueError("module identity reconciliation requires an acyclic topology")
        for unit_id in ready:
            result.append(unit_id)
            pending.pop(unit_id)
        for values in pending.values():
            values.difference_update(ready)
    return result


def _ordered_dependency_closure(
    node: AggregateSnapshot,
    node_by_id: Mapping[str, AggregateSnapshot],
) -> tuple[AggregateSnapshot, ...]:
    ordered: list[AggregateSnapshot] = []
    permanent: set[str] = set()
    visiting: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in permanent:
            return
        if node_id in visiting:
            raise ValueError("construction dependency graph contains a cycle")
        dependency = node_by_id.get(node_id)
        if dependency is None:
            raise ValueError(f"dependency node does not exist in epoch: {node_id}")
        visiting.add(node_id)
        for parent_id in sorted(
            str(item) for item in list(dependency.payload.get("dependency_node_ids") or [])
        ):
            visit(parent_id)
        visiting.remove(node_id)
        permanent.add(node_id)
        ordered.append(dependency)

    for dependency_id in sorted(
        str(item) for item in list(node.payload.get("dependency_node_ids") or [])
    ):
        visit(dependency_id)
    return tuple(ordered)


def dependency_fingerprint(node: AggregateSnapshot, node_by_id: Mapping[str, AggregateSnapshot]) -> str:
    dependency_data = []
    for node_id in sorted(str(item) for item in list(node.payload.get("dependency_node_ids") or [])):
        dependency = node_by_id[node_id]
        dependency_data.append(
            {
                "node_id": node_id,
                "candidate_digest": str(dependency.payload.get("candidate_digest") or ""),
                "output_hashes": dict(dependency.payload.get("output_hashes") or {}),
            }
        )
    return hashlib.sha256(json.dumps(dependency_data, sort_keys=True).encode("utf-8")).hexdigest()
