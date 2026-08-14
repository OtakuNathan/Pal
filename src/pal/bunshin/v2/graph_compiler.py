from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from pal.bunshin.v2.contract_protocol import ContractDocument
from pal.bunshin.v2.graph_satellites import (
    FamilyGraphSatelliteProjection,
    WorkspaceAuthorityProjectionError,
    apply_workspace_authority_rules,
)
from pal.bunshin.v2.graph_protocol import (
    EdgeKind,
    EdgeSpec,
    GraphIR,
    GraphSourceMap,
    NodeSpec,
    RoleBinding,
    SourceLocation,
)


_SEMANTIC_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class GraphCompilationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        semantic_path: str = "",
        location: SourceLocation | None = None,
    ) -> None:
        self.semantic_path = semantic_path
        self.location = location
        detail = message
        if semantic_path:
            detail = f"{semantic_path}: {detail}"
        if location is not None:
            detail += f" ({location.path}:{location.line}:{location.column})"
        super().__init__(detail)


@dataclass(frozen=True)
class GraphCompileBindings:
    producer: RoleBinding
    checker: RoleBinding
    execution_adapter: str


@dataclass(frozen=True)
class GraphCompiler:
    """Compile validated Family authoring into immutable execution GraphIR.

    The compiler validates topology and binding invariants. It never invents
    semantic nodes: in particular the terminal assembly/delivery node must be
    authored as an ordinary produced module and named by ``graph.sink``.
    """

    def compile(
        self,
        document: ContractDocument | Mapping[str, Any],
        *,
        graph_id: str,
        generation: int,
        bindings: GraphCompileBindings,
        satellite_projector: FamilyGraphSatelliteProjection,
        source_ref: str,
        workspace_authority_rules: tuple[Mapping[str, Any], ...],
        source_map: GraphSourceMap | None = None,
        source_map_ref: str = "",
    ) -> GraphIR:
        payload = (
            document.model_dump(mode="python")
            if isinstance(document, ContractDocument)
            else copy.deepcopy(dict(document))
        )
        graph = dict(payload.get("graph") or {})
        sink = str(graph.get("sink") or "").strip()
        if not sink:
            self._fail("graph.sink is required", "graph.sink", source_map)
        modules = {
            str(name): dict(value or {})
            for name, value in dict(payload.get("modules") or {}).items()
        }
        if sink not in modules:
            self._fail(
                f"declared sink {sink!r} is not a module",
                "graph.sink",
                source_map,
            )
        produced_names = {
            name
            for name, module in modules.items()
            if str(module.get("execution") or "") == "produce"
        }
        if sink not in produced_names:
            self._fail(
                "the sink must be an executable produced module",
                f"modules.{sink}.execution",
                source_map,
            )
        projections = {
            name: satellite_projector.project(
                document=payload,
                node_name=name,
                node=modules[name],
            )
            for name in sorted(produced_names)
        }
        try:
            projections = apply_workspace_authority_rules(
                document=payload,
                projections=projections,
                rules=workspace_authority_rules,
            )
        except WorkspaceAuthorityProjectionError as exc:
            semantic_path = _workspace_authority_semantic_path(
                exc,
                workspace_authority_rules,
            )
            self._fail(str(exc), semantic_path, source_map)
        nodes: dict[str, NodeSpec] = {}
        for name in sorted(produced_names):
            module = modules[name]
            projection = projections[name]
            dependencies = {
                str(provider): dict(dependency or {})
                for provider, dependency in sorted(
                    dict(module.get("dependencies") or {}).items()
                )
            }
            nodes[name] = NodeSpec(
                name=name,
                responsibility=str(module.get("responsibility") or "").strip(),
                satellite_data=projection.satellite_data,
                producer_binding=bindings.producer,
                checker_binding=bindings.checker,
                execution_adapter=bindings.execution_adapter,
                workspace_policy=projection.workspace_policy,
                output_contract=tuple(
                    str(item)
                    for item in list(module.get("provides") or [])
                ),
                is_sink=name == sink,
            )
        edges: list[EdgeSpec] = []
        seen_edges: set[tuple[str, str]] = set()
        for consumer_name, module in sorted(modules.items()):
            if consumer_name not in produced_names:
                continue
            for producer_name, raw_dependency in sorted(
                dict(module.get("dependencies") or {}).items()
            ):
                path = f"modules.{consumer_name}.dependencies.{producer_name}"
                if producer_name not in modules:
                    self._fail(
                        f"unknown provider module {producer_name!r}",
                        path,
                        source_map,
                    )
                # Contract-only declarations have no executable GraphIR
                # vertex.  Their Family-owned semantics are present in the
                # opaque node satellite data, but they are not schedulable
                # predecessors or repair targets themselves.
                if producer_name not in produced_names:
                    continue
                key = (producer_name, consumer_name)
                if key in seen_edges:
                    self._fail("duplicate dependency edge", path, source_map)
                seen_edges.add(key)
                dependency = dict(raw_dependency or {})
                consumed = tuple(
                    str(item) for item in list(dependency.get("consumes") or [])
                )
                provided = set(modules[producer_name].get("provides") or [])
                missing_outputs = sorted(set(consumed) - provided)
                if missing_outputs:
                    self._fail(
                        "dependency consumes outputs not provided by "
                        f"{producer_name}: {', '.join(missing_outputs)}",
                        f"{path}.consumes",
                        source_map,
                    )
                edges.append(
                    EdgeSpec(
                        producer=producer_name,
                        consumer=consumer_name,
                        kind=(
                            EdgeKind.EXECUTION
                            if (
                                bindings.execution_adapter
                                != "software_git.v2"
                                or consumer_name == sink
                            )
                            else EdgeKind.CONTRACT
                        ),
                        contract_ref=path,
                        consumed_outputs=consumed,
                    )
                )
        execution_edges = [
            edge for edge in edges if edge.kind == EdgeKind.EXECUTION
        ]
        self._assert_acyclic(nodes, edges, source_map)
        outgoing_from_sink = [
            edge for edge in execution_edges if edge.producer == sink
        ]
        if outgoing_from_sink:
            self._fail(
                "the declared sink cannot feed another executable node",
                "graph.sink",
                source_map,
            )
        unreachable = sorted(
            name
            for name in nodes
            if name != sink
            and sink not in _reachable_from(name, edges)
        )
        if unreachable:
            self._fail(
                "every executable node must reach the declared sink; "
                "unconnected nodes: " + ", ".join(unreachable),
                "graph.sink",
                source_map,
            )
        effective_source_map = source_map or GraphSourceMap(
            source_ref=source_ref,
            locations={},
        )
        return GraphIR(
            graph_id=graph_id,
            generation=generation,
            nodes=nodes,
            edges=tuple(edges),
            sink=sink,
            source_ref=source_ref,
            source_map_ref=(
                source_map_ref
                or _source_map_fingerprint(effective_source_map)
            ),
        )

    @staticmethod
    def _assert_acyclic(
        nodes: Mapping[str, NodeSpec],
        edges: list[EdgeSpec],
        source_map: GraphSourceMap | None,
    ) -> None:
        incoming = {name: 0 for name in nodes}
        outgoing: dict[str, list[str]] = {name: [] for name in nodes}
        for edge in edges:
            incoming[edge.consumer] += 1
            outgoing[edge.producer].append(edge.consumer)
        ready = sorted(name for name, count in incoming.items() if count == 0)
        visited: list[str] = []
        while ready:
            current = ready.pop(0)
            visited.append(current)
            for consumer in sorted(outgoing[current]):
                incoming[consumer] -= 1
                if incoming[consumer] == 0:
                    ready.append(consumer)
                    ready.sort()
        if len(visited) != len(nodes):
            cyclic = sorted(name for name, count in incoming.items() if count)
            location = (
                source_map.location_for(f"modules.{cyclic[0]}.dependencies")
                if source_map and cyclic
                else None
            )
            raise GraphCompilationError(
                "executable dependency graph contains a cycle: "
                + ", ".join(cyclic),
                semantic_path="modules",
                location=location,
            )

    @staticmethod
    def _fail(
        message: str,
        semantic_path: str,
        source_map: GraphSourceMap | None,
    ) -> None:
        raise GraphCompilationError(
            message,
            semantic_path=semantic_path,
            location=(
                source_map.location_for(semantic_path) if source_map else None
            ),
        )


