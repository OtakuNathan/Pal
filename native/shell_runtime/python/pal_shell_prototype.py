"""Opt-in adapter for the standalone native prototype; never imported by Pal startup."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
import itertools
from pathlib import Path
import threading
import weakref

import _pal_shell_runtime as native

TERMINAL = frozenset({"exited", "cancelled", "timed_out", "failed"})
READ_EFFECTS = frozenset({"none", "local_read", "external_read"})


class ShellRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class Completion:
    session_id: int
    origin_turn: str
    result: dict


class ShellRuntime:
    def __init__(self, *, completed_capacity: int = 32, on_ready=None):
        self.loop = asyncio.get_running_loop()
        self.loop_thread = threading.get_ident()
        self.native = native.Runtime(completed_capacity)
        self._sequence = itertools.count(1)
        self._pending: dict[int, tuple[asyncio.Future, str, str]] = {}
        self._owners: dict[int, str] = {}
        self.completion_budgets = {}
        self._foreground: dict[asyncio.Task, str] = {}
        self._completions: dict[int, Completion] = {}
        self._seen: OrderedDict[int, None] = OrderedDict()
        self._consumed: set[int] = set()
        self._closed = False
        self._closing: asyncio.Task | None = None
        self._on_ready = on_ready
        self.delivery_threads: set[int] = set()
        reference = weakref.ref(self)
        loop = self.loop

        def callback(event):
            # This callback runs in the GIL executor, not on the asyncio thread.
            loop.call_soon_threadsafe(deliver, event)

        def deliver(event):
            instance = reference()
            if instance is not None:
                instance._deliver(event)

        native.connect(self.native, callback)

    def _deliver(self, event: dict) -> None:
        self.delivery_threads.add(threading.get_ident())
        assert threading.get_ident() == self.loop_thread
        event = dict(event)
        request = event["request_id"]
        session = event["session_id"]
        if request:
            pending = self._pending.get(request)
            if pending is None:
                return
            future, operation, turn = pending
            if operation == "run" and session:
                self._owners[session] = turn
            if not future.done():
                future.set_result(event)
            return
        if session in self._seen or self._closed:
            return
        self._seen[session] = None
        if len(self._seen) > 256:
            previous, _ = self._seen.popitem(last=False)
            self._consumed.discard(previous)
        origin = self._owners.pop(session, "")
        if session in self._consumed:
            return
        self._completions[session] = Completion(session, origin, event)
        if self._on_ready:
            self._on_ready()

    async def _request(self, operation: str, *args, turn: str = "", cleanup: bool = False) -> dict:
        if (self._closed or self._closing is not None) and not cleanup:
            raise ShellRejected("shell runtime is closed")
        request = next(self._sequence)
        future = self.loop.create_future()
        self._pending[request] = future, operation, turn
        try:
            getattr(self.native, operation)(request, *args)
            try:
                result = await asyncio.shield(future)
            except asyncio.CancelledError:
                # Cancel the subscription/foreground operation, not an acknowledged background session.
                await self._request("cancel_request", request, cleanup=True)
                result = await asyncio.shield(future)
                if operation == "acquire_write" and result["status"] == "write_acquired":
                    await self._request("release_write", result["session_id"], cleanup=True)
                if operation == "run" and result["session_id"]:
                    session = result["session_id"]
                    await self._discard_cancelled_session(session)
                elif operation == "run" and result.get("output_id"):
                    await self._request("release_output", result["output_id"], cleanup=True)
                raise
            if result["status"] == "rejected":
                raise ShellRejected(result["error"])
            return result
        finally:
            self._pending.pop(request, None)

    async def run(self, cmd: str, *, cwd: str = "", tty: bool = False,
                  wait_ms: int | None = None, timeout_ms: int | None = None,
                  shell: str = "/bin/bash", turn_id: str = "", retain_output: bool = False, inline_limit: int = 0, load_output: bool = True) -> dict:
        if not load_output and not retain_output:
            raise ValueError("file handoff requires retained output")
        task = asyncio.current_task()
        self._foreground[task] = turn_id
        result = None
        try:
            result = await self._request(
                "run", shell, cmd, cwd, tty,
                (1000 if tty else 300000) if wait_ms is None else wait_ms,
                0 if timeout_ms is None else timeout_ms, inline_limit, turn=turn_id,
            )
            if load_output:
                result = await self.materialize(result)
            if result["session_id"]:
                await self._request("acknowledge", result["session_id"], False)
            elif not retain_output:
                await self.release_output(result)
            return result
        except asyncio.CancelledError:
            # A cancelled handoff has not returned a session to the tool caller,
            # even if its native acknowledgement just won the scheduling race.
            if result is not None and result["session_id"]:
                sid = result["session_id"]
                await self._request("terminate", sid, cleanup=True)
                await self._discard_cancelled_session(sid)
            elif result is not None and result.get("output_id"):
                await self._request("release_output", result["output_id"], cleanup=True)
            raise
        finally:
            self._foreground.pop(task, None)

    async def _discard_cancelled_session(self, session_id: int) -> None:
        while True:
            result = await self._request("read", session_id, 5000, cleanup=True)
            if result["status"] in TERMINAL:
                break
        await self._request("release_output", session_id, cleanup=True)
        self._mark_consumed(session_id)

    async def materialize(self, event: dict) -> dict:
        """Read the exact file prefix observed by native code, outside the asyncio thread."""
        def load():
            result = dict(event)
            for stream in ("stdout", "stderr"):
                path = event.get(stream + "_path", "")
                size = event.get(stream + "_total", 0)
                if stream + "_bytes" in event:
                    raw = event[stream + "_bytes"]
                elif path:
                    with Path(path).open("rb") as file:
                        raw = file.read(size)
                    if len(raw) != size:
                        raise OSError("shell output file shorter than its snapshot")
                else:
                    raw = b""
                result[stream + "_bytes"] = raw
                result[stream] = raw.decode("utf-8", errors="replace")
            return result
        return await asyncio.to_thread(load)

    async def release_output(self, result: dict) -> None:
        await self._request("release_output", result["output_id"], cleanup=True)
        if result["session_id"]:
            self._mark_consumed(result["session_id"])

    async def read(self, session_id: int, *, wait_ms: int = 0) -> dict:
        result = await self.materialize(await self._request("read", session_id, wait_ms))
        if result["status"] in TERMINAL:
            await self._request("acknowledge", session_id, True)
            self._mark_consumed(session_id)
        return result

    async def write(self, session_id: int, text: str) -> dict:
        return await self.materialize(await self._request("write", session_id, text))

    async def resize(self, session_id: int, rows: int, columns: int) -> dict:
        return await self.materialize(await self._request("resize", session_id, rows, columns))

    async def terminate(self, session_id: int) -> dict:
        return await self.materialize(await self._request("terminate", session_id))

    async def release(self, session_id: int) -> None:
        await self._request("release", session_id)
        self._owners.pop(session_id, None)
        self._mark_consumed(session_id)

    def _mark_consumed(self, session_id: int) -> None:
        self._consumed.add(session_id)
        self.completion_budgets.pop(session_id, None)
        self._completions.pop(session_id, None)

    def drain_completions(self) -> list[Completion]:
        events = list(self._completions.values())
        self._completions.clear()
        return events

    async def acknowledge_completion(self, event: Completion) -> None:
        await self.release_output(event.result)

    async def interrupt_turn(self, turn_id: str) -> None:
        tasks = [task for task, owner in self._foreground.items() if owner == turn_id]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @asynccontextmanager
    async def tool_admission(self, effect_kind: str):
        """Use the resolved registry record, including indirect-call and role projections."""
        if effect_kind in READ_EFFECTS:
            yield
            return
        lease = (await self._request("acquire_write"))["session_id"]
        try:
            yield
        finally:
            await asyncio.shield(self._request("release_write", lease, cleanup=True))

    async def close(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close_runtime())
        await asyncio.shield(self._closing)
        # call_soon_threadsafe deliveries were queued before native.close joined its workers.
        await asyncio.sleep(0)
        self._owners.clear()
        self.completion_budgets.clear()
        self.drain_completions()

    async def _close_runtime(self) -> None:
        # Finish cancelled foreground handoffs before destroying their output files.
        tasks = list(self._foreground)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._closed = True
        await asyncio.to_thread(self.native.close)

    async def reset(self) -> ShellRuntime:
        await self.close()
        return type(self)(on_ready=self._on_ready)
