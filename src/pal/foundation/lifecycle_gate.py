"""Shared synchronous/asynchronous lifecycle admission fence."""
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager

class WriterPreferredRWGate:
    """One process-wide lifecycle fence shared by sync and async tool calls."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextmanager
    def read(self):
        with self._condition:
            while self._writer or self._waiting_writers:
                self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @contextmanager
    def write(self):
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._condition.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            with self._condition:
                self._writer = False
                self._condition.notify_all()

    @asynccontextmanager
    async def read_async(self):
        acquired = asyncio.create_task(asyncio.to_thread(self._acquire_read))
        try:
            await asyncio.shield(acquired)
        except asyncio.CancelledError:
            await self._drain_acquisition(acquired)
            self._release_read()
            raise
        try:
            yield
        finally:
            self._release_read()

    @asynccontextmanager
    async def write_async(self):
        acquired = asyncio.create_task(asyncio.to_thread(self._acquire_write))
        try:
            await asyncio.shield(acquired)
        except asyncio.CancelledError:
            await self._drain_acquisition(acquired)
            self._release_write()
            raise
        try:
            yield
        finally:
            self._release_write()

    @staticmethod
    async def _drain_acquisition(acquired: asyncio.Task[None]) -> None:
        # The thread owns a pending lease, even when its waiter is cancelled.
        # Repeated cancellation cannot abandon it: wait for acquisition before
        # releasing exactly once and propagating the original cancellation.
        while not acquired.done():
            try:
                await asyncio.shield(acquired)
            except asyncio.CancelledError:
                continue
        acquired.result()

    def _acquire_read(self) -> None:
        with self._condition:
            while self._writer or self._waiting_writers:
                self._condition.wait()
            self._readers += 1

    def _release_read(self) -> None:
        with self._condition:
            self._readers -= 1
            if self._readers == 0:
                self._condition.notify_all()

    def _acquire_write(self) -> None:
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._condition.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1

    def _release_write(self) -> None:
        with self._condition:
            self._writer = False
            self._condition.notify_all()
