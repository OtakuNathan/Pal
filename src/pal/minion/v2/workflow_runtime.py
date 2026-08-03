from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from pal.minion.v2.cycle_protocol import (
    AssignmentKind,
    CycleAction,
    CycleAssignment,
    CycleSlot,
    CycleVerdict,
    NodeCycle,
    NodeCycleState,
    PlanCycle,
    PlanCycleState,
)
from pal.minion.v2.graph_executor import (
    FindingClass,
    FindingRoute,
    GraphDiff,
    GraphExecution,
    GraphExecutionState,
    NodeReuseKind,
    diff_graphs,
)
from pal.minion.v2.graph_protocol import GraphIR
from pal.minion.v2.repository import MinionV2Repository


@dataclass(frozen=True)
class RunnableAssignment:
    node_name: str
    cycle_id: str
    slot: CycleSlot
    kind: AssignmentKind
    generation: int


@dataclass(frozen=True)
class InstalledGraph:
    execution: GraphExecution
    diff: GraphDiff | None


@dataclass
class WorkflowCoordinator:
    """The single mechanical owner of PlanCycle and GraphExecution state.

    Family roles author and inspect semantic products.  This coordinator owns
    cycle transitions, graph readiness, replan reuse, reverse finding routing,
    and publication of the declared sink.  It does not launch processes or
    interpret Family-specific contract fields.
    """

    repository: MinionV2Repository

    def ensure_plan_cycle(
        self,
        *,
        workflow_id: str,
        generation: int = 1,
    ) -> PlanCycle:
        cycle = self.repository.read_plan_cycle(workflow_id=workflow_id)
        if cycle is not None:
            return cycle
        cycle = PlanCycle(
            cycle_id=f"{workflow_id}:plan",
            generation=generation,
        )
        self.repository.store_plan_cycle(
            workflow_id=workflow_id,
            cycle=cycle,
        )
        return cycle

    def transition_plan(
        self,
        *,
        workflow_id: str,
        action: CycleAction,
        assignment: CycleAssignment | None = None,
        product_ref: str = "",
        verdict: CycleVerdict | None = None,
    ) -> PlanCycle:
        cycle = self.ensure_plan_cycle(workflow_id=workflow_id)
        updated = cycle.transition(
            action,
            assignment=assignment,
            product_ref=product_ref,
            verdict=verdict,
        )
        self.repository.store_plan_cycle(
            workflow_id=workflow_id,
            cycle=updated,
        )
        return updated

    def begin_plan_revision(self, *, workflow_id: str) -> PlanCycle:
        cycle = self.ensure_plan_cycle(workflow_id=workflow_id)
        if cycle.state == PlanCycleState.HUMAN_REVIEW:
            updated = cycle.transition(CycleAction.HUMAN_EDITED)
        elif cycle.state == PlanCycleState.ACCEPTED:
            updated = replace(
                cycle,
                generation=cycle.generation + 1,
                state=PlanCycleState.REPAIR_READY,
                active_assignment=None,
                product_ref="",
                accepted_product_ref="",
                last_verdict=None,
            )
        elif cycle.state == PlanCycleState.REPAIR_READY:
            return cycle
        else:
            raise RuntimeError(
                "a new plan revision requires a quiescent accepted or repair cycle"
            )
        self.repository.store_plan_cycle(
            workflow_id=workflow_id,
            cycle=updated,
        )
        return updated

    def start_plan_assignment(
        self,
        *,
        workflow_id: str,
        slot: CycleSlot,
        kind: AssignmentKind,
        input_fingerprint: str,
    ) -> PlanCycle:
        cycle = self.ensure_plan_cycle(workflow_id=workflow_id)
        running_state = (
            PlanCycleState.PRODUCING
            if slot == CycleSlot.PRODUCER
            else PlanCycleState.CHECKING
        )
        if (
            cycle.state == running_state
            and cycle.active_assignment is not None
            and cycle.active_assignment.slot == slot
        ):
            if (
                cycle.active_assignment.kind == kind
                and cycle.active_assignment.input_fingerprint
                == input_fingerprint
            ):
                return cycle
            raise RuntimeError(
                "plan slot already runs a different assignment"
            )
        return self.transition_plan(
            workflow_id=workflow_id,
            action=(
                CycleAction.START_PRODUCER
                if slot == CycleSlot.PRODUCER
                else CycleAction.START_CHECKER
            ),
            assignment=CycleAssignment(
                slot=slot,
                kind=kind,
                generation=cycle.generation,
                input_fingerprint=input_fingerprint,
            ),
        )

    def submit_plan_product(
        self,
        *,
        workflow_id: str,
        product_ref: str,
    ) -> PlanCycle:
        cycle = self.ensure_plan_cycle(workflow_id=workflow_id)
        if (
            cycle.state in {
                PlanCycleState.CHECKER_READY,
                PlanCycleState.CHECKING,
                PlanCycleState.HUMAN_REVIEW,
                PlanCycleState.ACCEPTED,
            }
            and cycle.product_ref == product_ref
        ):
            return cycle
        return self.transition_plan(
            workflow_id=workflow_id,
            action=CycleAction.PRODUCER_SUBMITTED,
            product_ref=product_ref,
        )

    def reject_plan_product(self, *, workflow_id: str) -> PlanCycle:
        return self.transition_plan(
            workflow_id=workflow_id,
            action=CycleAction.PRODUCER_REJECTED,
        )

    def submit_plan_verdict(
        self,
        *,
        workflow_id: str,
        accepted: bool,
        finding_refs: Iterable[str] = (),
    ) -> PlanCycle:
        cycle = self.ensure_plan_cycle(workflow_id=workflow_id)
        finding_refs_tuple = tuple(finding_refs)
        if (
            cycle.last_verdict is not None
            and cycle.last_verdict.accepted == accepted
            and cycle.last_verdict.finding_refs == finding_refs_tuple
            and (
                accepted
                and cycle.state in {
                    PlanCycleState.HUMAN_REVIEW,
                    PlanCycleState.ACCEPTED,
                }
                or not accepted
                and cycle.state == PlanCycleState.REPAIR_READY
            )
        ):
            return cycle
        updated = self.transition_plan(
            workflow_id=workflow_id,
            action=(
                CycleAction.CHECKER_ACCEPTED
                if accepted
                else CycleAction.CHECKER_REJECTED
            ),
            verdict=CycleVerdict(
                accepted=accepted,
                generation=cycle.generation,
                finding_refs=finding_refs_tuple,
            ),
        )
        if accepted:
            updated = self.transition_plan(
                workflow_id=workflow_id,
                action=CycleAction.REQUEST_HUMAN_REVIEW,
            )
        return updated

    def install_graph(
        self,
        *,
        workflow_id: str,
        graph: GraphIR,
    ) -> InstalledGraph:
        if graph.graph_id != workflow_id:
            raise ValueError("GraphIR identity must equal its workflow identity")
        existing_graph = self.repository.read_graph_generation(
            graph_id=graph.graph_id,
            generation=graph.generation,
        )
        if existing_graph is not None:
            if existing_graph != graph:
                raise ValueError(
                    "GraphIR generation identity is already bound to other content"
                )
            existing_execution = self.repository.read_graph_execution(
                workflow_id=workflow_id,
                generation=graph.generation,
            )
            if existing_execution is not None:
                return InstalledGraph(execution=existing_execution, diff=None)
        previous_graph = (
            self.repository.read_graph_generation(
                graph_id=graph.graph_id,
                generation=graph.generation - 1,
            )
            if graph.generation > 1
            else None
        )
        previous_execution = (
            self.repository.read_graph_execution(
                workflow_id=workflow_id,
                generation=graph.generation - 1,
            )
            if previous_graph is not None
            else None
        )
        self.repository.store_graph_generation(
            workflow_id=workflow_id,
            graph=graph,
            status="running",
        )
        if previous_graph is None:
            execution = GraphExecution.start(graph)
            diff = None
        else:
            if previous_execution is None:
                raise RuntimeError(
                    "a prior GraphIR generation has no GraphExecution projection"
                )
            diff = diff_graphs(previous_graph, graph)
            execution = _replanned_execution(
                previous_execution,
                graph,
                diff,
            )
        self.repository.store_graph_execution(
            workflow_id=workflow_id,
            execution=execution,
        )
        return InstalledGraph(execution=execution, diff=diff)

    def execution(
        self,
        *,
        workflow_id: str,
        generation: int | None = None,
    ) -> GraphExecution:
        execution = self.repository.read_graph_execution(
            workflow_id=workflow_id,
            generation=generation,
        )
        if execution is None:
            raise RuntimeError("workflow has no installed GraphExecution")
        return execution

    def runnable_assignments(
        self,
        *,
        workflow_id: str,
        generation: int | None = None,
    ) -> tuple[RunnableAssignment, ...]:
        execution = self.execution(
            workflow_id=workflow_id,
            generation=generation,
        )
        assignments: list[RunnableAssignment] = []
        for name in execution.runnable_nodes():
            cycle = execution.cycles[name]
            slot = (
                CycleSlot.CHECKER
                if cycle.state == NodeCycleState.CHECKER_READY
                else CycleSlot.PRODUCER
            )
            kind = (
                AssignmentKind.RECHECK
                if slot == CycleSlot.CHECKER and cycle.last_verdict is not None
                else AssignmentKind.REPAIR
                if cycle.state == NodeCycleState.REPAIR_READY
                else AssignmentKind.INITIAL
            )
            assignments.append(
                RunnableAssignment(
                    node_name=name,
                    cycle_id=cycle.cycle_id,
                    slot=slot,
                    kind=kind,
                    generation=cycle.generation,
                )
            )
        return tuple(assignments)

    def start_assignment(
        self,
        *,
        workflow_id: str,
        node_name: str,
        slot: CycleSlot,
        kind: AssignmentKind,
        input_fingerprint: str,
    ) -> NodeCycle:
        execution = self.execution(workflow_id=workflow_id)
        cycle = execution.cycles[node_name]
        running_state = (
            NodeCycleState.PRODUCING
            if slot == CycleSlot.PRODUCER
            else NodeCycleState.CHECKING
        )
        if (
            cycle.state == running_state
            and cycle.active_assignment is not None
            and cycle.active_assignment.slot == slot
        ):
            if (
                cycle.active_assignment.kind == kind
                and cycle.active_assignment.input_fingerprint
                == input_fingerprint
            ):
                return cycle
            raise RuntimeError(
                f"{node_name} already runs a different {slot.value} assignment"
            )
        assignment = CycleAssignment(
            slot=slot,
            kind=kind,
            generation=cycle.generation,
            input_fingerprint=input_fingerprint,
        )
        action = (
            CycleAction.START_PRODUCER
            if slot == CycleSlot.PRODUCER
            else CycleAction.START_CHECKER
        )
        return self._store_cycle(
            workflow_id,
            execution,
            cycle.transition(action, assignment=assignment),
        )

    def producer_submitted(
        self,
        *,
        workflow_id: str,
        node_name: str,
        product_ref: str,
    ) -> NodeCycle:
        execution = self.execution(workflow_id=workflow_id)
        cycle = execution.cycles[node_name]
        if (
            cycle.state in {
                NodeCycleState.CHECKER_READY,
                NodeCycleState.CHECKING,
                NodeCycleState.ACCEPTED,
            }
            and cycle.product_ref == product_ref
        ):
            return cycle
        return self._store_cycle(
            workflow_id,
            execution,
            cycle.transition(
                CycleAction.PRODUCER_SUBMITTED,
                product_ref=product_ref,
            ),
        )

    def accept_null_node(
        self,
        *,
        workflow_id: str,
        node_name: str,
        product_ref: str,
        input_fingerprint: str,
    ) -> NodeCycle:
        """Mechanically close a null producer/checker pair in one write."""

        execution = self.execution(workflow_id=workflow_id)
        cycle = execution.cycles[node_name]
        if cycle.state == NodeCycleState.ACCEPTED:
            if cycle.accepted_product_ref != product_ref:
                raise RuntimeError(
                    f"{node_name} is already accepted with another product"
                )
            return cycle
        if cycle.state not in {
            NodeCycleState.PRODUCER_READY,
            NodeCycleState.REPAIR_READY,
        }:
            raise RuntimeError(
                f"null node must start from a producer boundary, got {cycle.state.value}"
            )
        cycle = cycle.transition(
            CycleAction.START_PRODUCER,
            assignment=CycleAssignment(
                slot=CycleSlot.PRODUCER,
                kind=(
                    AssignmentKind.REPAIR
                    if cycle.state == NodeCycleState.REPAIR_READY
                    else AssignmentKind.INITIAL
                ),
                generation=cycle.generation,
                input_fingerprint=input_fingerprint,
            ),
        )
        cycle = cycle.transition(
            CycleAction.PRODUCER_SUBMITTED,
            product_ref=product_ref,
        )
        cycle = cycle.transition(
            CycleAction.START_CHECKER,
            assignment=CycleAssignment(
                slot=CycleSlot.CHECKER,
                kind=AssignmentKind.INITIAL,
                generation=cycle.generation,
                input_fingerprint=input_fingerprint,
            ),
        )
        cycle = cycle.transition(
            CycleAction.CHECKER_ACCEPTED,
            verdict=CycleVerdict(
                accepted=True,
                generation=cycle.generation,
            ),
        )
        return self._store_cycle(workflow_id, execution, cycle)

    def checker_verdict(
        self,
        *,
        workflow_id: str,
        node_name: str,
        accepted: bool,
        finding_refs: Iterable[str] = (),
        finding_class: FindingClass | None = None,
        dependency_node: str = "",
        accepted_product_ref: str = "",
    ) -> FindingRoute | None:
        execution = self.execution(workflow_id=workflow_id)
        cycle = execution.cycles[node_name]
        finding_refs_tuple = tuple(finding_refs)
        if (
            accepted
            and cycle.state == NodeCycleState.ACCEPTED
            and cycle.last_verdict is not None
            and cycle.last_verdict.accepted
            and cycle.last_verdict.finding_refs == finding_refs_tuple
        ):
            return None
        if (
            not accepted
            and cycle.last_verdict is not None
            and not cycle.last_verdict.accepted
            and cycle.last_verdict.finding_refs == finding_refs_tuple
        ):
            # The first application already mutated the graph and routed the
            # immutable finding artifact. Replaying its receipt must not infer
            # or emit a second route from caller-supplied parameters.
            return None
        updated, route = execution.apply_checker_verdict(
            current_node=node_name,
            accepted=accepted,
            finding_refs=finding_refs_tuple,
            finding_class=finding_class,
            dependency_node=dependency_node,
            accepted_product_ref=accepted_product_ref,
        )
        self.repository.store_graph_execution(
            workflow_id=workflow_id,
            execution=updated,
        )
        return route

    def require_node_triage(
        self,
        *,
        workflow_id: str,
        node_name: str,
    ) -> NodeCycle | None:
        execution = self.repository.read_graph_execution(
            workflow_id=workflow_id
        )
        if execution is None or node_name not in execution.cycles:
            return None
        cycle = execution.cycles[node_name]
        if cycle.state == NodeCycleState.TRIAGE_REQUIRED:
            return cycle
        return self._store_cycle(
            workflow_id,
            execution,
            cycle.transition(CycleAction.REQUIRE_TRIAGE),
        )

    def require_plan_triage(self, *, workflow_id: str) -> PlanCycle | None:
        cycle = self.repository.read_plan_cycle(workflow_id=workflow_id)
        if cycle is None:
            return None
        if cycle.state == PlanCycleState.TRIAGE_REQUIRED:
            return cycle
        updated = cycle.transition(CycleAction.REQUIRE_TRIAGE)
        self.repository.store_plan_cycle(
            workflow_id=workflow_id,
            cycle=updated,
        )
        return updated

    def published_sink_ref(self, *, workflow_id: str) -> str:
        execution = self.execution(workflow_id=workflow_id)
        if execution.state != GraphExecutionState.COMPLETED:
            raise RuntimeError("the declared sink has not completed verification")
        if not execution.published_sink_ref:
            raise RuntimeError("completed GraphExecution has no sink product")
        return execution.published_sink_ref

    def request_workflow_pause(self, *, workflow_id: str) -> None:
        self._control_plan(workflow_id, CycleAction.REQUEST_PAUSE)
        self._control_graph(workflow_id, CycleAction.REQUEST_PAUSE)

    def request_workflow_cancel(self, *, workflow_id: str) -> None:
        self._control_plan(workflow_id, CycleAction.REQUEST_CANCEL)
        self._control_graph(workflow_id, CycleAction.REQUEST_CANCEL)

    def resume_workflow(self, *, workflow_id: str) -> None:
        self._control_plan(workflow_id, CycleAction.RESUME)
        self._control_graph(workflow_id, CycleAction.RESUME)

    def confirm_plan_control(
        self,
        *,
        workflow_id: str,
        cancel: bool,
    ) -> None:
        self._control_plan(
            workflow_id,
            CycleAction.CANCELLED if cancel else CycleAction.PAUSED,
        )

    def confirm_node_control(
        self,
        *,
        workflow_id: str,
        node_name: str,
        cancel: bool,
    ) -> None:
        execution = self.repository.read_graph_execution(
            workflow_id=workflow_id
        )
        if execution is None or node_name not in execution.cycles:
            return
        cycle = execution.cycles[node_name]
        expected = (
            NodeCycleState.CANCEL_REQUESTED
            if cancel
            else NodeCycleState.PAUSE_REQUESTED
        )
        if cycle.state != expected:
            if (
                cancel
                and execution.state == GraphExecutionState.REPLAN_REQUIRED
                and cycle.is_running
            ):
                cycle = cycle.transition(CycleAction.REQUEST_CANCEL)
            else:
                return
        self.repository.store_graph_execution(
            workflow_id=workflow_id,
            execution=execution.with_cycle(
                cycle.transition(
                    CycleAction.CANCELLED if cancel else CycleAction.PAUSED
                )
            ),
        )

    def resolve_triage(
        self,
        *,
        workflow_id: str,
        node_name: str = "",
        plan: bool = False,
    ) -> None:
        if plan:
            self._control_plan(workflow_id, CycleAction.RESOLVE_TRIAGE)
            return
        execution = self.repository.read_graph_execution(
            workflow_id=workflow_id
        )
        if execution is None or node_name not in execution.cycles:
            return
        cycle = execution.cycles[node_name]
        if cycle.state != NodeCycleState.TRIAGE_REQUIRED:
            return
        self.repository.store_graph_execution(
            workflow_id=workflow_id,
            execution=execution.with_cycle(
                cycle.transition(CycleAction.RESOLVE_TRIAGE)
            ),
        )

    def _control_plan(
        self,
        workflow_id: str,
        action: CycleAction,
    ) -> None:
        cycle = self.repository.read_plan_cycle(workflow_id=workflow_id)
        if cycle is None:
            return
        if cycle.state in {
            PlanCycleState.ACCEPTED,
            PlanCycleState.REJECTED,
            PlanCycleState.CANCELLED,
        }:
            return
        if action == CycleAction.REQUEST_PAUSE:
            if cycle.state in {
                PlanCycleState.PAUSE_REQUESTED,
                PlanCycleState.PAUSED,
            }:
                return
            was_running = cycle.is_running
            cycle = cycle.transition(action)
            if not was_running:
                cycle = cycle.transition(CycleAction.PAUSED)
        elif action == CycleAction.REQUEST_CANCEL:
            if cycle.state in {
                PlanCycleState.CANCEL_REQUESTED,
                PlanCycleState.CANCELLED,
            }:
                return
            was_running = cycle.is_running
            cycle = cycle.transition(action)
            if not was_running:
                cycle = cycle.transition(CycleAction.CANCELLED)
        elif action == CycleAction.RESUME:
            if cycle.state != PlanCycleState.PAUSED:
                return
            cycle = cycle.transition(action)
        elif action == CycleAction.RESOLVE_TRIAGE:
            if cycle.state != PlanCycleState.TRIAGE_REQUIRED:
                return
            cycle = cycle.transition(action)
        elif action in {CycleAction.PAUSED, CycleAction.CANCELLED}:
            expected = (
                PlanCycleState.PAUSE_REQUESTED
                if action == CycleAction.PAUSED
                else PlanCycleState.CANCEL_REQUESTED
            )
            if cycle.state != expected:
                return
            cycle = cycle.transition(action)
        else:
            raise ValueError(f"unsupported plan control action: {action.value}")
        self.repository.store_plan_cycle(
            workflow_id=workflow_id,
            cycle=cycle,
        )

    def _control_graph(
        self,
        workflow_id: str,
        action: CycleAction,
    ) -> None:
        execution = self.repository.read_graph_execution(
            workflow_id=workflow_id
        )
        if execution is None:
            return
        cycles = dict(execution.cycles)
        changed = False
        for name, current in tuple(cycles.items()):
            if current.state in {
                NodeCycleState.ACCEPTED,
                NodeCycleState.CANCELLED,
            }:
                continue
            if action == CycleAction.REQUEST_PAUSE:
                if current.state in {
                    NodeCycleState.PAUSE_REQUESTED,
                    NodeCycleState.PAUSED,
                }:
                    continue
                was_running = current.is_running
                updated = current.transition(action)
                if not was_running:
                    updated = updated.transition(CycleAction.PAUSED)
            elif action == CycleAction.REQUEST_CANCEL:
                if current.state in {
                    NodeCycleState.CANCEL_REQUESTED,
                    NodeCycleState.CANCELLED,
                }:
                    continue
                was_running = current.is_running
                updated = current.transition(action)
                if not was_running:
                    updated = updated.transition(CycleAction.CANCELLED)
            elif action == CycleAction.RESUME:
                if current.state != NodeCycleState.PAUSED:
                    continue
                updated = current.transition(action)
            else:
                raise ValueError(f"unsupported graph control action: {action.value}")
            cycles[name] = updated
            changed = True
        if changed:
            self.repository.store_graph_execution(
                workflow_id=workflow_id,
                execution=replace(
                    execution,
                    cycles=cycles,
                    state=(
                        GraphExecutionState.CANCELLED
                        if action == CycleAction.REQUEST_CANCEL
                        else execution.state
                    ),
                ),
            )

    def _store_cycle(
        self,
        workflow_id: str,
        execution: GraphExecution,
        cycle: NodeCycle,
    ) -> NodeCycle:
        updated = execution.with_cycle(cycle)
        self.repository.store_graph_execution(
            workflow_id=workflow_id,
            execution=updated,
        )
        return updated.cycles[cycle.node_name]


