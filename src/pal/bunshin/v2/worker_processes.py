"""Manager-local process accounting; destructive authority stays with each owner."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from pal.bunshin.v2.process_lifecycle import WorkerProcessOwner


@dataclass
class WorkerProcesses:
    _owners: dict[str, WorkerProcessOwner] = field(default_factory=dict, init=False)
    _invocations_by_run: dict[str, str] = field(default_factory=dict, init=False)

    def contains(self, invocation_id: str) -> bool:
        return invocation_id in self._owners

    def register(self, owner: WorkerProcessOwner) -> None:
        if owner.invocation_id in self._owners or owner.run_id in self._invocations_by_run:
            raise RuntimeError(f"logical worker {owner.invocation_id} already owns a process")
        self._owners[owner.invocation_id] = owner
        self._invocations_by_run[owner.run_id] = owner.invocation_id

    def unregister(
        self,
        owner: WorkerProcessOwner,
        *,
        before_remove: Callable[[str, bool], None] | None = None,
    ) -> None:
        if not owner.process_group_reaped:
            raise RuntimeError("worker process accounting cannot close before child cleanup")
        if self._owners.get(owner.invocation_id) is not owner:
            return
        if before_remove is not None:
            before_remove(owner.run_id, True)
        self._owners.pop(owner.invocation_id)
        if self._invocations_by_run.get(owner.run_id) == owner.invocation_id:
            self._invocations_by_run.pop(owner.run_id)

    async def send_control(self, run_id: str, message: Mapping[str, object]) -> bool:
        invocation_id = self._invocations_by_run.get(run_id, '')
        owner = self._owners.get(invocation_id)
        if owner is None:
            return False
        payload = (json.dumps(dict(message), ensure_ascii=False) + '\n').encode('utf-8')
        return await owner.write_control(payload)

    async def close(self, invocation_id: str) -> None:
        owner = self._owners.get(invocation_id)
        if owner is not None:
            await owner.close()

    async def close_all(self) -> None:
        owners = tuple(self._owners.values())
        results = await asyncio.gather(*(owner.close() for owner in owners), return_exceptions=True)
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise RuntimeError(
                'worker supervisor stopped before every owned process was cleaned up'
            ) from failures[0]
        if self._owners or self._invocations_by_run:
            raise RuntimeError('worker supervisor retained live process accounting')
