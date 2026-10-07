from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from pal.bunshin.cycle_protocol import (
    AssignmentKind,
    CycleAction,
    CycleVerdict,
    CycleSlot,
    NodeCycle,
    NodeCycleState,
)
from pal.bunshin.graph_protocol import EdgeKind, GraphIR
from pal.bunshin.dependency_repair_protocol import (
    DependencyRepairCohort,
    DependencyRepairDeferred,
    DependencyRepairLedger,
    RepairClosure,
    RepairIncarnation,
    RepairIntent,
)


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
    node_names: tuple[str, ...] = ()


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
    dependency_repairs: DependencyRepairLedger = field(default_factory=DependencyRepairLedger)

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
        for cohort in (*self.dependency_repairs.history,
                       *((self.dependency_repairs.pending,) if self.dependency_repairs.pending else ())):
            if set(cohort.scope) - set(self.graph.nodes):
                raise ValueError("dependency repair scope contains unknown graph nodes")
            expected_scope: set[str] = set()
            by_node = {item.node_name: item for item in cohort.frontier.values()}
            for intent in cohort.intents.values():
                if (intent.graph_id, intent.generation, intent.generation_hash) != (
                    self.graph.graph_id, self.graph.generation, self.graph.generation_hash,
                ):
                    raise ValueError("dependency repair belongs to another graph generation")
                eligible = set(self.graph.checker_predecessors(intent.source_node))
                if any(name not in eligible
                       or self.graph.nodes[name].producer_binding.participant != "profile"
                       or self.graph.nodes[name].execution_adapter != "software_git.v2"
                       for name in intent.provider_nodes):
                    raise ValueError("stored dependency repair has an ineligible provider")
                for provider in intent.provider_nodes:
                    expected_scope.add(provider)
                    expected_scope.update(self._dependency_consumers(intent.source_node, provider))
                source = by_node.get(intent.source_node)
                if (source is None or source.cycle_id != intent.source_cycle_id
                        or source.aggregate_id != intent.source_aggregate_id
                        or source.role_assignment_id != intent.source_assignment_id
                        or source.input_fingerprint != intent.source_input_fingerprint
                        or source.fencing_token != intent.source_fencing_token
                        or source.slot != CycleSlot.CHECKER.value):
                    raise ValueError("stored repair source does not match its frozen receipt")
            if set(cohort.scope) != expected_scope:
                raise ValueError("stored dependency repair scope omits its actual closure")
        if self.dependency_repairs.pending:
            if self.state in {GraphExecutionState.CANCELLED, GraphExecutionState.REPLAN_REQUIRED}:
                object.__setattr__(self, "dependency_repairs", self.dependency_repairs.archive(
                    status="superseded", reason=self.state.value,
                ))
            elif self.state == GraphExecutionState.COMPLETED or self.published_sink_ref:
                raise ValueError("pending dependency repair forbids terminal publication")

    @property
    def pending_scope(self) -> frozenset[str]:
        return self.dependency_repairs.fenced_nodes

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
            if name in self.pending_scope:
                continue
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
        previous = self.cycles[cycle.node_name]
        if cycle.node_name in self.pending_scope and cycle != previous:
            if (cycle.state in {NodeCycleState.ACCEPTED, NodeCycleState.CHECKER_READY,
                               NodeCycleState.REPAIR_READY, NodeCycleState.PRODUCER_READY}
                    or (cycle.active_assignment is not None
                        and cycle.active_assignment != previous.active_assignment)):
                raise ValueError("pending dependency repair fences this graph admission or verdict")
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
        dependency_nodes: tuple[str, ...] = (),
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
            providers = tuple(dict.fromkeys(
                ((dependency_node,) if dependency_node else ())
                + dependency_nodes
            ))
            eligible = set(self.graph.checker_predecessors(current_node))
            if not providers or any(name not in eligible for name in providers):
                raise ValueError(
                    "dependency defect must name a checker execution provider"
                )
            stale = set().union(
                *(self._dependency_consumers(current_node, name)
                  for name in providers)
            )
            # A target can also consume another target. Its repair must not
            # be overwritten by the consumer invalidation of that provider.
            stale.difference_update(providers)
            return FindingRoute(
                target=RouteTarget.NODE_PRODUCER,
                node_name=providers[0],
                assignment_kind=AssignmentKind.REPAIR,
                stale_nodes=tuple(sorted(stale)),
                node_names=providers,
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

    def _dependency_consumers(
        self, current_node: str, provider: str,
    ) -> set[str]:
        return {
            current_node,
            *self.graph.repair_descendants(current_node),
            *self.graph.repair_descendants(provider),
        } - {provider}

    def register_dependency_repair(
        self, intent: RepairIntent, *, frontier: tuple[RepairIncarnation, ...] = (),
    ) -> "GraphExecution":
        """Freeze an actual scope and join eligible reports before any repair.

        The caller supplies every admitted runtime/setup owner in the newly
        affected scope under the admission write lock. A logical CHECKING
        source is kept intact until its exact closure proof arrives.
        """
        previous = self.dependency_repairs.find_intent(intent.key)
        if previous is not None:
            if previous != intent:
                raise ValueError("repair intent identity is already bound to changed content")
            return self
        if self.state != GraphExecutionState.RUNNING:
            raise ValueError("dependency repair cannot register in a terminal graph")
        if (intent.graph_id, intent.generation, intent.generation_hash) != (
            self.graph.graph_id, self.graph.generation, self.graph.generation_hash,
        ):
            raise ValueError("dependency repair intent belongs to another graph generation")
        if intent.source_node not in self.cycles:
            raise ValueError("dependency repair source is not a graph node")
        source = self.cycles[intent.source_node]
        if (source.cycle_id != intent.source_cycle_id or source.generation != intent.generation
                or source.product_ref != intent.source_product_ref):
            raise ValueError("dependency repair source candidate or cycle identity changed")
        eligible = set(self.graph.checker_predecessors(intent.source_node))
        if any(name not in eligible or self.graph.nodes[name].producer_binding.participant != "profile"
               or self.graph.nodes[name].execution_adapter != "software_git.v2"
               for name in intent.provider_nodes):
            raise ValueError("dependency repair requires a software-produced checker execution provider")
        scope = set(intent.provider_nodes)
        for provider in intent.provider_nodes:
            scope.update(self._dependency_consumers(intent.source_node, provider))
        pending = self.dependency_repairs.pending
        if pending is not None:
            if (intent.source_node not in pending.scope
                    and not ((scope & set(pending.scope)) - {self.graph.sink})):
                raise DependencyRepairDeferred("independent outside-scope report requires a later cohort")
            if any(item.key not in pending.frontier and item.node_name in pending.scope
                   for item in frontier):
                raise ValueError("repair frontier was already frozen for this node")
            pending = replace(pending, intents={**pending.intents, intent.key: intent},
                              scope=tuple(sorted(scope | set(pending.scope))))
        else:
            pending = DependencyRepairCohort(intents={intent.key: intent}, scope=tuple(sorted(scope)))
        pending = pending.with_frontier(frontier)
        self._validate_repair_frontier(pending)
        sources = [item for item in pending.frontier.values() if item.node_name == intent.source_node]
        if len(sources) != 1 or (
            sources[0].cycle_id != intent.source_cycle_id
            or sources[0].aggregate_id != intent.source_aggregate_id
            or sources[0].role_assignment_id != intent.source_assignment_id
            or sources[0].input_fingerprint != intent.source_input_fingerprint
            or sources[0].fencing_token != intent.source_fencing_token
            or sources[0].slot != CycleSlot.CHECKER.value
        ):
            raise ValueError("repair source receipt does not match its frozen incarnation")
        if source.active_assignment is None and sources[0].key not in pending.closures:
            raise ValueError("repair source has no admitted checker or exact closed receipt")
        cycles = dict(self.cycles)
        for item in pending.frontier.values():
            cycle = cycles[item.node_name]
            if item.key in pending.closures or item.node_name == intent.source_node:
                continue
            if cycle.state != NodeCycleState.CANCEL_REQUESTED:
                cycles[item.node_name] = cycle.transition(CycleAction.REQUEST_STALE)
            elif cycle.resume_state != NodeCycleState.STALE:
                raise ValueError("terminal user cancellation dominates dependency repair")
        return replace(self, cycles=cycles, published_sink_ref="",
                       dependency_repairs=replace(self.dependency_repairs, pending=pending))

    def _validate_repair_frontier(self, pending: DependencyRepairCohort) -> None:
        by_node = {item.node_name: item for item in pending.frontier.values()}
        for name in pending.scope:
            cycle = self.cycles[name]
            item = by_node.get(name)
            if (cycle.active_assignment is not None or not cycle.is_quiescent) and item is None:
                raise ValueError(f"repair frontier omitted admitted node {name}")
            if item is None:
                continue
            if item.cycle_id != cycle.cycle_id or item.generation != cycle.generation:
                raise ValueError("repair frontier belongs to another cycle incarnation")
            if item.key in pending.closures:
                if cycle.state != NodeCycleState.STALE:
                    raise ValueError("closed repair incarnation was replaced")
            elif cycle.active_assignment is not None and (
                cycle.active_assignment.input_fingerprint != item.input_fingerprint
                or cycle.active_assignment.slot.value != item.slot
            ):
                raise ValueError("repair frontier does not match the admitted graph assignment")
            elif cycle.state == NodeCycleState.CANCELLED:
                raise ValueError("terminal user cancellation dominates dependency repair")

    def update_dependency_repair_frontier(
        self, frontier: tuple[RepairIncarnation, ...],
    ) -> "GraphExecution":
        pending = self._pending_dependency_repair()
        if any(item.key not in pending.frontier for item in frontier):
            raise ValueError("repair frontier cannot admit another incarnation after freeze")
        updated = pending.with_frontier(frontier)
        # Updating binds ownership discovered while retiring an already
        # admitted setup. Scope expansion is only allowed by registration.
        self._validate_repair_frontier(updated)
        return replace(self, dependency_repairs=replace(self.dependency_repairs, pending=updated))

    def capture_dependency_repair(
        self, incarnation_key: str, *, receipt_refs: tuple[Mapping[str, Any], ...] = (),
    ) -> "GraphExecution":
        pending = self._pending_dependency_repair().capture(incarnation_key, receipt_refs)
        return replace(self, dependency_repairs=replace(self.dependency_repairs, pending=pending))

    def close_dependency_repair(self, proof: RepairClosure) -> "GraphExecution":
        pending = self._pending_dependency_repair()
        self._validate_repair_frontier(pending)
        updated = pending.close(proof)
        if proof.incarnation.key in pending.closures:
            return self
        cycle = self.cycles[proof.incarnation.node_name]
        if cycle.state != NodeCycleState.CANCEL_REQUESTED:
            cycle = cycle.transition(CycleAction.REQUEST_STALE)
        cycle = cycle.transition(CycleAction.STALE_CONFIRMED, assignment=cycle.active_assignment)
        return replace(self, cycles={**self.cycles, cycle.node_name: cycle},
                       dependency_repairs=replace(self.dependency_repairs, pending=updated))

    def _pending_dependency_repair(self) -> DependencyRepairCohort:
        if self.state != GraphExecutionState.RUNNING or self.dependency_repairs.pending is None:
            raise ValueError("dependency repair requires a current pending cohort")
        return self.dependency_repairs.pending

    def apply_dependency_repair(
        self, *, cohort_key: str,
    ) -> tuple["GraphExecution", FindingRoute | None]:
        """Seal the re-read component and release its repairs atomically."""
        for item in self.dependency_repairs.history:
            if item.key == cohort_key:
                if item.status == "applied":
                    return self, None
                raise ValueError("superseded dependency repair cannot apply")
        pending = self._pending_dependency_repair()
        if pending.key != cohort_key:
            raise ValueError("dependency repair component changed before seal")
        self._validate_repair_frontier(pending)
        if not pending.ready:
            raise ValueError("dependency repair frontier has not been captured and closed")
        for intent in pending.intents.values():
            member = next(item for item in pending.frontier.values() if item.node_name == intent.source_node)
            if intent.packet_sha256 not in {ref["sha256"] for ref in pending.captures.get(member.key, ())}:
                raise ValueError("dependency repair source packet has not been captured")
        providers = pending.providers
        consumers = tuple(sorted(set(pending.scope) - set(providers)))
        cycles = dict(self.cycles)
        for name in providers:
            cycles[name] = _repair_ready(cycles[name])
            refs = tuple(sorted({intent.packet_sha256 for intent in pending.intents.values()
                                 if name in intent.provider_nodes}))
            cycles[name] = replace(cycles[name], last_verdict=CycleVerdict(
                accepted=False, generation=cycles[name].generation, finding_refs=refs,
            ))
        # No readiness refresh is allowed between provider and consumer writes.
        result = replace(self, cycles=cycles)._mark_nodes_stale(consumers)
        barriers = {name: values for name, values in self.repair_barriers.items()
                    if name not in providers}
        for provider in providers:
            affected = set(self.graph.repair_descendants(provider))
            for consumer in consumers:
                if consumer in affected:
                    barriers[consumer] = tuple(sorted({*barriers.get(consumer, ()), provider}))
        result = replace(result, repair_barriers=barriers, published_sink_ref="",
                         dependency_repairs=self.dependency_repairs.archive(status="applied"))
        route = FindingRoute(target=RouteTarget.NODE_PRODUCER, node_name=providers[0],
                             node_names=providers, assignment_kind=AssignmentKind.REPAIR,
                             stale_nodes=consumers)
        return result, route

    def supersede_dependency_repairs(self, reason: str) -> "GraphExecution":
        if not reason:
            raise ValueError("superseding dependency repair requires a reason")
        if self.state not in {GraphExecutionState.CANCELLED, GraphExecutionState.REPLAN_REQUIRED}:
            raise ValueError("supersession requires terminal cancellation or replan admission fence")
        return replace(self, dependency_repairs=self.dependency_repairs.archive(
            status="superseded", reason=reason,
        ))

    def apply_checker_verdict(
        self,
        *,
        current_node: str,
        accepted: bool,
        finding_refs: tuple[str, ...] = (),
        finding_class: FindingClass | None = None,
        dependency_node: str = "",
        dependency_nodes: tuple[str, ...] = (),
        accepted_product_ref: str = "",
    ) -> tuple["GraphExecution", FindingRoute | None]:
        """Close one checker assignment and route a rejection mechanically.

        Findings carry semantics; the executor owns graph traversal.  Role
        workers never choose another worker, mutate downstream state, or
        invent a repair target.
        """

        cycle = self.cycles[current_node]
        if current_node in self.pending_scope:
            raise ValueError("pending dependency repair captures verdicts without publishing them")
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
            dependency_nodes=dependency_nodes,
        )
        # Validate the complete batch before closing even the reporting
        # checker. A bad later target must never leave a partial repair route.
        for name in route.node_names:
            if self.cycles[name].state not in {
                NodeCycleState.ACCEPTED,
                NodeCycleState.STALE,
                NodeCycleState.PRODUCER_READY,
                NodeCycleState.REPAIR_READY,
            }:
                raise ValueError(
                    "repair target must be quiescent, got "
                    f"{self.cycles[name].state.value}"
                )
        action = (
            CycleAction.CHECKER_RETRY
            if route.target == RouteTarget.NODE_CHECKER
            else CycleAction.CHECKER_REJECTED
        )
        result = replace(
            self,
            cycles={
                **self.cycles,
                current_node: cycle.transition(action, verdict=verdict),
            },
        )
        if finding_class != FindingClass.DEPENDENCY_DEFECT:
            result = result._refresh_readiness_and_terminal()
        if route.target == RouteTarget.NODE_CHECKER:
            return result, route
        if route.target == RouteTarget.PLAN_CYCLE:
            return replace(
                result._mark_nodes_stale(
                    route.stale_nodes,
                    allow_active=True,
                    preserve_verdict_node=current_node,
                ),
                state=GraphExecutionState.REPLAN_REQUIRED,
            ), route
        cycles = dict(result.cycles)
        for target_name in route.node_names or (route.node_name,):
            if target_name != current_node:
                cycles[target_name] = _repair_ready(cycles[target_name])
        result = replace(result, cycles=cycles)
        if finding_class == FindingClass.DEPENDENCY_DEFECT:
            barriers = dict(result.repair_barriers)
            for provider in route.node_names:
                for consumer in self._dependency_consumers(current_node, provider):
                    if consumer not in route.stale_nodes:
                        # Batch targets already have repair work. Their normal
                        # producer/checker prerequisites gate execution inputs;
                        # CONTRACT edges must not serialize software repairs.
                        continue
                    barriers[consumer] = tuple(
                        sorted({*barriers.get(consumer, ()), provider})
                    )
            result = replace(result, repair_barriers=barriers)
        return result._mark_nodes_stale(
            route.stale_nodes,
            preserve_verdict_node=current_node,
        ), route

    def _mark_nodes_stale(
        self,
        node_names: tuple[str, ...],
        *,
        allow_active: bool = False,
        preserve_verdict_node: str = "",
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
                    last_verdict=(
                        cycle.last_verdict if name == preserve_verdict_node else None
                    ),
                )
            elif allow_active and (cycle.is_running or cycle.active_assignment is not None):
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
        if self.state not in {GraphExecutionState.RUNNING, GraphExecutionState.COMPLETED}:
            return self
        cycles = dict(self.cycles)
        changed = True
        while changed:
            changed = False
            for name, cycle in list(cycles.items()):
                if name in self.pending_scope:
                    continue
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
        if sink_cycle.state == NodeCycleState.ACCEPTED and not self.dependency_repairs.pending:
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
