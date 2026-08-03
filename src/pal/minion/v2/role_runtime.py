from __future__ import annotations

from dataclasses import dataclass, field

from pal.minion.v2.coroutine_runtime import CoroutineRunSemaphore
from pal.minion.v2.process_lifecycle import RoleProcessShell, WorkerProcessOwner


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

    def __post_init__(self) -> None:
        self._run_semaphore = CoroutineRunSemaphore(self.max_active_runs)

    @property
    def active_run_count(self) -> int:
        return self._run_semaphore.active_count

    @property
    def active_run_ids(self) -> frozenset[str]:
        return self._run_semaphore.active_run_ids

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
        )
