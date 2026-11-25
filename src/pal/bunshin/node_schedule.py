from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from pal.bunshin.cycle_protocol import CycleAction, CycleSlot, CycleWork, NodeCycle, NodeCycleState
from pal.bunshin.graph_protocol import GraphIR


@dataclass(frozen=True)
class NodeSchedulingDecision:
    work: CycleWork | None
    retain_slot: bool


def decide_node(cycle: NodeCycle, *, dependencies_ready: bool, fenced: bool) -> NodeSchedulingDecision:
    """The finite scheduler boundary, also executed by the formal renderer."""
    work = cycle.pending_work if dependencies_ready and not fenced else None
    return NodeSchedulingDecision(work, not fenced and (cycle.is_running or work is not None))


@dataclass(frozen=True)
class NodeSchedule:
    """Read-only scheduling view of the current graph and its node cycles.

    There is no independently persisted scheduler cursor. NodeCycle owns the
    role to run; this view answers whether its dependencies permit dispatch.
    """

    graph: GraphIR
    cycles: Mapping[str, NodeCycle]
    repair_barriers: Mapping[str, tuple[str, ...]]
    fenced_nodes: frozenset[str]

    def dependencies_ready(self, name: str, slot: CycleSlot) -> bool:
        predecessors = (
            self.graph.checker_predecessors(name)
            if slot == CycleSlot.CHECKER
            else self.graph.producer_predecessors(name)
        )
        return all(
            self.cycles[dependency].state == NodeCycleState.ACCEPTED
            for dependency in (*predecessors, *self.repair_barriers.get(name, ()))
        )

    def runnable(self, name: str) -> bool:
        return self.decision(name).work is not None

    def decision(self, name: str) -> NodeSchedulingDecision:
        work = self.cycles[name].pending_work
        return decide_node(
            self.cycles[name],
            dependencies_ready=work is not None and self.dependencies_ready(name, work.slot),
            fenced=name in self.fenced_nodes,
        )

    def runnable_nodes(self) -> tuple[str, ...]:
        return tuple(sorted(name for name in self.cycles if self.runnable(name)))

    def retain_slot(self, name: str) -> bool:
        # An admitted assignment owns its slot until its durable result is
        # settled. After settlement, keep it only for a runnable successor.
        return self.decision(name).retain_slot

    def unblock_ready(self) -> dict[str, NodeCycle]:
        return {
            name: cycle.transition(CycleAction.UNBLOCK)
            if (name not in self.fenced_nodes
                and cycle.state in {NodeCycleState.BLOCKED, NodeCycleState.STALE}
                and self.dependencies_ready(name, CycleSlot.PRODUCER))
            else cycle
            for name, cycle in self.cycles.items()
        }

    def outstanding_barriers(self) -> dict[str, tuple[str, ...]]:
        return {
            name: providers
            for name, providers in self.repair_barriers.items()
            if not all(self.cycles[provider].state == NodeCycleState.ACCEPTED for provider in providers)
        }
