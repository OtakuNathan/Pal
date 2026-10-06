from __future__ import annotations

from pal.bunshin.v2.unit_of_work import BunshinUnitOfWork
from dataclasses import dataclass, replace
from typing import Iterable

from pal.bunshin.v2.cycle_protocol import (
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
from pal.bunshin.v2.graph_executor import (
    FindingClass,
    FindingRoute,
    GraphDiff,
    GraphExecution,
    GraphExecutionState,
    NodeReuseKind,
    diff_graphs,
)
from pal.bunshin.v2.graph_protocol import GraphIR
from pal.bunshin.v2.dependency_repair_protocol import RepairIncarnation
from pal.bunshin.v2.repository import BunshinV2Repository


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

    repository: BunshinV2Repository

    def ensure_plan_cycle(
        self,
        *,
        workflow_id: str,
        generation: int = 1,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> PlanCycle:
        cycle = (unit_of_work or self.repository).cycles.read_plan_cycle(
            workflow_id=workflow_id,
        )
        if cycle is not None:
            return cycle
        cycle = PlanCycle(
            cycle_id=f"{workflow_id}:plan",
            generation=generation,
        )
        (unit_of_work or self.repository).cycles.store_plan_cycle(
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
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> PlanCycle:
        cycle = self.ensure_plan_cycle(
            workflow_id=workflow_id,
            unit_of_work=unit_of_work,
        )
        updated = cycle.transition(
            action,
            assignment=assignment,
            product_ref=product_ref,
            verdict=verdict,
        )
        (unit_of_work or self.repository).cycles.store_plan_cycle(
            workflow_id=workflow_id,
            cycle=updated,
        )
        return updated

    def begin_plan_revision(
        self,
        *,
        workflow_id: str,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> PlanCycle:
        cycle = self.ensure_plan_cycle(
            workflow_id=workflow_id,
            unit_of_work=unit_of_work,
        )
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
        (unit_of_work or self.repository).cycles.store_plan_cycle(
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
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> PlanCycle:
        cycle = self.ensure_plan_cycle(
            workflow_id=workflow_id,
            unit_of_work=unit_of_work,
        )
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
            unit_of_work=unit_of_work,
        )

    def import_plan_product(
        self, *, workflow_id: str, product_ref: str,
        unit_of_work: BunshinUnitOfWork,
    ) -> PlanCycle:
        """Bind an imported initial product after its domain provenance guard.

        The caller pairs this with IMPORT_ARCHITECTURE_REVISION under the
        same write lock, or validates that exact persisted import for recovery.
        No producer assignment, verifier verdict, or graph installation occurs.
        """
        cycle = self.ensure_plan_cycle(workflow_id=workflow_id, unit_of_work=unit_of_work)
        if (product_ref and cycle.generation == 1 and cycle.product_ref == product_ref
                and cycle.state in {PlanCycleState.CHECKER_READY, PlanCycleState.CHECKING,
                                    PlanCycleState.HUMAN_REVIEW, PlanCycleState.ACCEPTED}):
            return cycle
        return self.transition_plan(workflow_id=workflow_id, action=CycleAction.IMPORT_PRODUCT,
                                    product_ref=product_ref, unit_of_work=unit_of_work)

    def submit_plan_product(
        self,
        *,
        workflow_id: str,
        product_ref: str,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> PlanCycle:
        cycle = self.ensure_plan_cycle(
            workflow_id=workflow_id,
            unit_of_work=unit_of_work,
        )
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
            unit_of_work=unit_of_work,
        )

    def reject_plan_product(
        self,
        *,
        workflow_id: str,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> PlanCycle:
        return self.transition_plan(
            workflow_id=workflow_id,
            action=CycleAction.PRODUCER_REJECTED,
            unit_of_work=unit_of_work,
        )

    def submit_plan_verdict(
        self,
        *,
        workflow_id: str,
        accepted: bool,
        finding_refs: Iterable[str] = (),
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> PlanCycle:
        cycle = self.ensure_plan_cycle(
            workflow_id=workflow_id,
            unit_of_work=unit_of_work,
        )
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
            unit_of_work=unit_of_work,
        )
        if accepted:
            updated = self.transition_plan(
                workflow_id=workflow_id,
                action=CycleAction.REQUEST_HUMAN_REVIEW,
                unit_of_work=unit_of_work,
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
        with self.repository.transaction() as connection:
            existing_graph = (connection or self.repository).cycles.read_graph_generation(
                graph_id=graph.graph_id,
                generation=graph.generation,
            )
            if existing_graph is not None:
                if existing_graph != graph:
                    raise ValueError(
                        "GraphIR generation identity is already bound to other content"
                    )
                existing_execution = (connection or self.repository).cycles.read_graph_execution(
                    workflow_id=workflow_id,
                    generation=graph.generation,
                )
                if existing_execution is not None:
                    return InstalledGraph(execution=existing_execution, diff=None)
            previous_graph = (
                (connection or self.repository).cycles.read_graph_generation(
                    graph_id=graph.graph_id,
                    generation=graph.generation - 1,
                )
                if graph.generation > 1
                else None
            )
            previous_execution = (
                (connection or self.repository).cycles.read_graph_execution(
                    workflow_id=workflow_id,
                    generation=graph.generation - 1,
                )
                if previous_graph is not None
                else None
            )
            (connection or self.repository).cycles.store_graph_generation(
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
                if previous_execution.dependency_repairs.pending is not None:
                    previous_execution = replace(
                        previous_execution,
                        state=GraphExecutionState.REPLAN_REQUIRED,
                        published_sink_ref="",
                    )
                    (connection or self.repository).cycles.store_graph_execution(
                        workflow_id=workflow_id,
                        execution=previous_execution,
                    )
                execution = _replanned_execution(
                    previous_execution,
                    graph,
                    diff,
                )
            (connection or self.repository).cycles.store_graph_execution(
                workflow_id=workflow_id,
                execution=execution,
            )
        return InstalledGraph(execution=execution, diff=diff)

    def execution(
        self,
        *,
        workflow_id: str,
        generation: int | None = None,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> GraphExecution:
        execution = (unit_of_work or self.repository).cycles.read_graph_execution(
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
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> NodeCycle:
        execution = self.execution(
            workflow_id=workflow_id,
            unit_of_work=unit_of_work,
        )
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
        if node_name not in execution.runnable_nodes():
            raise RuntimeError(
                f"{node_name} is not runnable: graph admission or dependency readiness is fenced"
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
            unit_of_work=unit_of_work,
        )

    def producer_submitted(
        self,
        *,
        workflow_id: str,
        node_name: str,
        product_ref: str,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> NodeCycle:
        execution = self.execution(
            workflow_id=workflow_id,
            unit_of_work=unit_of_work,
        )
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
            unit_of_work=unit_of_work,
        )

    def accept_null_node(
        self,
        *,
        workflow_id: str,
        node_name: str,
        product_ref: str,
        input_fingerprint: str,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> NodeCycle:
        """Mechanically close a null producer/checker pair in one write."""

        execution = self.execution(
            workflow_id=workflow_id,
            unit_of_work=unit_of_work,
        )
        cycle = execution.cycles[node_name]
        if cycle.state == NodeCycleState.ACCEPTED:
            if cycle.accepted_product_ref != product_ref:
                raise RuntimeError(
                    f"{node_name} is already accepted with another product"
                )
            return cycle
        if (node_name not in execution.runnable_nodes()
                or any(execution.cycles[name].state != NodeCycleState.ACCEPTED
                       for name in execution.graph.checker_predecessors(node_name))):
            raise RuntimeError(f"{node_name} null admission or checker inputs are fenced")
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
        return self._store_cycle(
            workflow_id,
            execution,
            cycle,
            unit_of_work=unit_of_work,
        )

    def checker_verdict(
        self,
        *,
        workflow_id: str,
        node_name: str,
        accepted: bool,
        finding_refs: Iterable[str] = (),
        finding_class: FindingClass | None = None,
        dependency_node: str = "",
        dependency_nodes: tuple[str, ...] = (),
        accepted_product_ref: str = "",
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> FindingRoute | None:
        execution = self.execution(
            workflow_id=workflow_id,
            unit_of_work=unit_of_work,
        )
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
            dependency_nodes=dependency_nodes,
            accepted_product_ref=accepted_product_ref,
        )
        (unit_of_work or self.repository).cycles.store_graph_execution(
            workflow_id=workflow_id,
            execution=updated,
        )
        return route

    def require_node_triage(
        self,
        *,
        workflow_id: str,
        node_name: str,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> NodeCycle | None:
        execution = (unit_of_work or self.repository).cycles.read_graph_execution(
            workflow_id=workflow_id,
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
            unit_of_work=unit_of_work,
        )

    def require_plan_triage(
        self,
        *,
        workflow_id: str,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> PlanCycle | None:
        cycle = (unit_of_work or self.repository).cycles.read_plan_cycle(
            workflow_id=workflow_id,
        )
        if cycle is None:
            return None
        if cycle.state == PlanCycleState.TRIAGE_REQUIRED:
            return cycle
        updated = cycle.transition(CycleAction.REQUIRE_TRIAGE)
        (unit_of_work or self.repository).cycles.store_plan_cycle(
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

    def request_workflow_pause(
        self,
        *,
        workflow_id: str,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        self._control_plan(workflow_id, CycleAction.REQUEST_PAUSE, unit_of_work=unit_of_work)
        self._control_graph(workflow_id, CycleAction.REQUEST_PAUSE, unit_of_work=unit_of_work)

    def request_workflow_cancel(
        self,
        *,
        workflow_id: str,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        self._control_plan(workflow_id, CycleAction.REQUEST_CANCEL, unit_of_work=unit_of_work)
        self._control_graph(workflow_id, CycleAction.REQUEST_CANCEL, unit_of_work=unit_of_work)

    def resume_workflow(
        self,
        *,
        workflow_id: str,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        self._control_plan(workflow_id, CycleAction.RESUME, unit_of_work=unit_of_work)
        self._control_graph(workflow_id, CycleAction.RESUME, unit_of_work=unit_of_work)

    def confirm_plan_control(
        self,
        *,
        workflow_id: str,
        cancel: bool,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        self._control_plan(
            workflow_id,
            CycleAction.CANCELLED if cancel else CycleAction.PAUSED,
            unit_of_work=unit_of_work,
        )

    def confirm_node_control(
        self,
        *,
        workflow_id: str,
        node_name: str,
        cancel: bool,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        execution = (unit_of_work or self.repository).cycles.read_graph_execution(
            workflow_id=workflow_id,
        )
        if execution is None or node_name not in execution.cycles:
            return
        if node_name in execution.pending_scope:
            # Aggregate pause/cleanup confirmation cannot replace a frozen
            # repair cursor. Only its exact RepairClosure retires that cursor;
            # terminal cancellation first archives the cohort above this layer.
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
        (unit_of_work or self.repository).cycles.store_graph_execution(
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
        pending_checker_input_fingerprint: str = "",
        pending_checker_generation: int = 0,
        pending_repair: RepairIncarnation | None = None,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        if plan:
            if pending_checker_input_fingerprint or pending_repair is not None:
                raise ValueError("pending node checker settlement cannot restore a plan cycle")
            self._control_plan(
                workflow_id,
                CycleAction.RESOLVE_TRIAGE,
                unit_of_work=unit_of_work,
            )
            return
        execution = (unit_of_work or self.repository).cycles.read_graph_execution(
            workflow_id=workflow_id,
        )
        if execution is None or node_name not in execution.cycles:
            if pending_checker_input_fingerprint or pending_repair is not None:
                raise ValueError("pending checker settlement has no current graph cycle")
            return
        cycle = execution.cycles[node_name]
        cohort = execution.dependency_repairs.pending
        member = next((item for item in cohort.frontier.values() if item.node_name == node_name), None) if cohort else None
        if pending_repair is not None or member is not None:
            if (pending_checker_input_fingerprint or pending_repair is None
                    or pending_repair != member or cohort is None
                    or execution.state != GraphExecutionState.RUNNING
                    or cycle.cycle_id != pending_repair.cycle_id
                    or cycle.generation != pending_repair.generation):
                raise ValueError("dependency repair recovery does not own the frozen graph cursor")
            if pending_repair.key in cohort.closures:
                raise ValueError("dependency repair recovery cannot restore a closed incarnation")
            slot = CycleSlot(pending_repair.slot)
            assignment = CycleAssignment(slot, AssignmentKind.RESUME,
                                         pending_repair.generation, pending_repair.input_fingerprint)
            if cycle.state != NodeCycleState.TRIAGE_REQUIRED:
                if (cycle.active_assignment is None
                        or cycle.active_assignment.slot != slot
                        or cycle.active_assignment.input_fingerprint != assignment.input_fingerprint):
                    raise ValueError("dependency repair recovery lost its current graph cursor")
                return
            ready = NodeCycleState.CHECKER_READY if slot == CycleSlot.CHECKER else NodeCycleState.PRODUCER_READY
            allowed = {ready, NodeCycleState.CANCEL_REQUESTED}
            if slot == CycleSlot.PRODUCER:
                allowed.add(NodeCycleState.REPAIR_READY)
            if cycle.active_assignment is not None or cycle.resume_state not in allowed:
                raise ValueError("dependency repair recovery has an incompatible triaged cursor")
            resumed = cycle.transition(CycleAction.RESOLVE_TRIAGE)
            # This is the original admitted setup/cleanup cursor, not a role
            # start. No failure budget, candidate, admission, or outbox changes.
            resumed = replace(resumed, active_assignment=assignment,
                state=(NodeCycleState.CANCEL_REQUESTED if resumed.state == NodeCycleState.CANCEL_REQUESTED
                       else NodeCycleState.CHECKING if slot == CycleSlot.CHECKER else NodeCycleState.PRODUCING),
                resume_state=NodeCycleState.STALE if resumed.state == NodeCycleState.CANCEL_REQUESTED else None)
            restored = replace(execution, cycles={**execution.cycles, node_name: resumed})
            restored._validate_repair_frontier(cohort)
            (unit_of_work or self.repository).cycles.store_graph_execution(
                workflow_id=workflow_id, execution=restored)
            return
        if cycle.state != NodeCycleState.TRIAGE_REQUIRED:
            if pending_checker_input_fingerprint and not (
                cycle.state == NodeCycleState.CHECKING
                and cycle.generation == pending_checker_generation
                and cycle.active_assignment == CycleAssignment(
                    CycleSlot.CHECKER, AssignmentKind.RESUME,
                    pending_checker_generation, pending_checker_input_fingerprint,
                )
            ):
                raise ValueError("pending checker settlement does not own the current graph cursor")
            return
        if pending_checker_input_fingerprint and (
            cycle.resume_state != NodeCycleState.CHECKER_READY
            or cycle.active_assignment is not None
            or not cycle.product_ref
            or cycle.generation != pending_checker_generation
        ):
            raise ValueError("pending checker settlement does not match the triaged graph cycle")
        resumed = cycle.transition(CycleAction.RESOLVE_TRIAGE)
        if pending_checker_input_fingerprint:
            # CHECKING is the logical checker/settlement cursor, not an OS
            # process. The verifier already submitted and quiesced. Restore
            # only that cursor; no role admission or process is created here.
            resumed = resumed.transition(
                CycleAction.START_CHECKER,
                assignment=CycleAssignment(
                    slot=CycleSlot.CHECKER,
                    kind=AssignmentKind.RESUME,
                    generation=cycle.generation,
                    input_fingerprint=pending_checker_input_fingerprint,
                ),
            )
        (unit_of_work or self.repository).cycles.store_graph_execution(
            workflow_id=workflow_id,
            execution=execution.with_cycle(resumed),
        )

    def _control_plan(
        self,
        workflow_id: str,
        action: CycleAction,
        *,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        cycle = (unit_of_work or self.repository).cycles.read_plan_cycle(
            workflow_id=workflow_id,
        )
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
        (unit_of_work or self.repository).cycles.store_plan_cycle(
            workflow_id=workflow_id,
            cycle=cycle,
        )

    def _control_graph(
        self,
        workflow_id: str,
        action: CycleAction,
        *,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        execution = (unit_of_work or self.repository).cycles.read_graph_execution(
            workflow_id=workflow_id,
        )
        if execution is None:
            return
        if action == CycleAction.REQUEST_CANCEL:
            execution = replace(execution, state=GraphExecutionState.CANCELLED,
                                published_sink_ref="")
        cycles = dict(execution.cycles)
        changed = action == CycleAction.REQUEST_CANCEL
        for name, current in tuple(cycles.items()):
            if action in {CycleAction.REQUEST_PAUSE, CycleAction.RESUME} and name in execution.pending_scope:
                # Workflow/node aggregates own pause intent. Keep the cohort's
                # original admitted cursors and evidence intact for recovery.
                continue
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
                was_running = current.is_running or current.active_assignment is not None
                updated = current.transition(action)
                if not was_running:
                    updated = updated.transition(CycleAction.PAUSED)
            elif action == CycleAction.REQUEST_CANCEL:
                if current.state == NodeCycleState.CANCELLED:
                    continue
                was_running = current.is_running or current.active_assignment is not None
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
            (unit_of_work or self.repository).cycles.store_graph_execution(
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
        *,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> NodeCycle:
        updated = execution.with_cycle(cycle)
        (unit_of_work or self.repository).cycles.store_graph_execution(
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
    invalidated = set(source.pending_scope)
    for cohort in source.dependency_repairs.history:
        if cohort.status == "superseded":
            invalidated.update(cohort.scope)
    for name in target.nodes:
        decision = diff.decisions[name]
        previous = source.cycles.get(name)
        cycle_id = f"{target.graph_id}:g{target.generation}:{name}"
        if (
            decision.kind == NodeReuseKind.REUSE_ACCEPTED
            and name not in invalidated
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
