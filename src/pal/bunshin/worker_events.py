"""Bounded worker IPC, with one writer owning complete JSON-line frames."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, TextIO

from pal.bunshin.failure_diagnostics import exception_diagnostic
from pal.foundation.diagnostics import exception_report


EventWriter = Callable[[dict[str, Any]], Awaitable[None]]
LineWriter = Callable[[bytes], Awaitable[None]]
_CONFIRMED_EVENTS = {"terminal", "approval_requested", "clarification_requested"}


def _json_line(value: dict[str, Any]) -> bytes:
    # Match the Manager's replacement of undecodable filename bytes, so the
    # recovered error is also valid UTF-8 for SQLite and failure artifacts.
    text = json.dumps(value, ensure_ascii=False) + "\n"
    return re.sub(r"[\ud800-\udfff]", "\ufffd", text).encode("utf-8")


class WorkerEventDeliveryError(RuntimeError):
    """A required worker message could not reach the Manager pipe."""


class JsonLinePipe:
    """Write a pipe without blocking the role's event loop or executor shutdown."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.fd: int | None = None
        self._failed = False
        self._closed = False
        # In-memory streams also let embedded entrypoints capture wire output.
        if isinstance(stream, io.StringIO) or (
            isinstance(stream, io.TextIOWrapper) and isinstance(stream.buffer, io.BytesIO)
        ):
            return
        self.fd = os.dup(stream.fileno())
        try:
            self._blocking = os.get_blocking(self.fd)
            os.set_blocking(self.fd, False)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    async def write(self, line: bytes) -> None:
        if self._closed:
            raise WorkerEventDeliveryError("worker event pipe is closed")
        if self._failed:
            raise WorkerEventDeliveryError("worker event pipe has an interrupted frame")
        if self.fd is None:
            self.stream.write(line.decode("utf-8"))
            self.stream.flush()
            return
        remaining = memoryview(line)
        try:
            while remaining:
                try:
                    count = os.write(self.fd, remaining)
                except InterruptedError:
                    continue
                except BlockingIOError:
                    await self._wait_writable()
                    continue
                if count <= 0:
                    raise WorkerEventDeliveryError("worker event pipe made no write progress")
                remaining = remaining[count:]
        except BaseException:
            # Never append another JSON frame after a possibly partial write.
            self._failed = True
            raise

    async def _wait_writable(self) -> None:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        fd = self.fd
        assert fd is not None

        def writable() -> None:
            if not ready.done():
                ready.set_result(None)

        loop.add_writer(fd, writable)
        try:
            await ready
        finally:
            loop.remove_writer(fd)

    def close(self) -> None:
        self._closed = True
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                # dup shares the original stdout's file status flags.
                os.set_blocking(fd, self._blocking)
            finally:
                os.close(fd)


@dataclass
class _Message:
    line: bytes
    delivered: asyncio.Future[None] | None


def _abandon_confirmation(delivered: asyncio.Future[None]) -> None:
    if not delivered.cancel() and not delivered.cancelled():
        delivered.exception()


