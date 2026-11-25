from __future__ import annotations

from pal.bunshin.unit_of_work import BunshinUnitOfWork
from pal.bunshin.storage.queries import QueriesStore
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Iterable, Mapping
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, ARCHITECT_RESULT_DRAIN_STATES, NODE_RESULT_DRAIN_STATES, DeferredEffectError, SubmissionInvariantError
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.execution_graph_facts import accepted_node_receipt, graph_node_bindings

from pal.bunshin.cycle_protocol import (
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
from pal.bunshin.graph_executor import (
    FindingClass,
    FindingRoute,
    GraphDiff,
    GraphExecution,
    GraphExecutionState,
    NodeReuseKind,
    diff_graphs,
)
from pal.bunshin.graph_protocol import GraphIR
from pal.bunshin.dependency_repair_protocol import RepairIncarnation
from pal.bunshin.repository import BunshinRepository


def workflow_pauses_cycles(workflow: AggregateSnapshot) -> bool:
    return workflow.state in {"PAUSE_REQUESTED", "PAUSED"} or (
        workflow.state == "TRIAGE_REQUIRED"
        and str(workflow.payload.get("triage_resume_state") or "") in {"ACTIVE", "PAUSE_REQUESTED"}
    )


def role_pause_must_drain(snapshot: AggregateSnapshot, queries: QueriesStore) -> bool:
    return queries.has_pending_role_receipt(snapshot) or (
        snapshot.aggregate_type == AggregateType.ARCHITECTURE_REVISION
        and snapshot.state in ARCHITECT_RESULT_DRAIN_STATES
    )


def node_pause_must_drain(
    node: AggregateSnapshot, execution: GraphExecution | None, queries: QueriesStore,
) -> bool:
    if queries.has_pending_role_receipt(node):
        return True
    if node.state not in NODE_RESULT_DRAIN_STATES:
        return False
    name = str(node.payload.get("module_name") or node.payload.get("unit_id") or "")
    # A registered dependency-repair capture has already transferred the
    # submitted result to its durable cohort. That owner handles pause/cleanup.
    captured = (node.state == "REVIEW_SNAPSHOTTING" and execution is not None
                and int(node.payload.get("graph_generation") or 0) == execution.graph.generation
                and name in execution.pending_scope and bool(node.payload.get("dependency_repair_capture_ref")))
    return not captured


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

    repository: BunshinRepository

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
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> InstalledGraph:
        if graph.graph_id != workflow_id:
            raise ValueError("GraphIR identity must equal its workflow identity")
        transaction = nullcontext(unit_of_work) if unit_of_work is not None else self.repository.transaction()
        with transaction as connection:
            existing_graph = (connection or self.repository).cycles.read_graph_generation(
                graph_id=graph.graph_id,
                generation=graph.generation,
            )
            if existing_graph is not None:
                if existing_graph.generation_hash != graph.generation_hash:
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

    def rebind_direct_task(self, *, workflow_id: str, graph: GraphIR,
                           unit_of_work: BunshinUnitOfWork) -> None:
        """Advance a clarified repository task without retaining an old verdict."""
        previous = self.execution(workflow_id=workflow_id, unit_of_work=unit_of_work)
        if (graph.graph_id != workflow_id or graph.generation != previous.graph.generation + 1
                or len(graph.nodes) != 1 or set(graph.nodes) != set(previous.graph.nodes)
                or graph.edges or any(spec.satellite_data.get("execution_mode") != "direct" for spec in graph.nodes.values())
                or any(cycle.state != NodeCycleState.TRIAGE_REQUIRED for cycle in previous.cycles.values())):
            raise ValueError("direct task clarification requires the current triaged repository cycle")
        unit_of_work.cycles.store_graph_generation(workflow_id=workflow_id, graph=graph, status="running")
        unit_of_work.cycles.store_graph_execution(workflow_id=workflow_id,
            execution=_replanned_execution(previous, graph, diff_graphs(previous.graph, graph)))

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
            work = cycle.pending_work
            assert work is not None  # runnable_nodes requires pending work
            assignments.append(
                RunnableAssignment(
                    node_name=name,
                    cycle_id=cycle.cycle_id,
                    slot=work.slot,
                    kind=work.kind,
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

    def recover_legacy_repair_cursor(self, node: AggregateSnapshot) -> NodeCycle:
        """Repair the old paused-repair projection once, before queued resume.

        Older runtimes recorded PRODUCER_READY for an interrupted repair.
        The retained operational resume receipt is the only missing fact;
        new cycles persist REPAIR_READY directly.
        """
        with self.repository.transaction() as work:
            current = work.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
            execution = self.execution(workflow_id=node.workflow_id, unit_of_work=work)
            name = str(node.payload.get("module_name") or node.payload.get("unit_id") or "")
            cycle = execution.cycles[name]
            if (current is None or current.version != node.version
                    or current.state != "REPAIR_QUEUED"
                    or cycle.state != NodeCycleState.PRODUCER_READY or cycle.active_assignment is not None
                    or name in execution.pending_scope
                    or int(current.payload.get("graph_generation") or execution.graph.generation) != execution.graph.generation
                    or not any(current.payload.get(key) == "REPAIRING" for key in ("resume_state", "triage_resume_state"))):
                raise SubmissionInvariantError("legacy repair resume receipt does not match its NodeCycle")
            return self._store_cycle(node.workflow_id, execution, replace(cycle, state=NodeCycleState.REPAIR_READY), unit_of_work=work)

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

    def complete_graph(self, *, workflow_id: str, epoch_id: str, generation: int) -> bool:
        """Project the authoritative completed graph into its workflow epoch.

        The operational node records provide receipt data, not a second vote
        about semantic acceptance. The complete generation and the exact
        accepted product for every node must be bound in the same transaction.
        """
        with self.repository.transaction() as work:
            epoch = work.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
            if epoch is None or epoch.state != "RUNNING":
                return False
            workflow = work.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
            if (workflow is None or workflow.state != "ACTIVE" or epoch.workflow_id != workflow_id
                    or workflow.payload.get("execution_epoch_id", epoch_id) != epoch_id):
                return False
            execution = work.cycles.read_graph_execution(workflow_id=workflow_id)
            if execution is None or execution.state != GraphExecutionState.COMPLETED:
                return False
            if (generation and execution.graph.generation != generation
                    or int(epoch.payload.get("graph_generation") or execution.graph.generation) != execution.graph.generation):
                return False
            bindings = graph_node_bindings(execution.graph, (
                node for node in work.queries.list_workflow_snapshots(workflow_id)
                if node.aggregate_type == AggregateType.DAG_NODE_RUN
                and node.payload.get("epoch_id") == epoch_id
            ))
            receipts = {
                name: accepted_node_receipt(node, execution.cycles[name])
                for name, node in bindings.items()
            }
            sink_node = bindings[execution.graph.sink]
            sink = receipts[execution.graph.sink]
            if not execution.published_sink_ref or sink.candidate.sha256 != execution.published_sink_ref:
                raise SubmissionInvariantError("completed graph has a different accepted sink receipt")
            work.transitions.dispatch(ActionEnvelope(
                action_type="ALL_REQUIRED_NODES_ACCEPTED", workflow_id=workflow_id,
                aggregate_type=AggregateType.EXECUTION_EPOCH, aggregate_id=epoch_id,
                actor="bunshin-outbox", expected_version=epoch.version,
                idempotency_key=f"graph-complete:{epoch_id}:{execution.graph.generation}:{execution.published_sink_ref}",
                payload={
                    "sink_candidate_ref": sink.candidate.to_dict(),
                    "accepted_sink": {
                        "node_id": sink_node.aggregate_id,
                        "candidate_digest": sink.candidate_digest,
                        "verification_ref": sink.verification.to_dict(),
                    },
                    "accepted_candidate_refs": [receipt.candidate.to_dict() for receipt in receipts.values()],
                    "verification_artifact_refs": [receipt.verification.to_dict() for receipt in receipts.values()],
                },
            ))
            return True

    def assert_graph_preparation_allowed(
        self, work: BunshinUnitOfWork, *, workflow_id: str, epoch_id: str, source_epoch_id: str,
    ) -> AggregateSnapshot:
        workflow = work.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            raise SubmissionInvariantError("execution preparation has no parent workflow")
        # CREATED is the deliberately supported low-level compiler admission;
        # normal workflow effects only prepare under ACTIVE.
        if workflow.state not in {"CREATED", "ACTIVE"}:
            raise DeferredEffectError("workflow control fences graph preparation")
        linked = str(workflow.payload.get("execution_epoch_id") or "")
        if linked and linked not in {source_epoch_id, epoch_id}:
            raise SubmissionInvariantError("execution preparation belongs to a superseded lineage")
        if linked and linked == source_epoch_id and linked != epoch_id:
            source = work.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, linked)
            if source is not None and source.state in {"PAUSE_REQUESTED", "PAUSED", "CANCEL_REQUESTED", "CANCELLED", "TRIAGE_REQUIRED"}:
                raise DeferredEffectError("source epoch control fences graph preparation")
        return workflow

    def activate_graph(
        self, *, workflow_id: str, epoch_id: str, actor: str,
        module_identity_delta: Mapping[str, object] | None = None,
        source_epoch_id: str = "",
    ) -> bool:
        """Activate only a fully materialized GraphIR generation.

        Normal compilation and restart share this boundary. A crash partway
        through node materialization must not authorize an incomplete graph.
        """
        with self.repository.transaction() as work:
            epoch = work.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
            if epoch is None or epoch.workflow_id != workflow_id:
                raise SubmissionInvariantError("execution epoch is missing or belongs to another workflow")
            if epoch.state != "STARTING":
                return False
            generation = int(epoch.payload.get("graph_generation") or 0)
            graph = work.cycles.read_graph_execution(workflow_id=workflow_id)
            if graph is None:
                raise SubmissionInvariantError("execution compilation has no installed graph")
            if generation and graph.graph.generation != generation:
                raise SubmissionInvariantError("execution compilation belongs to a superseded graph generation")
            recorded_source = str(epoch.payload.get("source_execution_epoch_id") or "")
            if recorded_source and source_epoch_id and recorded_source != source_epoch_id:
                raise SubmissionInvariantError("graph installation source changed during preparation")
            source_epoch_id = recorded_source or source_epoch_id
            workflow = self.assert_graph_preparation_allowed(
                work, workflow_id=workflow_id, epoch_id=epoch_id, source_epoch_id=source_epoch_id,
            )
            if source_epoch_id and module_identity_delta is None:
                raise SubmissionInvariantError("replan preparation has not completed")
            bindings = graph_node_bindings(graph.graph, (
                node for node in work.queries.list_workflow_snapshots(workflow_id)
                if node.aggregate_type == AggregateType.DAG_NODE_RUN
                and node.payload.get("epoch_id") == epoch_id
            ))
            if source_epoch_id:
                graph = self._settle_replan_installation(work, graph, bindings, source_epoch_id)
                work.cycles.store_graph_execution(workflow_id=workflow_id, execution=graph)
            else:
                for name, node in bindings.items():
                    if graph.cycles[name].state == NodeCycleState.ACCEPTED or node.state == "ACCEPTED":
                        accepted_node_receipt(node, graph.cycles[name])
            node_ids = [bindings[name].aggregate_id for name in sorted(bindings)]
            work.transitions.dispatch(ActionEnvelope(
                action_type="NODES_COMPILED", workflow_id=workflow_id,
                aggregate_type=AggregateType.EXECUTION_EPOCH, aggregate_id=epoch_id,
                actor=actor, expected_version=epoch.version,
                idempotency_key=f"{epoch_id}:NODES_COMPILED:{epoch.version}",
                payload={
                    "node_ids": node_ids,
                    "implementation_node_ids": node_ids,
                    "sink_node_id": bindings[graph.graph.sink].aggregate_id,
                    **({"module_identity_delta": dict(module_identity_delta)} if module_identity_delta is not None else {}),
                },
            ))
            self._link_activated_graph(work, workflow, epoch_id=epoch_id, source_epoch_id=source_epoch_id, actor=actor)
            return True

    def _link_activated_graph(
        self, work: BunshinUnitOfWork, workflow: AggregateSnapshot, *,
        epoch_id: str, source_epoch_id: str, actor: str,
    ) -> None:
        workflow_id = workflow.workflow_id
        if workflow.state == "ACTIVE" and workflow.payload.get("execution_epoch_id") != epoch_id:
            work.transitions.dispatch(ActionEnvelope(
                action_type="LINK_EXECUTION_EPOCH", workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW, aggregate_id=workflow_id,
                actor=actor, expected_version=workflow.version,
                idempotency_key=f"activate-graph:{epoch_id}:link", payload={"execution_epoch_id": epoch_id},
            ))
        if source_epoch_id:
            previous = work.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, source_epoch_id)
            if previous is not None and previous.state == "REPLAN_REQUIRED":
                work.transitions.dispatch(ActionEnvelope(
                    action_type="SUCCESSOR_EPOCH_STARTED", workflow_id=workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH, aggregate_id=source_epoch_id,
                    actor=actor, expected_version=previous.version,
                    idempotency_key=f"successor-epoch:{source_epoch_id}:{epoch_id}",
                    payload={"successor_execution_epoch_id": epoch_id},
                ))

    def reconcile_activated_graph_link(
        self, *, workflow_id: str, epoch_id: str, source_epoch_id: str, actor: str,
    ) -> None:
        """Compatibility recovery of a fully compiled historical epoch."""
        with self.repository.transaction() as work:
            workflow = self.assert_graph_preparation_allowed(
                work, workflow_id=workflow_id, epoch_id=epoch_id, source_epoch_id=source_epoch_id,
            )
            epoch = work.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
            if epoch is None or epoch.workflow_id != workflow_id or epoch.state != "RUNNING":
                raise SubmissionInvariantError("unlinked execution epoch is not a compiled generation")
            graph = work.cycles.read_graph_execution(workflow_id=workflow_id)
            if graph is None or graph.graph.generation != int(epoch.payload["graph_generation"]):
                raise SubmissionInvariantError("unlinked execution belongs to a superseded generation")
            bindings = graph_node_bindings(graph.graph, (
                node for node in work.queries.list_workflow_snapshots(workflow_id)
                if node.aggregate_type == AggregateType.DAG_NODE_RUN and node.payload.get("epoch_id") == epoch_id
            ))
            if set(epoch.payload.get("node_ids") or []) != {node.aggregate_id for node in bindings.values()}:
                raise SubmissionInvariantError("unlinked epoch has incomplete compiled membership")
            for name, node in bindings.items():
                if graph.cycles[name].state == NodeCycleState.ACCEPTED or node.state == "ACCEPTED":
                    accepted_node_receipt(node, graph.cycles[name])
            self._link_activated_graph(work, workflow, epoch_id=epoch_id, source_epoch_id=source_epoch_id, actor=actor)

    def begin_graph_activation(
        self, *, workflow_id: str, epoch_id: str, graph: GraphIR,
        manifest_ref: ArtifactRef, topology_ref: ArtifactRef, actor: str,
        source_epoch_id: str = "", skeleton_commit_sha: str = "",
        initial_repair_bill_ref: Mapping[str, object] | None = None,
    ) -> InstalledGraph:
        """Atomically pin the epoch and install its graph before materialization."""
        with self.repository.transaction() as work:
            self.assert_graph_preparation_allowed(
                work, workflow_id=workflow_id, epoch_id=epoch_id, source_epoch_id=source_epoch_id,
            )
            epoch = work.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
            if epoch is None:
                epoch = work.transitions.dispatch(ActionEnvelope(
                    action_type="CREATE_EXECUTION_EPOCH", workflow_id=workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH, aggregate_id=epoch_id,
                    actor=actor, expected_version=0, idempotency_key=f"{epoch_id}:CREATE_EXECUTION_EPOCH:0",
                    payload={"architecture_manifest_ref": manifest_ref.to_dict(), "topology_ref": topology_ref.to_dict(),
                             "architecture_manifest_sha": manifest_ref.sha256, "graph_generation": graph.generation,
                             "source_execution_epoch_id": source_epoch_id,
                             **({"skeleton_commit_sha": skeleton_commit_sha} if skeleton_commit_sha else {}),
                             **({"initial_repair_bill_ref": dict(initial_repair_bill_ref)} if initial_repair_bill_ref else {})},
                )).snapshot
            if (epoch.workflow_id != workflow_id or epoch.payload.get("graph_generation") != graph.generation
                    or epoch.payload.get("architecture_manifest_ref") != manifest_ref.to_dict()
                    or epoch.payload.get("source_execution_epoch_id", source_epoch_id) != source_epoch_id
                    or epoch.payload.get("initial_repair_bill_ref", dict(initial_repair_bill_ref or {})) != dict(initial_repair_bill_ref or {})):
                raise SubmissionInvariantError("execution epoch belongs to another pinned compilation")
            if epoch.state == "NOT_STARTED":
                work.transitions.dispatch(ActionEnvelope(
                    action_type="START_EXECUTION", workflow_id=workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH, aggregate_id=epoch_id,
                    actor=actor, expected_version=epoch.version, idempotency_key=f"{epoch_id}:START_EXECUTION:{epoch.version}",
                ))
            elif epoch.state != "STARTING":
                raise DeferredEffectError("execution epoch is not preparing")
            return self.install_graph(workflow_id=workflow_id, graph=graph, unit_of_work=work)

    def _settle_replan_installation(
        self, work: BunshinUnitOfWork, execution: GraphExecution,
        bindings: Mapping[str, AggregateSnapshot], source_epoch_id: str,
    ) -> GraphExecution:
        """Commit exact carried receipts, never tentative acceptance from a diff."""
        source_epoch = work.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, source_epoch_id)
        if source_epoch is None or source_epoch.workflow_id != execution.graph.graph_id:
            raise SubmissionInvariantError("replan source epoch is unavailable")
        source = work.cycles.read_graph_execution(workflow_id=source_epoch.workflow_id,
            generation=int(source_epoch.payload.get("graph_generation") or 0))
        if source is None:
            raise SubmissionInvariantError("replan source graph is unavailable")
        source_nodes = {str(node.payload.get("module_name") or node.payload.get("unit_id") or ""): node
            for node in work.queries.list_workflow_snapshots(source_epoch.workflow_id)
            if node.aggregate_type == AggregateType.DAG_NODE_RUN and node.payload.get("epoch_id") == source_epoch_id}
        cycles = dict(execution.cycles)
        artifacts = ContentAddressedArtifactStore(self.repository.runtime_root, self.repository.artifacts)
        for name, node in bindings.items():
            cycle = cycles[name]
            if node.state != "ACCEPTED":
                if cycle.state == NodeCycleState.ACCEPTED:
                    cycles[name] = replace(cycle, state=NodeCycleState.STALE, product_ref="", last_verdict=None)
                continue
            previous = source_nodes.get(name)
            if cycle.state != NodeCycleState.ACCEPTED or previous is None or name not in source.cycles:
                raise SubmissionInvariantError(f"node has no authorised carried acceptance: {name}")
            old = accepted_node_receipt(previous, source.cycles[name])
            candidate = ArtifactRef.from_mapping(dict(node.payload.get("candidate_ref") or {}))
            if (cycle.accepted_product_ref != old.candidate.sha256 or not candidate.sha256
                    or node.payload.get("carried_forward_from_epoch_id") != source_epoch_id
                    or node.payload.get("carried_forward_from_node_run_id") != previous.aggregate_id
                    or node.payload.get("candidate_digest") != old.candidate_digest
                    or node.payload.get("verification_artifact_ref") != old.verification.to_dict()):
                raise SubmissionInvariantError(f"carried receipt lost its verified identity: {name}")
            if candidate.sha256 != old.candidate.sha256:
                value = artifacts.read_json(candidate)
                if (value.get("carried_forward_from_candidate") != old.candidate.sha256
                        or value.get("candidate_digest") != old.candidate_digest):
                    raise SubmissionInvariantError(f"carried candidate does not identify its verified predecessor: {name}")
            cycles[name] = replace(cycle, product_ref=candidate.sha256, accepted_product_ref=candidate.sha256)
            accepted_node_receipt(node, cycles[name])
        for name, cycle in cycles.items():
            if cycle.state == NodeCycleState.ACCEPTED and any(
                cycles[provider].state != NodeCycleState.ACCEPTED for provider in execution.graph.checker_predecessors(name)
            ):
                raise SubmissionInvariantError(f"carried acceptance has an unaccepted dependency: {name}")
        return replace(execution, cycles=cycles, state=GraphExecutionState.RUNNING, published_sink_ref="").refresh()

    def request_workflow_pause(
        self,
        *,
        workflow_id: str,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        self._control_plan(workflow_id, CycleAction.REQUEST_PAUSE, unit_of_work=unit_of_work)
        self._control_graph(workflow_id, CycleAction.REQUEST_PAUSE, unit_of_work=unit_of_work)

    def pause_after_node_settlement(self, *, workflow_id: str, unit_of_work: BunshinUnitOfWork) -> None:
        """Apply a durable parent pause only after a submitted node result closes."""
        workflow = unit_of_work.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is not None and workflow_pauses_cycles(workflow):
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
        generation: int = 0,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        execution = (unit_of_work or self.repository).cycles.read_graph_execution(
            workflow_id=workflow_id,
        )
        if execution is None or node_name not in execution.cycles or (generation and execution.graph.generation != generation):
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
            stores = unit_of_work or self.repository
            workflow = stores.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
            revision_id = str(workflow.payload.get("architecture_revision_id") or "") if workflow else ""
            if any(role_pause_must_drain(revision, stores.queries) for revision in stores.queries.list_workflow_snapshots(workflow_id)
                   if revision.aggregate_type == AggregateType.ARCHITECTURE_REVISION
                   and (not revision_id or revision.aggregate_id == revision_id)):
                return
        if action == CycleAction.REQUEST_PAUSE:
            if cycle.state in {
                PlanCycleState.PAUSE_REQUESTED,
                PlanCycleState.PAUSED,
                PlanCycleState.TRIAGE_REQUIRED,
            }:
                # Child triage already satisfies the aggregate pause policy.
                # Nesting another pause would overwrite its original cursor.
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
        draining = {
            str(node.payload.get("module_name") or node.payload.get("unit_id") or "")
            for node in (unit_of_work or self.repository).queries.list_workflow_snapshots(workflow_id)
            if node.aggregate_type == AggregateType.DAG_NODE_RUN
            and int(node.payload.get("graph_generation") or 0) == execution.graph.generation
            and node_pause_must_drain(node, execution, (unit_of_work or self.repository).queries)
        } if action == CycleAction.REQUEST_PAUSE else set()
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
                if name in draining:
                    # The business submit already committed. Preserve the
                    # admitted cursor until its immutable result is published.
                    # Older pauses could have overwritten this cursor while
                    # retaining its exact active assignment.
                    if current.state == NodeCycleState.PAUSE_REQUESTED and current.active_assignment is not None:
                        cycles[name] = replace(current, resume_state=None, state=(
                            NodeCycleState.PRODUCING if current.active_assignment.slot == CycleSlot.PRODUCER
                            else NodeCycleState.CHECKING
                        ))
                        changed = True
                    continue
                if current.state in {
                    NodeCycleState.PAUSE_REQUESTED,
                    NodeCycleState.PAUSED,
                    NodeCycleState.TRIAGE_REQUIRED,
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
