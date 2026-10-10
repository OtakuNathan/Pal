from __future__ import annotations
import hashlib
import json
from typing import Mapping
from pal.bunshin.contracts import AggregateSnapshot


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
