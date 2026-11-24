from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

from pal.bunshin.coroutine_runtime import CoroutineRunPermit, CoroutineRunSemaphore, NodeSlot
from pal.bunshin.process_lifecycle import RoleProcessShell, WorkerProcessOwner


@dataclass
class RoleSupervisor:
    """Bind an admitted role attempt to its process shell and cleanup."""
    max_active_runs: int
    capacity_pool: CoroutineRunSemaphore | None = None
    _run_semaphore: CoroutineRunSemaphore = field(init=False)
    _current_permit: ContextVar[CoroutineRunPermit | None] = field(init=False)

    def __post_init__(self) -> None:
        self._run_semaphore = self.capacity_pool or CoroutineRunSemaphore(self.max_active_runs)
        self._current_permit = ContextVar(
            f"pal_bunshin_role_permit_{id(self)}",
            default=None,
        )

    @property
    def active_run_count(self) -> int:
        return self._run_semaphore.active_count

    @property
    def active_run_ids(self) -> frozenset[str]:
        return self._run_semaphore.active_run_ids

    async def acquire_process_slot(self, run_id: str, *, node: NodeSlot | None = None) -> CoroutineRunPermit:
        """Reserve capacity immediately before a process attempt is claimed."""

        current = self._current_permit.get()
        if current is not None and not current.released:
            raise RuntimeError("current logical coroutine already owns a process slot")
        permit = await self._run_semaphore.acquire(run_id, node=node)
        self._current_permit.set(permit)
        return permit

    async def release_process_slot(self, *, reserve_successor: bool = False) -> None:
        """Release an admitted slot even when materialization failed pre-spawn."""

        permit = self._current_permit.get()
        self._current_permit.set(None)
        if permit is not None:
            permit.attempt_finished = True
            if not permit.shell_owns_cleanup:
                await permit.release(reserve_successor=reserve_successor)

    def process_shell(
        self,
        owner: WorkerProcessOwner,
        *,
        run_id: str,
    ) -> RoleProcessShell:
        return RoleProcessShell(
            owner=owner,
            semaphore=self._run_semaphore,
            run_id=run_id,
            preacquired_permit=self._current_permit.get(),
        )
