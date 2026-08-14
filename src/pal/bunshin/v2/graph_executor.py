from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from pal.bunshin.v2.cycle_protocol import (
    AssignmentKind,
    CycleAction,
    CycleVerdict,
    CycleSlot,
    NodeCycle,
    NodeCycleState,
)
from pal.bunshin.v2.graph_protocol import EdgeKind, GraphIR


class GraphExecutionState(StrEnum):
    RUNNING = "RUNNING"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class FindingClass(StrEnum):
    MODULE_DEFECT = "module_defect"
    VERIFICATION_DEFECT = "verification_defect"
    DEPENDENCY_DEFECT = "dependency_defect"
    CONTRACT_DEFECT = "contract_defect"
    ARCHITECTURE_DEFECT = "architecture_defect"
    REQUIREMENTS_DEFECT = "requirements_defect"
    SINK_DEFECT = "sink_defect"


class RouteTarget(StrEnum):
    NODE_PRODUCER = "node_producer"
    NODE_CHECKER = "node_checker"
    PLAN_CYCLE = "plan_cycle"


@dataclass(frozen=True)
class FindingRoute:
    target: RouteTarget
    node_name: str = ""
    assignment_kind: AssignmentKind = AssignmentKind.REPAIR
    stale_nodes: tuple[str, ...] = ()


class NodeReuseKind(StrEnum):
    REUSE_ACCEPTED = "reuse_accepted"
    REUSE_STALE = "reuse_stale"
    CREATE = "create"
    RETIRE = "retire"


@dataclass(frozen=True)
class NodeReuseDecision:
    node_name: str
    kind: NodeReuseKind
    reuse_workspace: bool
    reuse_sessions: bool
    reason: str


@dataclass(frozen=True)
class GraphDiff:
    source_generation: int
    target_generation: int
    decisions: Mapping[str, NodeReuseDecision]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decisions",
            MappingProxyType(dict(self.decisions)),
        )


