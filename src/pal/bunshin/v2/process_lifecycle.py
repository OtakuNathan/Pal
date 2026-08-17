from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Mapping

from pal.bunshin.v2.execution import WorkspaceLockRegistry
from pal.bunshin.v2.coroutine_runtime import (
    CoroutineRunPermit,
    CoroutineRunSemaphore,
)


class WorkerProcessReapError(RuntimeError):
    """The worker owner could not terminate and reap its direct child."""


WorkerOwnerCallback = Callable[["WorkerProcessOwner"], None]
WorkerHeartbeatFactory = Callable[[], Awaitable[None]]


@dataclass
class WorkerProcessOwner:
    """Owner for one currently reachable worker and its worktree occupancy.

    The private process reference is the only destructive authority.  A PID is
    derived from that object only for the synchronous group-kill handoff and is
    never persisted or retried after the reference is withdrawn.
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
    _process: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    lock_path: Path | None = field(default=None, init=False)
    stderr: bytes = field(default=b"", init=False)
    process_group_reaped: bool = field(default=False, init=False)
    _registered: bool = field(default=False, init=False)
    _closed: bool = field(default=False, init=False)
    _returncode: int | None = field(default=None, init=False, repr=False)
    _termination_sent: bool = field(default=False, init=False, repr=False)
    _stdin: asyncio.StreamWriter | None = field(default=None, init=False, repr=False)
    _stdout: asyncio.StreamReader | None = field(default=None, init=False, repr=False)
    _heartbeat_tasks: list[asyncio.Task[None]] = field(default_factory=list, init=False)
    _stderr_task: asyncio.Task[bytes] | None = field(default=None, init=False)
    _leader_exit_task: asyncio.Task[None] | None = field(default=None, init=False)
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
            and self._process is None
            and self.process_group_reaped
        )

    @property
    def pid(self) -> int:
        process = self._process
        return int(process.pid) if process is not None else 0

    @property
    def returncode(self) -> int | None:
        process = self._process
        if process is not None and process.returncode is not None:
            self._record_exit(process, int(process.returncode))
        return self._returncode

    async def wait(self) -> int:
        process = self._process
        if process is not None:
            self._record_exit(process, int(await process.wait()))
        elif self._leader_exit_task is not None:
            await asyncio.shield(self._leader_exit_task)
        if self._returncode is None:
            raise WorkerProcessReapError("worker exited without a return code")
        return self._returncode

    async def stdout_lines(self) -> AsyncIterator[bytes]:
        stdout = self._stdout
        if stdout is None:
            raise WorkerProcessReapError("worker process has no stdout pipe")
        while True:
            line = await stdout.readline()
            if not line:
                return
            yield line

    async def __aenter__(self) -> "WorkerProcessOwner":
        if self._closed or self._process is not None:
            raise RuntimeError("worker process owner cannot be entered twice")
        if self.workspace is not None:
            self.lock_path = self.workspace_locks.acquire(
                self.lock_key,
                self.workspace,
            )
        try:
            process = await asyncio.create_subprocess_exec(
                *self.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                env=dict(self.env),
                start_new_session=True,
            )
            self._process = process
            self._stdin = process.stdin
            self._stdout = process.stdout
            self.on_started(self)
            # Mark registration before invoking the callback so a partially
            # completed callback is always paired with an unregister attempt.
            self._registered = True
            self.on_registered(self)
            self._heartbeat_tasks = [
                asyncio.create_task(factory())
                for factory in self.heartbeat_factories
            ]
            if process.stderr is not None:
                self._stderr_task = asyncio.create_task(process.stderr.read())
            self._leader_exit_task = asyncio.create_task(
                self._reap_after_leader_exit(process),
                name=f"bunshin-worker-owner-{self.invocation_id}",
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

    async def _reap_after_leader_exit(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        self._record_exit(process, int(await process.wait()))

    def _record_exit(
        self,
        process: asyncio.subprocess.Process,
        returncode: int,
    ) -> None:
        self._returncode = int(returncode)
        if self._process is process:
            self._process = None
        self.process_group_reaped = True

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            process = self._process
            # Withdraw the only published authority before signalling.  The
            # local reference below exists solely to issue one terminal signal
            # and reap the direct child.
            self._process = None
            if process is not None and process.returncode is None and not self._termination_sent:
                self._termination_sent = True
                with contextlib.suppress(ProcessLookupError):
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
            if process is not None:
                try:
                    returncode = await asyncio.wait_for(
                        asyncio.shield(process.wait()),
                        timeout=self.reap_timeout_seconds,
                    )
                except asyncio.TimeoutError as exc:
                    raise WorkerProcessReapError(
                        "worker leader did not exit after its one-shot termination; "
                        "registration and worktree ownership remain fenced"
                    ) from exc
                self._record_exit(process, int(returncode))
            elif self._leader_exit_task is None:
                self.process_group_reaped = True

            if self._leader_exit_task is not None:
                await asyncio.shield(self._leader_exit_task)
            if self._stderr_task is not None:
                self.stderr = await self._stderr_task
            stdin = self._stdin
            if stdin is not None and not stdin.is_closing():
                stdin.close()
                with contextlib.suppress(Exception):
                    await stdin.wait_closed()
            self._stdin = None
            self._stdout = None

            # Manager accounting and workspace ownership remain live until the
            # direct child and its parent-owned pipes are finished.
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
        if self._process is None:
            raise RuntimeError("worker process has not started")
        return self._process

    async def write_control(self, message: bytes) -> bool:
        process = self._process
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
    ownership proves direct-child cleanup, while the semaphore accounts
    logical worker incarnations. Release is legal only after the owner closes
    its process reference, broker registration, heartbeats, and workspace
    lock.
    """

    owner: WorkerProcessOwner
    semaphore: CoroutineRunSemaphore
    run_id: str
    preacquired_permit: CoroutineRunPermit | None = None
    _permit: CoroutineRunPermit | None = field(default=None, init=False)
    _entered: bool = field(default=False, init=False)

    async def __aenter__(self) -> WorkerProcessOwner:
        if self._entered:
            raise RuntimeError("role process shell cannot be entered twice")
        permit = self.preacquired_permit
        if permit is not None:
            if permit.released:
                raise RuntimeError("preacquired coroutine run permit is already released")
            self._permit = permit
        else:
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
