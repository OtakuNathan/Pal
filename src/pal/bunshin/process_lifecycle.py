from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Mapping

from pal.bunshin.workspace_resources import WorkspaceLockRegistry
from pal.bunshin.coroutine_runtime import (
    CoroutineRunPermit,
    CoroutineRunSemaphore,
)


class WorkerProcessReapError(RuntimeError):
    """The worker owner could not terminate and reap its process group."""


def _process_group_live(pgid: int) -> bool:
    """Observe live group members; zombies have closed their file descriptors."""
    proc = Path("/proc")
    if sys.platform.startswith("linux"):
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                # comm may contain spaces and parentheses; split after its
                # closing delimiter to locate state, ppid, and pgrp reliably.
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
                return True
        return False
    listing = subprocess.run(
        ["ps", "-axo", "pgid=,stat="], capture_output=True, text=True,
        check=True, timeout=2,
    )
    return any(
        int(fields[0]) == pgid and not fields[1].startswith(("Z", "X"))
        for line in listing.stdout.splitlines()
        if len(fields := line.split()) == 2
    )


WorkerOwnerCallback = Callable[["WorkerProcessOwner"], None]
WorkerHeartbeatFactory = Callable[[], Awaitable[None]]


@dataclass
class WorkerProcessOwner:
    """Owner for one currently reachable worker and its worktree occupancy.

    The private process reference owns a live group supervisor. It survives
    worker exit, so its PID remains safe for the one-shot group-kill handoff.
    After withdrawing this authority, group IDs are used only for observation.
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
    effect_key: str = ""
    assignment_id: str = ""
    attempt_id: str = ""
    business_lease_resource_key: str = ""
    business_fencing_token: int = 0
    on_reserved: WorkerOwnerCallback | None = None
    reap_timeout_seconds: float = 5.0
    memory_read_lease_factory: Callable[[], contextlib.AbstractContextManager[None]] | None = None
    _memory_read_lease: contextlib.AbstractContextManager[None] | None = field(default=None, init=False, repr=False)
    _process: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = field(default=None, init=False, repr=False)
    _spawn_adopted: bool = field(default=False, init=False)
    _closing: bool = field(default=False, init=False)
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
    _stdout_drain_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _leader_exit_task: asyncio.Task[None] | None = field(default=None, init=False)
    _status_reader: asyncio.StreamReader | None = field(default=None, init=False, repr=False)
    _status_writer: asyncio.StreamWriter | None = field(default=None, init=False, repr=False)
    _group_id: int = field(default=0, init=False, repr=False)
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _stdout_read_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

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
        return self._returncode

    async def wait(self) -> int:
        if self._leader_exit_task is not None:
            await asyncio.shield(self._leader_exit_task)
        if self._returncode is None:
            raise WorkerProcessReapError("worker exited without a return code")
        await self._confirm_group_reaped()
        return self._returncode

    async def stdout_lines(self) -> AsyncIterator[bytes]:
        stdout = self._stdout
        if stdout is None:
            raise WorkerProcessReapError("worker process has no stdout pipe")
        while True:
            async with self._stdout_read_lock:
                if self._closing:
                    return
                # JSON diagnostic frames can exceed StreamReader's line limit.
                # Drain complete chunks without dropping any part of the frame.
                chunks = []
                while True:
                    try:
                        chunks.append(await stdout.readuntil(b"\n"))
                        break
                    except asyncio.LimitOverrunError as exc:
                        chunks.append(await stdout.readexactly(exc.consumed))
                    except asyncio.IncompleteReadError as exc:
                        chunks.append(exc.partial)
                        break
                line = b"".join(chunks)
            if not line:
                return
            yield line

    async def __aenter__(self) -> "WorkerProcessOwner":
        if self._closed or self._process is not None or self._spawn_task is not None:
            raise RuntimeError("worker process owner cannot be entered twice")
        try:
            if self.on_reserved is not None:
                self._registered = True
                self.on_reserved(self)
            if self.workspace is not None:
                self.lock_path = self.workspace_locks.acquire(
                    self.lock_key,
                    self.workspace,
                )
            if self.memory_read_lease_factory is not None:
                lease = self.memory_read_lease_factory()
                lease.__enter__()
                self._memory_read_lease = lease
            # Keep the spawn future owned even when cancellation arrives before
            # asyncio returns the child object. Cleanup joins it and adopts/reaps
            # the child before releasing workspace or process capacity.
            self._spawn_task = asyncio.create_task(self._spawn_worker())
            process = await asyncio.shield(self._spawn_task)
            self._adopt_spawned_process(process)
            if self._closing:
                raise WorkerProcessReapError("worker was retired during process startup")
            self.on_started(self)
            # Mark registration before invoking the callback so a partially
            # completed callback is always paired with an unregister attempt.
            self._registered = True
            self.on_registered(self)
            self._heartbeat_tasks = [
                asyncio.create_task(factory())
                for factory in self.heartbeat_factories
            ]
            return self
        except BaseException:
            await self._close_shielded()
            raise

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self._close_shielded()

    async def _spawn_worker(self) -> asyncio.subprocess.Process:
        options = dict(
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE, env=dict(self.env), start_new_session=True,
        )
        if os.name == "nt":
            return await asyncio.create_subprocess_exec(*self.argv, **options)
        parent, child = socket.socketpair()
        try:
            parent.setblocking(False)
            self._status_reader, self._status_writer = await asyncio.open_connection(sock=parent)
            supervisor = Path(__file__).with_name("process_group_supervisor.py")
            return await asyncio.create_subprocess_exec(
                sys.executable, str(supervisor), str(child.fileno()), *self.argv,
                pass_fds=(child.fileno(),), **options,
            )
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()

    def _adopt_spawned_process(self, process: asyncio.subprocess.Process) -> None:
        if self._spawn_adopted:
            return
        self._spawn_adopted = True
        self._process = process
        self._group_id = int(process.pid)
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._leader_exit_task = asyncio.create_task(
            self._reap_after_leader_exit(process),
            name=f"bunshin-worker-owner-{self.invocation_id}",
        )
        if process.stderr is not None:
            self._stderr_task = asyncio.create_task(process.stderr.read())

    async def _close_shielded(self) -> None:
        close_task = asyncio.create_task(self.close())
        cancelled = False
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                # Repeated cancellation must not detach the only spawn/reap
                # owner and falsely advertise that this task is quiescent.
                cancelled = True
        close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _reap_after_leader_exit(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        if self._status_reader is not None:
            status = await self._status_reader.readline()
            if status:
                self._returncode = int(status)
                self._terminate_group(process)
        returncode = int(await process.wait())
        if self._returncode is None:
            self._returncode = returncode
        if self._process is process:
            self._process = None

    async def _confirm_group_reaped(self) -> None:
        if self.process_group_reaped:
            return
        if self._group_id and os.name != "nt":
            deadline = asyncio.get_running_loop().time() + self.reap_timeout_seconds
            while await asyncio.to_thread(_process_group_live, self._group_id):
                if asyncio.get_running_loop().time() >= deadline:
                    raise WorkerProcessReapError(
                        "worker process group remains live; registration and worktree ownership remain fenced"
                    )
                await asyncio.sleep(.01)
        self.process_group_reaped = True

    def _terminate_group(self, process: asyncio.subprocess.Process) -> None:
        if self._termination_sent:
            return
        # The supervisor stays alive after reporting the worker's exit. Never
        # signal a numeric group ID after that supervisor has already exited.
        self._process = None
        if process.returncode is not None:
            return
        self._termination_sent = True
        with contextlib.suppress(ProcessLookupError):
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)

    async def _drain_stdout(self) -> None:
        # A paused StreamReader can keep Process.wait() pending even after
        # SIGKILL. Serialize with the event consumer and discard in bounded
        # chunks once this incarnation is closing.
        async with self._stdout_read_lock:
            if self._stdout is not None:
                while await self._stdout.read(65536):
                    pass

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            first_close = not self._closing
            self._closing = True
            if self._spawn_task is not None and not self._spawn_adopted:
                try:
                    spawned = await asyncio.shield(self._spawn_task)
                except Exception:
                    # A failed native spawn produced no child. If it produced
                    # one, its successful result must be reaped below.
                    if not self._spawn_task.done():
                        raise
                else:
                    self._adopt_spawned_process(spawned)
            process = self._process
            if process is not None:
                self._terminate_group(process)
            if self._stdout is not None and self._stdout_drain_task is None:
                self._stdout_drain_task = asyncio.create_task(self._drain_stdout())
            if self._leader_exit_task is not None and (process is not None or first_close):
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._leader_exit_task),
                        timeout=self.reap_timeout_seconds,
                    )
                except asyncio.TimeoutError as exc:
                    raise WorkerProcessReapError(
                        "worker group supervisor or pipes did not close after its one-shot termination; "
                        "registration and worktree ownership remain fenced"
                    ) from exc
            elif self._leader_exit_task is None:
                self.process_group_reaped = True

            if self._leader_exit_task is not None:
                await asyncio.shield(self._leader_exit_task)
            await self._confirm_group_reaped()
            if self._status_writer is not None:
                self._status_writer.close()
                with contextlib.suppress(ConnectionError):
                    await self._status_writer.wait_closed()
                self._status_writer = None
                self._status_reader = None
            if self._stderr_task is not None:
                self.stderr = await self._stderr_task
            if self._stdout_drain_task is not None:
                await asyncio.shield(self._stdout_drain_task)
            stdin = self._stdin
            if stdin is not None and not stdin.is_closing():
                stdin.close()
                with contextlib.suppress(Exception):
                    await stdin.wait_closed()
            self._stdin = None
            self._stdout = None

            # Manager accounting and workspace ownership remain live until the
            # complete process group and its parent-owned pipes are finished.
            for task in self._heartbeat_tasks:
                task.cancel()
            if self._heartbeat_tasks:
                await asyncio.gather(
                    *self._heartbeat_tasks,
                    return_exceptions=True,
                )
            self._heartbeat_tasks.clear()

            # Keep host memory sidecars alive until the worker and its pipes
            # are gone. Failed reaping deliberately retains this ownership.
            if self._memory_read_lease is not None:
                self._memory_read_lease.__exit__(None, None, None)
                self._memory_read_lease = None
            if self._registered:
                self.on_unregistered(self)
                self._registered = False
            if self.lock_path is not None:
                self.workspace_locks.release(self.lock_key)
            self._closed = True

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
    ownership proves process-group cleanup, while the semaphore accounts
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
