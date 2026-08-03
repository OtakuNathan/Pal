from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Mapping

from pal.minion.v2.execution import WorkspaceLockRegistry, terminate_process_group
from pal.minion.v2.coroutine_runtime import (
    CoroutineRunPermit,
    CoroutineRunSemaphore,
)


class WorkerProcessReapError(RuntimeError):
    """The worker owner could not prove that its complete process group exited."""


WorkerOwnerCallback = Callable[["WorkerProcessOwner"], None]
WorkerHeartbeatFactory = Callable[[], Awaitable[None]]


@dataclass
class WorkerProcessOwner:
    """RAII owner for one worker process group and its worktree occupancy.

    Registration, lease heartbeats, the process group, and the worktree lock
    have one lifetime.  A leader exit is not a worker exit: the owner remains
    registered until every process in the group has been reaped.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str]
    invocation_id: str
    run_id: str
    workspace: Path | None
    workspace_locks: WorkspaceLockRegistry
    on_started: WorkerOwnerCallback
    on_registered: WorkerOwnerCallback
    on_unregistered: WorkerOwnerCallback
    heartbeat_factories: tuple[WorkerHeartbeatFactory, ...] = ()
    reap_timeout_seconds: float = 5.0
    process: asyncio.subprocess.Process | None = field(default=None, init=False)
    process_group_id: int = field(default=0, init=False)
    lock_path: Path | None = field(default=None, init=False)
    stderr: bytes = field(default=b"", init=False)
    process_group_reaped: bool = field(default=False, init=False)
    _registered: bool = field(default=False, init=False)
    _closed: bool = field(default=False, init=False)
    _heartbeat_tasks: list[asyncio.Task[None]] = field(default_factory=list, init=False)
    _stderr_task: asyncio.Task[bytes] | None = field(default=None, init=False)
    _leader_exit_task: asyncio.Task[None] | None = field(default=None, init=False)
    _reap_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    @property
    def lock_key(self) -> str:
        return f"worker:{self.invocation_id}"

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def resources_released(self) -> bool:
        return (
            self._closed
            and not self._registered
            and (
                self.process is None
                or self.process_group_reaped
            )
        )

    async def __aenter__(self) -> "WorkerProcessOwner":
        if self.process is not None:
            raise RuntimeError("worker process owner cannot be entered twice")
        if self.workspace is not None:
            self.lock_path = self.workspace_locks.acquire(
                self.lock_key,
                self.workspace,
            )
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                env=dict(self.env),
                start_new_session=True,
            )
            self.process_group_id = int(self.process.pid)
            self.on_started(self)
            # Mark registration before invoking the callback so a partially
            # completed callback is always paired with an unregister attempt.
            self._registered = True
            self.on_registered(self)
            self._heartbeat_tasks = [
                asyncio.create_task(factory())
                for factory in self.heartbeat_factories
            ]
            if self.process.stderr is not None:
                self._stderr_task = asyncio.create_task(self.process.stderr.read())
            self._leader_exit_task = asyncio.create_task(
                self._reap_after_leader_exit(),
                name=f"minion-worker-owner-{self.invocation_id}",
            )
            return self
        except BaseException:
            await self._close_shielded()
            raise

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self._close_shielded()

    async def _close_shielded(self) -> None:
        close_task = asyncio.create_task(self.close())
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            # Process cleanup is a cancellation boundary.  Preserve the
            # caller's cancellation only after the owned resources are closed.
            await close_task
            raise

    async def _reap_after_leader_exit(self) -> None:
        process = self._require_process()
        await process.wait()
        await self._ensure_process_group_reaped()

    async def _ensure_process_group_reaped(self) -> None:
        async with self._reap_lock:
            if self.process_group_reaped:
                return
            process = self._require_process()
            group_reap = asyncio.create_task(
                terminate_process_group(
                    self.process_group_id,
                    timeout_seconds=self.reap_timeout_seconds,
                )
            )
            leader_wait = asyncio.create_task(process.wait())
            reaped, _returncode = await asyncio.gather(group_reap, leader_wait)
            if not reaped:
                raise WorkerProcessReapError(
                    "worker process group could not be reaped; "
                    "run registration and worktree ownership remain held"
                )
            self.process_group_reaped = True

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            if self.process is not None:
                await self._ensure_process_group_reaped()
                if self._leader_exit_task is not None:
                    await self._leader_exit_task
                if self._stderr_task is not None:
                    self.stderr = await self._stderr_task

            # Leases remain live until process-group cleanup succeeds.  Only
            # then may Manager accounting say that the worker has exited.
            for task in self._heartbeat_tasks:
                task.cancel()
            if self._heartbeat_tasks:
                await asyncio.gather(
                    *self._heartbeat_tasks,
                    return_exceptions=True,
                )
            self._heartbeat_tasks.clear()

            if self._registered:
                self.on_unregistered(self)
                self._registered = False
            if self.workspace is not None:
                self.workspace_locks.release(self.lock_key)
            self._closed = True

    def _require_process(self) -> asyncio.subprocess.Process:
        if self.process is None:
            raise RuntimeError("worker process has not started")
        return self.process

    async def write_control(self, message: bytes) -> bool:
        process = self.process
        if (
            process is None
            or process.returncode is not None
            or process.stdin is None
            or process.stdin.is_closing()
        ):
            return False
        try:
            process.stdin.write(message)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionError):
            return False
        return True


@dataclass
class RoleProcessShell:
    """Bind one materialized worker to exactly one coroutine-run permit.

    The permit is deliberately outside ``WorkerProcessOwner``: process
    ownership proves OS cleanup, while the semaphore accounts logical worker
    incarnations.  Release is legal only after the owner proves its process
    group, broker registration, heartbeats, and workspace lock are closed.
    """

    owner: WorkerProcessOwner
    semaphore: CoroutineRunSemaphore
    run_id: str
    _permit: CoroutineRunPermit | None = field(default=None, init=False)
    _entered: bool = field(default=False, init=False)

    async def __aenter__(self) -> WorkerProcessOwner:
        if self._entered:
            raise RuntimeError("role process shell cannot be entered twice")
        self._permit = await self.semaphore.acquire(self.run_id)
        try:
            result = await self.owner.__aenter__()
        except BaseException:
            if self.owner.resources_released:
                await self._release_permit()
            raise
        self._entered = True
        return result

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            await self.owner.__aexit__(exc_type, exc, traceback)
        finally:
            # A failed reap intentionally keeps the permit occupied. Manager
            # must finish cleanup before it may advertise capacity again.
            if self.owner.resources_released:
                await self._release_permit()

    async def close(self) -> None:
        await self.owner.close()
        if not self.owner.resources_released:
            raise WorkerProcessReapError(
                "role process shell cannot release capacity before cleanup"
            )
        await self._release_permit()

    async def _release_permit(self) -> None:
        permit = self._permit
        if permit is None:
            return
        self._permit = None
        await permit.release()
