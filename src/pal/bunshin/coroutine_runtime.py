from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pal.bunshin.workspace_resources import workspace_process_holders


@dataclass(frozen=True)
class NodeSlot:
    workflow_id: str
    epoch_id: str
    generation: int
    node_id: str


@dataclass
class CoroutineRunPermit:
    _semaphore: "CoroutineRunSemaphore"
    run_id: str
    node: NodeSlot | None = None
    assignment_id: str = ""
    shell_owns_cleanup: bool = False
    attempt_finished: bool = False
    _released: bool = field(default=False, init=False)

    @property
    def released(self) -> bool:
        return self._released

    async def release(self, *, reserve_successor: bool = False) -> None:
        if self._released:
            return
        await self._semaphore.release(self.run_id, reserve=self.node if reserve_successor else None,
                                      predecessor=self.assignment_id)
        self._released = True

    async def __aenter__(self) -> "CoroutineRunPermit":
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self.release()


@dataclass
class CoroutineRunSemaphore:
    """Charge each active role, inherited workspace holder, or between-role node
    reservation once. The DAG decides whether a node can retain its reservation.
    """

    capacity: int
    retain_node: Callable[[NodeSlot], bool] | None = None
    predecessor_settled: Callable[[str], bool] | None = None
    _inherited: dict[str, tuple[str, Path]] = field(default_factory=dict, init=False)
    _reservations: dict[NodeSlot, str] = field(default_factory=dict, init=False)
    _active_nodes: dict[str, NodeSlot] = field(default_factory=dict, init=False)
    _active_run_ids: set[str] = field(default_factory=set, init=False)
    _condition: asyncio.Condition = field(
        default_factory=asyncio.Condition,
        init=False,
    )

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("coroutine run capacity must be positive")

    @property
    def active_count(self) -> int:
        return len(self._active_run_ids)

    @property
    def active_run_ids(self) -> frozenset[str]:
        return frozenset(self._active_run_ids)

    @property
    def reserved_nodes(self) -> frozenset[NodeSlot]:
        return frozenset(set(self._reservations) | set(self._active_nodes.values()))

    @property
    def inherited_count(self) -> int:
        return len(self._inherited)

    async def recover_occupancy(self, rows: tuple[tuple[str, str, str], ...]) -> None:
        self._inherited = {run: (aggregate, Path(workspace)) for run, aggregate, workspace in rows}
        await self.recheck_occupancy()

    async def recheck_occupancy(self) -> None:
        if not self._inherited:
            return
        candidates = dict(self._inherited)
        clean = await asyncio.to_thread(
            lambda: [run for run, (_, workspace) in candidates.items() if not workspace_process_holders(workspace)]
        )
        async with self._condition:
            for run in clean:
                self._inherited.pop(run, None)
            if clean:
                self._condition.notify_all()

    async def resize(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("role capacity must be positive")
        async with self._condition:
            self.capacity = capacity
            self._condition.notify_all()

    async def reconcile_reservations(self) -> None:
        async with self._condition:
            stale = {node for node in self._reservations
                     if self.retain_node is None or not self.retain_node(node)}
            if stale:
                for node in stale:
                    del self._reservations[node]
            if stale or self._reservations:
                self._condition.notify_all()

    def _available(self, run_id: str, node: NodeSlot | None) -> bool:
        if run_id in self._inherited:
            return False
        if node is not None:
            if any(aggregate == node.node_id for aggregate, _ in self._inherited.values()):
                return False
            if node in self._active_nodes.values():
                return False
            if node in self._reservations:
                previous = self._reservations[node]
                return not previous or self.predecessor_settled is None or self.predecessor_settled(previous)
        return len(self._active_run_ids) + len(self._reservations) + self.inherited_count < self.capacity

    async def acquire(self, run_id: str, *, node: NodeSlot | None = None) -> CoroutineRunPermit:
        if not run_id:
            raise ValueError("run_id is required")
        async with self._condition:
            if run_id in self._active_run_ids:
                raise RuntimeError(f"coroutine run already active: {run_id}")
            await self._condition.wait_for(lambda: self._available(run_id, node))
            if run_id in self._active_run_ids:
                raise RuntimeError(f"coroutine run already active: {run_id}")
            if node is not None:
                self._reservations.pop(node, None)
                self._active_nodes[run_id] = node
            self._active_run_ids.add(run_id)
        return CoroutineRunPermit(self, run_id, node)

    async def try_acquire(self, run_id: str) -> CoroutineRunPermit | None:
        if not run_id:
            raise ValueError("run_id is required")
        async with self._condition:
            if run_id in self._active_run_ids:
                raise RuntimeError(f"coroutine run already active: {run_id}")
            if not self._available(run_id, None):
                return None
            self._active_run_ids.add(run_id)
        return CoroutineRunPermit(self, run_id)

    async def release(self, run_id: str, *, reserve: NodeSlot | None = None, predecessor: str = "") -> None:
        async with self._condition:
            if run_id not in self._active_run_ids:
                raise RuntimeError(f"coroutine run is not active: {run_id}")
            self._active_run_ids.remove(run_id)
            node = self._active_nodes.pop(run_id, None)
            if reserve is not None and node == reserve:
                self._reservations[reserve] = predecessor
            self._condition.notify_all()