def _replanned_execution(
    source: GraphExecution,
    target: GraphIR,
    diff: GraphDiff,
) -> GraphExecution:
    cycles: dict[str, NodeCycle] = {}
    for name in target.nodes:
        decision = diff.decisions[name]
        previous = source.cycles.get(name)
        cycle_id = f"{target.graph_id}:g{target.generation}:{name}"
        if (
            decision.kind == NodeReuseKind.REUSE_ACCEPTED
            and previous is not None
            and previous.state == NodeCycleState.ACCEPTED
        ):
            cycles[name] = replace(
                previous,
                cycle_id=cycle_id,
                generation=target.generation,
                active_assignment=None,
                last_verdict=(
                    replace(
                        previous.last_verdict,
                        generation=target.generation,
                    )
                    if previous.last_verdict is not None
                    else None
                ),
            )
            continue
        cycles[name] = NodeCycle(
            cycle_id=cycle_id,
            node_name=name,
            generation=target.generation,
            state=NodeCycleState.STALE,
            accepted_product_ref=(
                previous.accepted_product_ref if previous is not None else ""
            ),
        )
    return GraphExecution(
        graph=target,
        state=GraphExecutionState.RUNNING,
        cycles=cycles,
    ).refresh()


__all__ = [
    "InstalledGraph",
    "RunnableAssignment",
    "WorkflowCoordinator",
]
