"""Retain a timed-out lifecycle awaitable instead of invoking it twice."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, wait as wait_futures
from dataclasses import dataclass
import inspect
from typing import Any, Callable


async def _wait(value):
    return await asyncio.shield(value)


@dataclass
class OwnedLifecycleStep:
    callback: Callable[[], Any]
    pending: asyncio.Future | Future | None = None
    completed: bool = False
    value: Any = None

    def _success(self, value):
        self.completed, self.value = True, value
        return value

    def run(self, loop=None, *, timeout: float = 10.0):
        if self.completed:
            return self.value
        if self.pending is None:
            value = self.callback()
            if not inspect.isawaitable(value):
                return self._success(value)
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if asyncio.isfuture(value):
                # A synchronous hook may already have scheduled cleanup. Keep
                # that work even when its owner loop cannot be blocked here.
                self.pending = value
            elif loop is not None and loop.is_running() and current is not loop:
                self.pending = asyncio.run_coroutine_threadsafe(_wait(value), loop)
            elif current is not None:
                if inspect.iscoroutine(value):
                    value.close()
                raise RuntimeError("async provider lifecycle cannot block its owner event loop")
            else:
                return self._success(asyncio.run(_wait(value)))
        pending = self.pending
        consumed = False
        try:
            if isinstance(pending, Future):
                done, _ = wait_futures({pending}, timeout=timeout)
                if not done:
                    raise TimeoutError("provider lifecycle remains pending")
                # A callback can itself raise TimeoutError. Consume that
                # settled failure, distinct from timing out while waiting.
                consumed = True
                return self._success(pending.result())
            if pending.done():
                consumed = True
                return self._success(pending.result())
            owner = pending.get_loop()
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if not owner.is_running() or current is owner:
                raise RuntimeError("provider cleanup is pending on its owner event loop")
            bridge = asyncio.run_coroutine_threadsafe(_wait(pending), owner)
            done, _ = wait_futures({bridge}, timeout=timeout)
            if not done:
                raise TimeoutError("provider lifecycle remains pending")
            # Consume a settled callback failure as well as success; otherwise
            # a failed cross-loop step would be stuck on the same failed task.
            consumed = pending.done()
            bridge.result()
            consumed = True
            return self._success(pending.result())
        finally:
            if consumed:
                self.pending = None

    async def run_async(self, *, timeout: float = 5.0):
        if self.completed:
            return self.value
        if self.pending is None:
            value = self.callback()
            if not inspect.isawaitable(value):
                return self._success(value)
            self.pending = asyncio.ensure_future(value)
        pending = self.pending
        if isinstance(pending, Future):
            waiter = asyncio.wrap_future(pending)
        elif pending.get_loop() is asyncio.get_running_loop() or pending.done():
            waiter = pending
        else:
            if not pending.get_loop().is_running():
                raise RuntimeError("provider cleanup is pending on a stopped event loop")
            waiter = asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_wait(pending), pending.get_loop()))
        done, _ = await asyncio.wait({waiter}, timeout=timeout)
        if not done:
            raise TimeoutError(f"provider shutdown exceeded {timeout:g}s; cleanup remains owned")
        try:
            return self._success(waiter.result())
        finally:
            self.pending = None