@dataclass(frozen=True)
class GraphExecution:
    graph: GraphIR
    state: GraphExecutionState
    cycles: Mapping[str, NodeCycle]
    published_sink_ref: str = ""
    repair_barriers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if set(self.cycles) != set(self.graph.nodes):
            raise ValueError("GraphExecution cycles must exactly match GraphIR nodes")
        object.__setattr__(self, "cycles", MappingProxyType(dict(self.cycles)))
        barriers = {
            str(name): tuple(str(item) for item in providers)
            for name, providers in dict(self.repair_barriers).items()
        }
        if set(barriers) - set(self.graph.nodes):
            raise ValueError("repair barriers contain an unknown consumer")
        if any(
            provider not in self.graph.nodes
            for providers in barriers.values()
            for provider in providers
        ):
            raise ValueError("repair barriers contain an unknown provider")
        object.__setattr__(
            self,
            "repair_barriers",
            MappingProxyType(barriers),
        )

    @classmethod
    def start(cls, graph: GraphIR) -> "GraphExecution":
        cycles: dict[str, NodeCycle] = {}
        for name in graph.nodes:
            state = (
                NodeCycleState.PRODUCER_READY
                if not graph.producer_predecessors(name)
                else NodeCycleState.BLOCKED
            )
            cycles[name] = NodeCycle(
                cycle_id=f"{graph.graph_id}:g{graph.generation}:{name}",
                node_name=name,
                generation=graph.generation,
                state=state,
            )
        return cls(
            graph=graph,
            state=GraphExecutionState.RUNNING,
            cycles=cycles,
        )

    def runnable_nodes(self) -> tuple[str, ...]:
        if self.state != GraphExecutionState.RUNNING:
            return ()
        ready: list[str] = []
        for name, cycle in self.cycles.items():
            if cycle.state not in {
                NodeCycleState.PRODUCER_READY,
                NodeCycleState.REPAIR_READY,
                NodeCycleState.CHECKER_READY,
            }:
                continue
            predecessors = (
                self.graph.checker_predecessors(name)
                if cycle.state == NodeCycleState.CHECKER_READY
                else self.graph.producer_predecessors(name)
            )
            if all(
                self.cycles[dependency].state == NodeCycleState.ACCEPTED
                for dependency in predecessors
            ) and all(
                self.cycles[provider].state == NodeCycleState.ACCEPTED
                for provider in self.repair_barriers.get(name, ())
            ):
                ready.append(name)
        return tuple(sorted(ready))

    def with_cycle(self, cycle: NodeCycle) -> "GraphExecution":
        if cycle.node_name not in self.cycles:
            raise ValueError(f"unknown graph node: {cycle.node_name}")
        cycles = dict(self.cycles)
        cycles[cycle.node_name] = cycle
        result = replace(self, cycles=cycles)
        return result._refresh_readiness_and_terminal()

    def refresh(self) -> "GraphExecution":
        return self._refresh_readiness_and_terminal()

    def route_finding(
        self,
        *,
        finding_class: FindingClass,
        current_node: str,
        dependency_node: str = "",
    ) -> FindingRoute:
        if current_node not in self.graph.nodes:
            raise ValueError(f"unknown current node: {current_node}")
        if finding_class == FindingClass.MODULE_DEFECT:
            return FindingRoute(
                target=RouteTarget.NODE_PRODUCER,
                node_name=current_node,
                assignment_kind=AssignmentKind.REPAIR,
            )
        if finding_class == FindingClass.VERIFICATION_DEFECT:
            return FindingRoute(
                target=RouteTarget.NODE_CHECKER,
                node_name=current_node,
                assignment_kind=AssignmentKind.RECHECK,
            )
        if finding_class == FindingClass.DEPENDENCY_DEFECT:
            direct_dependencies = set(
                edge.producer for edge in self.graph.incoming(current_node)
            )
            if dependency_node not in direct_dependencies:
                raise ValueError(
                    "dependency defect must name a direct declared provider"
                )
            stale = {
                current_node,
                *self.graph.semantic_descendants(current_node),
                *self.graph.semantic_descendants(dependency_node),
            }
            stale.discard(dependency_node)
            return FindingRoute(
                target=RouteTarget.NODE_PRODUCER,
                node_name=dependency_node,
                assignment_kind=AssignmentKind.REPAIR,
                stale_nodes=tuple(sorted(stale)),
            )
        if finding_class in {
            FindingClass.CONTRACT_DEFECT,
            FindingClass.ARCHITECTURE_DEFECT,
            FindingClass.REQUIREMENTS_DEFECT,
        }:
            return FindingRoute(
                target=RouteTarget.PLAN_CYCLE,
                assignment_kind=AssignmentKind.REVISION,
                stale_nodes=tuple(sorted(self.graph.nodes)),
            )
        if finding_class == FindingClass.SINK_DEFECT:
            if current_node != self.graph.sink:
                raise ValueError(
                    "sink defects may only be reported by the declared sink checker"
                )
            return FindingRoute(
                target=RouteTarget.NODE_PRODUCER,
                node_name=self.graph.sink,
                assignment_kind=AssignmentKind.REPAIR,
            )
        raise ValueError(f"unsupported finding class: {finding_class}")

    def apply_checker_verdict(
        self,
        *,
        current_node: str,
        accepted: bool,
        finding_refs: tuple[str, ...] = (),
        finding_class: FindingClass | None = None,
        dependency_node: str = "",
        accepted_product_ref: str = "",
    ) -> tuple["GraphExecution", FindingRoute | None]:
        """Close one checker assignment and route a rejection mechanically.

        Findings carry semantics; the executor owns graph traversal.  Role
        workers never choose another worker, mutate downstream state, or
        invent a repair target.
        """

        cycle = self.cycles[current_node]
        verdict = CycleVerdict(
            accepted=accepted,
            generation=cycle.generation,
            finding_refs=finding_refs,
        )
        if accepted:
            updated = cycle.transition(
                CycleAction.CHECKER_ACCEPTED,
                verdict=verdict,
            )
            if accepted_product_ref:
                updated = replace(
                    updated,
                    accepted_product_ref=accepted_product_ref,
                )
            return self.with_cycle(updated), None
        if finding_class is None:
            raise ValueError("a rejected checker verdict requires a finding class")
        route = self.route_finding(
            finding_class=finding_class,
            current_node=current_node,
            dependency_node=dependency_node,
        )
        action = (
            CycleAction.CHECKER_RETRY
            if route.target == RouteTarget.NODE_CHECKER
            else CycleAction.CHECKER_REJECTED
        )
        result = self.with_cycle(
            cycle.transition(action, verdict=verdict)
        )
        if route.target == RouteTarget.NODE_CHECKER:
            return result, route
        if route.target == RouteTarget.PLAN_CYCLE:
            return replace(
                result._mark_nodes_stale(
                    route.stale_nodes,
                    allow_active=True,
                ),
                state=GraphExecutionState.REPLAN_REQUIRED,
            ), route
        cycles = dict(result.cycles)
        target = cycles[route.node_name]
        if route.node_name != current_node:
            target = _repair_ready(target)
            cycles[route.node_name] = target
        result = replace(result, cycles=cycles)
        if finding_class == FindingClass.DEPENDENCY_DEFECT:
            barriers = dict(result.repair_barriers)
            for stale_name in route.stale_nodes:
                barriers[stale_name] = tuple(
                    sorted(
                        {
                            *barriers.get(stale_name, ()),
                            route.node_name,
                        }
                    )
                )
            result = replace(result, repair_barriers=barriers)
        return result._mark_nodes_stale(route.stale_nodes), route

    def _mark_nodes_stale(
        self,
        node_names: tuple[str, ...],
        *,
        allow_active: bool = False,
    ) -> "GraphExecution":
        cycles = dict(self.cycles)
        for name in node_names:
            cycle = cycles.get(name)
            if cycle is None:
                continue
            if cycle.state == NodeCycleState.ACCEPTED:
                cycles[name] = cycle.transition(CycleAction.MARK_STALE)
            elif cycle.state in {
                NodeCycleState.PRODUCER_READY,
                NodeCycleState.REPAIR_READY,
                NodeCycleState.CHECKER_READY,
                NodeCycleState.BLOCKED,
                NodeCycleState.STALE,
            }:
                cycles[name] = replace(
                    cycle,
                    state=NodeCycleState.STALE,
                    active_assignment=None,
                    last_verdict=None,
                )
            elif allow_active and cycle.is_running:
                # REPLAN_REQUIRED closes graph admission immediately. The
                # process owner then quiesces/reaps this incarnation; the next
                # GraphIR generation creates the replacement cycle. Never
                # counterfeit a quiescent state while the process is live.
                continue
            else:
                raise ValueError(
                    f"cannot stale active node cycle {name} from {cycle.state.value}; "
                    "quiesce its process incarnation first"
                )
        return replace(self, cycles=cycles)

    def _refresh_readiness_and_terminal(self) -> "GraphExecution":
        cycles = dict(self.cycles)
        changed = True
        while changed:
            changed = False
            for name, cycle in list(cycles.items()):
                if cycle.state not in {
                    NodeCycleState.BLOCKED,
                    NodeCycleState.STALE,
                }:
                    continue
                if all(
                    cycles[dependency].state == NodeCycleState.ACCEPTED
                    for dependency in self.graph.producer_predecessors(name)
                ) and all(
                    cycles[provider].state == NodeCycleState.ACCEPTED
                    for provider in self.repair_barriers.get(name, ())
                ):
                    cycles[name] = replace(
                        cycle,
                        state=NodeCycleState.PRODUCER_READY,
                    )
                    changed = True
        barriers = {
            name: providers
            for name, providers in self.repair_barriers.items()
            if not all(
                cycles[provider].state == NodeCycleState.ACCEPTED
                for provider in providers
            )
        }
        sink_cycle = cycles[self.graph.sink]
        if sink_cycle.state == NodeCycleState.ACCEPTED:
            if any(
                cycle.state != NodeCycleState.ACCEPTED
                for cycle in cycles.values()
            ):
                raise ValueError(
                    "sink accepted before all executable predecessors closed"
                )
            return replace(
                self,
                cycles=cycles,
                repair_barriers=barriers,
                state=GraphExecutionState.COMPLETED,
                published_sink_ref=sink_cycle.accepted_product_ref,
            )
        return replace(self, cycles=cycles, repair_barriers=barriers)