class WorkerEventWriter:
    def __init__(
        self, write_line: LineWriter, *, capacity: int = 256,
        write_timeout_seconds: float = 30.0, close_timeout_seconds: float = 5.0,
        fallback: LineWriter | None = None,
    ) -> None:
        if capacity <= 0 or write_timeout_seconds <= 0 or close_timeout_seconds <= 0:
            raise ValueError("worker event queue capacity and timeouts must be positive")
        self._write_line = write_line
        self._fallback = fallback
        self._queue: asyncio.Queue[_Message | None] = asyncio.Queue(maxsize=capacity)
        self._enqueue_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._write_timeout = write_timeout_seconds
        self._close_timeout = close_timeout_seconds
        self._reported_failure = False

    async def __aenter__(self) -> WorkerEventWriter:
        if self._task is not None or self._closing:
            raise RuntimeError("worker event writer cannot be restarted")
        self._task = asyncio.create_task(self._send(), name="bunshin-worker-events")
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await self.close()
        except Exception as shutdown_error:
            if exc is None:
                raise
            exc.add_note("Worker event shutdown also failed:\n" + exception_report(shutdown_error))
            logging.getLogger(__name__).warning("Worker event shutdown failed after the primary failure")

    async def write_event(self, event: dict[str, Any]) -> None:
        await self._enqueue(
            {"kind": "event", "event": event},
            confirmed=str(event.get("event_kind") or "") in _CONFIRMED_EVENTS,
        )

    async def write_error(self, exc: Exception) -> None:
        await self._enqueue(_worker_error(exc), confirmed=True)

    async def _enqueue(self, value: dict[str, Any], *, confirmed: bool) -> None:
        line = _json_line(value)
        async with self._enqueue_lock:
            if self._closing or self._stopping.is_set():
                if confirmed:
                    raise WorkerEventDeliveryError("worker event writer is closed")
                return
            if self._task is None:
                raise RuntimeError("worker event writer has not started")
            delivered: asyncio.Future[None] | None = (
                asyncio.get_running_loop().create_future() if confirmed else None
            )
            try:
                accepted = await self._put(_Message(line, delivered))
            except BaseException:
                if delivered is not None:
                    _abandon_confirmation(delivered)
                raise
            if not accepted:
                if delivered is not None:
                    _abandon_confirmation(delivered)
                    raise WorkerEventDeliveryError("worker event writer is closing")
                return
        if delivered is not None:
            await delivered

    async def _put(self, message: _Message) -> bool:
        try:
            self._queue.put_nowait(message)
            return True
        except asyncio.QueueFull:
            pass
        put = asyncio.create_task(self._queue.put(message))
        stopping = asyncio.create_task(self._stopping.wait())
        try:
            done, _ = await asyncio.wait({put, stopping}, return_when=asyncio.FIRST_COMPLETED)
            return put in done and not self._stopping.is_set()
        finally:
            for task in (put, stopping):
                if not task.done():
                    task.cancel()
            await asyncio.gather(put, stopping, return_exceptions=True)

    async def _send(self) -> None:
        try:
            await self._send_messages()
        finally:
            self._stopping.set()
            self._fail_pending()

    async def _send_messages(self) -> None:
        while True:
            message = await self._queue.get()
            if message is None:
                return
            try:
                async with asyncio.timeout(self._write_timeout):
                    await self._write_line(message.line)
            except Exception as exc:
                if message.delivered is not None and not message.delivered.done():
                    failure = WorkerEventDeliveryError(
                        f"worker event delivery failed: {type(exc).__name__}: {exc}",
                    )
                    failure.__cause__ = exc
                    message.delivered.set_exception(failure)
                if not self._reported_failure:
                    self._reported_failure = True
                    logging.getLogger(__name__).warning(
                        "Worker event delivery failed: %s", type(exc).__name__,
                    )
                if message.delivered is not None and self._fallback is not None:
                    fallback = json.loads(message.line)
                    fallback["delivery_error"] = exception_report(exc)
                    await self._fallback(_json_line(fallback))
            except asyncio.CancelledError:
                if message.delivered is not None and not message.delivered.done():
                    message.delivered.set_exception(WorkerEventDeliveryError("worker event writer stopped before delivery"))
                raise
            else:
                if message.delivered is not None and not message.delivered.done():
                    message.delivered.set_result(None)

    async def close(self) -> None:
        async with self._close_lock:
            await self._close()

    async def _close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._stopping.set()
        task = self._task
        if task is None:
            return
        try:
            async with asyncio.timeout(self._close_timeout):
                async with self._enqueue_lock:
                    await self._queue.put(None)
                await asyncio.shield(task)
        except TimeoutError as exc:
            raise WorkerEventDeliveryError("worker event queue did not finish flushing") from exc
        except asyncio.CancelledError:
            if task.cancelled():
                raise WorkerEventDeliveryError("worker event sender was cancelled") from None
            raise
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._fail_pending()

    def _fail_pending(self) -> None:
        while not self._queue.empty():
            message = self._queue.get_nowait()
            if message is not None and message.delivered is not None and not message.delivered.done():
                message.delivered.set_exception(WorkerEventDeliveryError("worker event writer stopped before delivery"))


def _worker_error(exc: Exception) -> dict[str, Any]:
    return {
        "kind": "worker_error", "error": exception_report(exc),
        "failure_diagnostic": exception_diagnostic(exc),
    }


async def run_worker_with_events(operation: Callable[[EventWriter], Awaitable[int]]) -> int:
    pipe: JsonLinePipe | None = None
    primary_error: Exception | None = None
    try:
        pipe = JsonLinePipe(sys.stdout)
        async with WorkerEventWriter(pipe.write, fallback=_report_frame_to_stderr) as events:
            try:
                return await operation(events.write_event)
            except Exception as exc:
                primary_error = exc
                try:
                    await events.write_error(exc)
                except Exception:
                    # The sender owns the fallback for this original message.
                    # A shutdown failure below also retains primary_error.
                    pass
                return 1
    except Exception as exc:
        if primary_error is not None and exc is not primary_error:
            exc.add_note("Original worker error:\n" + exception_report(primary_error))
        await _report_error_to_stderr(exc)
        return 1
    finally:
        if pipe is not None:
            pipe.close()


async def _report_error_to_stderr(exc: Exception) -> None:
    await _report_frame_to_stderr(_json_line(_worker_error(exc)))


async def _report_frame_to_stderr(line: bytes) -> None:
    pipe: JsonLinePipe | None = None
    try:
        pipe = JsonLinePipe(sys.stderr)
        async with asyncio.timeout(5.0):
            # stderr may contain a library's unterminated diagnostic line.
            await pipe.write(b"\n" + _json_line({"kind": "worker_event_fallback", "message": json.loads(line)}))
    except (OSError, ValueError, WorkerEventDeliveryError, TimeoutError):
        # Both IPC channels can disappear when the Manager exits. The process
        # still returns failure; never replace the original error with a second
        # failure while trying to report it.
        pass
    finally:
        if pipe is not None:
            pipe.close()