def build_yaml_source_map(path: Path) -> GraphSourceMap:
    source = Path(path).expanduser().resolve()
    root = yaml.compose(source.read_text(encoding="utf-8"))
    locations: dict[str, SourceLocation] = {}

    def visit(node: yaml.Node, semantic_path: str) -> None:
        if semantic_path:
            locations[semantic_path] = SourceLocation(
                path=str(source),
                line=node.start_mark.line + 1,
                column=node.start_mark.column + 1,
            )
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                key = str(getattr(key_node, "value", ""))
                child_path = f"{semantic_path}.{key}" if semantic_path else key
                locations[child_path] = SourceLocation(
                    path=str(source),
                    line=key_node.start_mark.line + 1,
                    column=key_node.start_mark.column + 1,
                )
                visit(value_node, child_path)
        elif isinstance(node, yaml.SequenceNode):
            for index, child in enumerate(node.value):
                visit(child, f"{semantic_path}[{index}]")

    if root is not None:
        visit(root, "")
    return GraphSourceMap(source_ref=str(source), locations=locations)


def _workspace_authority_semantic_path(
    exc: WorkspaceAuthorityProjectionError,
    rules: tuple[Mapping[str, Any], ...],
) -> str:
    message = str(exc)
    candidates = [
        dict(rule)
        for rule in rules
        if (
            len(rules) == 1
            or f"workspace authority {str(dict(rule).get('id') or '')}" in message
        )
    ]
    if len(candidates) != 1:
        return "context"
    pointer = str(candidates[0].get("property_pointer") or "")
    if not pointer.startswith("/"):
        return "context"
    return ".".join(
        token.replace("~1", "/").replace("~0", "~")
        for token in pointer.split("/")[1:]
    ) or "context"


def _reachable_from(start: str, edges: list[EdgeSpec]) -> set[str]:
    reached: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        for edge in edges:
            if edge.producer != current or edge.consumer in reached:
                continue
            reached.add(edge.consumer)
            pending.append(edge.consumer)
    return reached


def _source_map_fingerprint(source_map: GraphSourceMap) -> str:
    import hashlib
    import json

    semantic_locations = {
        path: {
            "line": location.line,
            "column": location.column,
        }
        for path, location in sorted(source_map.locations.items())
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(
            semantic_locations,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