def _repair_ready(cycle: NodeCycle) -> NodeCycle:
    if cycle.state == NodeCycleState.ACCEPTED:
        cycle = cycle.transition(CycleAction.MARK_STALE)
    if cycle.state == NodeCycleState.STALE:
        cycle = cycle.transition(CycleAction.UNBLOCK)
    if cycle.state == NodeCycleState.PRODUCER_READY:
        return replace(cycle, state=NodeCycleState.REPAIR_READY)
    if cycle.state == NodeCycleState.REPAIR_READY:
        return cycle
    raise ValueError(
        f"repair target must be quiescent, got {cycle.state.value}"
    )


def diff_graphs(source: GraphIR, target: GraphIR) -> GraphDiff:
    if target.generation <= source.generation:
        raise ValueError("target graph generation must advance")
    decisions: dict[str, NodeReuseDecision] = {}
    source_names = set(source.nodes)
    target_names = set(target.nodes)
    for name in sorted(source_names - target_names):
        decisions[name] = NodeReuseDecision(
            node_name=name,
            kind=NodeReuseKind.RETIRE,
            reuse_workspace=False,
            reuse_sessions=False,
            reason="node was deleted",
        )
    for name in sorted(target_names - source_names):
        decisions[name] = NodeReuseDecision(
            node_name=name,
            kind=NodeReuseKind.CREATE,
            reuse_workspace=False,
            reuse_sessions=False,
            reason="node is new",
        )
    for name in sorted(source_names & target_names):
        old = source.nodes[name]
        new = target.nodes[name]
        if old.semantic_identity_hash != new.semantic_identity_hash:
            decisions[name] = NodeReuseDecision(
                node_name=name,
                kind=NodeReuseKind.CREATE,
                reuse_workspace=False,
                reuse_sessions=False,
                reason="responsibility changed",
            )
            continue
        unchanged = (
            old.contract_hash == new.contract_hash
            and _incoming_signature(source, name)
            == _incoming_signature(target, name)
        )
        decisions[name] = NodeReuseDecision(
            node_name=name,
            kind=(
                NodeReuseKind.REUSE_ACCEPTED
                if unchanged
                else NodeReuseKind.REUSE_STALE
            ),
            reuse_workspace=True,
            reuse_sessions=True,
            reason=(
                "responsibility, contract, and incoming edges are unchanged"
                if unchanged
                else "responsibility is unchanged but contract or edges changed"
            ),
        )
    # Acceptance is a property of a product built against one complete input
    # boundary, not merely of the node's own name.  Invalidate consumers
    # transitively whenever a semantic or execution predecessor cannot carry
    # its acceptance into the target generation.  For software graphs the
    # authored sink's synthetic execution predecessors intentionally include
    # every executable node.
    changed = True
    while changed:
        changed = False
        for name in sorted(target_names):
            decision = decisions[name]
            if decision.kind != NodeReuseKind.REUSE_ACCEPTED:
                continue
            predecessors = {
                edge.producer for edge in target.incoming(name)
            }
            predecessors.update(target.execution_predecessors(name))
            if any(
                decisions[provider].kind != NodeReuseKind.REUSE_ACCEPTED
                for provider in predecessors
            ):
                decisions[name] = NodeReuseDecision(
                    node_name=name,
                    kind=NodeReuseKind.REUSE_STALE,
                    reuse_workspace=True,
                    reuse_sessions=True,
                    reason="an accepted predecessor cannot be carried forward",
                )
                changed = True
    return GraphDiff(
        source_generation=source.generation,
        target_generation=target.generation,
        decisions=decisions,
    )


def _incoming_signature(graph: GraphIR, node_name: str) -> str:
    value = [
        {
            "producer": edge.producer,
            "kind": edge.kind.value,
            "contract_ref": edge.contract_ref,
            "consumed_outputs": list(edge.consumed_outputs),
        }
        for edge in graph.incoming(node_name)
        if edge.kind in {EdgeKind.EXECUTION, EdgeKind.CONTRACT}
    ]
    return hashlib.sha256(
        json.dumps(
            sorted(value, key=lambda item: (item["producer"], item["kind"])),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
