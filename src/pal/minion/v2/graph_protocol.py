from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


class EdgeKind(StrEnum):
    EXECUTION = "execution"
    CONTRACT = "contract"


class ExecutionAdapter(StrEnum):
    SOFTWARE_GIT = "software_git.v2"
    ARTIFACT_BUNDLE = "artifact_bundle.v2"


@dataclass(frozen=True)
class SourceLocation:
    path: str
    line: int
    column: int

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("source location path is required")
        if self.line < 1 or self.column < 1:
            raise ValueError("source locations are one-based")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
        }


@dataclass(frozen=True)
class GraphSourceMap:
    source_ref: str
    locations: Mapping[str, SourceLocation]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "locations",
            MappingProxyType(dict(self.locations)),
        )

    def location_for(self, semantic_path: str) -> SourceLocation | None:
        return self.locations.get(semantic_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_ref": self.source_ref,
            "locations": {
                key: value.to_dict()
                for key, value in sorted(self.locations.items())
            },
        }


@dataclass(frozen=True)
class RoleBinding:
    participant: str
    profile_id: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.participant not in {"profile", "null"}:
            raise ValueError("role participant must be profile or null")
        if self.participant == "profile" and not self.profile_id:
            raise ValueError("profile role binding requires profile_id")
        if self.participant == "null" and (self.profile_id or not self.reason):
            raise ValueError(
                "null role binding requires a reason and no profile_id"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "participant": self.participant,
            "profile_id": self.profile_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class NodeSpec:
    name: str
    responsibility: str
    satellite_data: Mapping[str, Any]
    producer_binding: RoleBinding
    checker_binding: RoleBinding
    execution_adapter: str
    workspace_policy: Mapping[str, Any]
    output_contract: tuple[str, ...]
    is_sink: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.responsibility:
            raise ValueError("node name and responsibility are required")
        if not self.execution_adapter:
            raise ValueError("node execution_adapter is required")
        if not self.satellite_data:
            raise ValueError("node satellite_data is required")
        object.__setattr__(
            self,
            "satellite_data",
            MappingProxyType(dict(self.satellite_data)),
        )
        object.__setattr__(
            self,
            "workspace_policy",
            MappingProxyType(dict(self.workspace_policy)),
        )
        object.__setattr__(self, "output_contract", tuple(self.output_contract))

    @property
    def semantic_identity_hash(self) -> str:
        return _stable_hash(
            {
                "name": self.name,
                "responsibility": self.responsibility,
            }
        )

    @property
    def contract_hash(self) -> str:
        return _stable_hash(
            {
                "satellite_data": dict(self.satellite_data),
                "output_contract": list(self.output_contract),
                "workspace_policy": dict(self.workspace_policy),
                "execution_adapter": self.execution_adapter,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "responsibility": self.responsibility,
            "satellite_data": dict(self.satellite_data),
            "producer_binding": self.producer_binding.to_dict(),
            "checker_binding": self.checker_binding.to_dict(),
            "execution_adapter": self.execution_adapter,
            "workspace_policy": dict(self.workspace_policy),
            "output_contract": list(self.output_contract),
            "is_sink": self.is_sink,
        }


@dataclass(frozen=True)
class EdgeSpec:
    producer: str
    consumer: str
    kind: EdgeKind
    contract_ref: str
    consumed_outputs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.producer or not self.consumer:
            raise ValueError("edge endpoints are required")
        if self.producer == self.consumer:
            raise ValueError("self edges are forbidden")
        if not self.contract_ref:
            raise ValueError("edge contract_ref is required")
        if not self.consumed_outputs:
            raise ValueError("edge consumed_outputs cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "producer": self.producer,
            "consumer": self.consumer,
            "kind": self.kind.value,
            "contract_ref": self.contract_ref,
            "consumed_outputs": list(self.consumed_outputs),
        }


@dataclass(frozen=True)
class GraphIR:
    graph_id: str
    generation: int
    nodes: Mapping[str, NodeSpec]
    edges: tuple[EdgeSpec, ...]
    sink: str
    source_ref: str
    source_map_ref: str

    def __post_init__(self) -> None:
        if not self.graph_id or self.generation < 1:
            raise ValueError("graph_id and positive generation are required")
        nodes = dict(self.nodes)
        if not nodes or self.sink not in nodes:
            raise ValueError("GraphIR requires nodes and a declared sink")
        if not nodes[self.sink].is_sink:
            raise ValueError("declared GraphIR sink is not marked as sink")
        if not self.source_ref or not self.source_map_ref:
            raise ValueError("GraphIR source and source-map refs are required")
        edge_keys: set[tuple[str, str]] = set()
        for edge in self.edges:
            if edge.producer not in nodes or edge.consumer not in nodes:
                raise ValueError("GraphIR edges must connect declared nodes")
            key = (edge.producer, edge.consumer)
            if key in edge_keys:
                raise ValueError("GraphIR contains a duplicate dependency edge")
            edge_keys.add(key)
        object.__setattr__(self, "nodes", MappingProxyType(nodes))
        object.__setattr__(self, "edges", tuple(self.edges))

    @property
    def generation_hash(self) -> str:
        return _stable_hash(self.to_dict(include_hash=False))

    def incoming(self, node_name: str) -> tuple[EdgeSpec, ...]:
        return tuple(edge for edge in self.edges if edge.consumer == node_name)

    def outgoing(self, node_name: str) -> tuple[EdgeSpec, ...]:
        return tuple(edge for edge in self.edges if edge.producer == node_name)

    def execution_predecessors(self, node_name: str) -> tuple[str, ...]:
        # Software module Coders work from accepted contracts and may run in
        # parallel. The authored sink is the assembly/delivery boundary, so
        # it waits for every produced module before its Candidate begins.
        # Data-oriented adapters retain their authored dependency schedule.
        if (
            node_name == self.sink
            and self.nodes[node_name].execution_adapter == "software_git.v2"
        ):
            return tuple(sorted(set(self.nodes) - {self.sink}))
        return tuple(
            edge.producer
            for edge in self.edges
            if edge.consumer == node_name and edge.kind == EdgeKind.EXECUTION
        )

    def descendants(self, node_name: str) -> tuple[str, ...]:
        """Return executable descendants used for readiness propagation."""

        return self._descendants(node_name, execution_only=True)

    def semantic_descendants(self, node_name: str) -> tuple[str, ...]:
        """Return consumers affected by a provider's semantic output."""

        return self._descendants(node_name, execution_only=False)

    def _descendants(
        self,
        node_name: str,
        *,
        execution_only: bool,
    ) -> tuple[str, ...]:
        seen: set[str] = set()
        pending = [node_name]
        while pending:
            producer = pending.pop()
            for edge in self.edges:
                if edge.producer != producer:
                    continue
                if execution_only and edge.kind != EdgeKind.EXECUTION:
                    continue
                if edge.consumer not in seen:
                    seen.add(edge.consumer)
                    pending.append(edge.consumer)
        seen.discard(node_name)
        return tuple(sorted(seen))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {
            "graph_id": self.graph_id,
            "generation": self.generation,
            "nodes": {
                key: value.to_dict()
                for key, value in sorted(self.nodes.items())
            },
            "edges": [
                edge.to_dict()
                for edge in sorted(
                    self.edges,
                    key=lambda item: (
                        item.producer,
                        item.consumer,
                        item.kind.value,
                    ),
                )
            ],
            "sink": self.sink,
            "source_ref": self.source_ref,
            "source_map_ref": self.source_map_ref,
        }
        if include_hash:
            value["generation_hash"] = _stable_hash(value)
        return value


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def graph_ir_from_mapping(value: Mapping[str, Any]) -> GraphIR:
    payload = dict(value or {})
    raw_nodes = dict(payload.get("nodes") or {})
    nodes: dict[str, NodeSpec] = {}
    for name, raw in raw_nodes.items():
        node = dict(raw or {})
        nodes[str(name)] = NodeSpec(
            name=str(node.get("name") or name),
            responsibility=str(node.get("responsibility") or ""),
            satellite_data=dict(node.get("satellite_data") or {}),
            producer_binding=_role_binding_from_mapping(
                node.get("producer_binding")
            ),
            checker_binding=_role_binding_from_mapping(
                node.get("checker_binding")
            ),
            execution_adapter=str(node.get("execution_adapter") or ""),
            workspace_policy=dict(node.get("workspace_policy") or {}),
            output_contract=tuple(
                str(item) for item in list(node.get("output_contract") or [])
            ),
            is_sink=bool(node.get("is_sink")),
        )
    edges = tuple(
        EdgeSpec(
            producer=str(dict(raw or {}).get("producer") or ""),
            consumer=str(dict(raw or {}).get("consumer") or ""),
            kind=EdgeKind(str(dict(raw or {}).get("kind") or "")),
            contract_ref=str(dict(raw or {}).get("contract_ref") or ""),
            consumed_outputs=tuple(
                str(item)
                for item in list(
                    dict(raw or {}).get("consumed_outputs") or []
                )
            ),
        )
        for raw in list(payload.get("edges") or [])
    )
    graph = GraphIR(
        graph_id=str(payload.get("graph_id") or ""),
        generation=int(payload.get("generation") or 0),
        nodes=nodes,
        edges=edges,
        sink=str(payload.get("sink") or ""),
        source_ref=str(payload.get("source_ref") or ""),
        source_map_ref=str(payload.get("source_map_ref") or ""),
    )
    expected_hash = str(payload.get("generation_hash") or "")
    if expected_hash and expected_hash != graph.generation_hash:
        raise ValueError("GraphIR generation hash mismatch")
    return graph


def _role_binding_from_mapping(value: Any) -> RoleBinding:
    payload = dict(value or {})
    return RoleBinding(
        participant=str(payload.get("participant") or ""),
        profile_id=str(payload.get("profile_id") or ""),
        reason=str(payload.get("reason") or ""),
    )
