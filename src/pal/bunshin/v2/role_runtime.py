from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field

from pal.bunshin.v2.coroutine_runtime import CoroutineRunPermit, CoroutineRunSemaphore
from pal.bunshin.v2.process_lifecycle import RoleProcessShell, WorkerProcessOwner


@dataclass
class RoleSupervisor:
    """Account only materialized role processes.

    Durable role sessions, checkpoints, and restoration already belong to the
    role protocol repository.  This object deliberately knows nothing about
    those logical coroutines: it owns only the semaphore consumed immediately
    before an OS process is spawned.
    """

    max_active_runs: int
    _run_semaphore: CoroutineRunSemaphore = field(init=False)
    _current_permit: ContextVar[CoroutineRunPermit | None] = field(init=False)

    def __post_init__(self) -> None:
        self._run_semaphore = CoroutineRunSemaphore(self.max_active_runs)
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

    async def acquire_process_slot(self, run_id: str) -> CoroutineRunPermit:
        """Reserve capacity immediately before a process attempt is claimed."""

        current = self._current_permit.get()
        if current is not None and not current.released:
            raise RuntimeError("current logical coroutine already owns a process slot")
        permit = await self._run_semaphore.acquire(run_id)
        self._current_permit.set(permit)
        return permit

    async def release_process_slot(self) -> None:
        """Release an admitted slot even when materialization failed pre-spawn."""

        permit = self._current_permit.get()
        self._current_permit.set(None)
        if permit is not None:
            await permit.release()

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
