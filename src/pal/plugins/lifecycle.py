from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pal.core.module_registry import ModuleHandle


Cleanup = Callable[[], Any]


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
            # The blocking worker cannot be cancelled.  Let it acquire, then
            # balance the admission before propagating cancellation.
            await asyncio.shield(acquired)
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
            await asyncio.shield(acquired)
            self._release_write()
            raise
        try:
            yield
        finally:
            self._release_write()

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


class _StagedRegistry:
    def __init__(self, real: Any, scope: "PluginScope", register_names: set[str]) -> None:
        self._real = real
        self._scope = scope
        self._register_names = register_names

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._real, name)
        if name not in self._register_names or not callable(value):
            return value

        def staged(*args: Any, **kwargs: Any) -> Any:
            if self._scope.published:
                return value(*args, **kwargs)
            return None

        return staged


class _StagedModuleRegistry:
    def __init__(self, real: Any, scope: "PluginScope") -> None:
        self._real = real
        self._scope = scope

    def get(self, module_id: str) -> ModuleHandle | None:
        current = self._real.get(module_id)
        if current is not None:
            return current
        handle = self._scope.handle
        return handle if handle is not None and handle.module_id == module_id else None

    def require(self, module_id: str) -> ModuleHandle:
        handle = self.get(module_id)
        if handle is None:
            raise KeyError(f"unknown module: {module_id}")
        return handle


class _StagedL3Registry:
    def __init__(self, real: Any, scope: "PluginScope") -> None:
        self._real = real
        self._scope = scope

    def register(self, provider: Any) -> None:
        if self._scope.published:
            self._real.register(provider)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _StagedExecutionRuntime:
    def __init__(self, real: Any, scope: "PluginScope") -> None:
        self._real = real
        self._scope = scope
        self.l3_plugin_registry = _StagedL3Registry(real.l3_plugin_registry, scope)

    def register_provider_ref(self, provider_id: str, provider: Any) -> None:
        if self._scope.published:
            self._real.register_provider_ref(provider_id, provider)

    def unregister_provider_ref(self, provider_id: str) -> None:
        if self._scope.published:
            self._real.unregister_provider_ref(provider_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class StagedMainContext:
    """Context facade that keeps a candidate generation private until commit."""

    def __init__(self, real: Any, scope: "PluginScope") -> None:
        self._real = real
        self._scope = scope
        self.module_registry = _StagedModuleRegistry(real.module_registry, scope)
        self.execution_runtime = _StagedExecutionRuntime(real.execution_runtime, scope)
        self.event_source_registry = _StagedRegistry(real.event_source_registry, scope, {"attach"})
        self.event_handler_registry = _StagedRegistry(real.event_handler_registry, scope, {"register"})
        self.prompt_fragment_registry = _StagedRegistry(real.prompt_fragment_registry, scope, {"register"})
        self.control_action_registry = _StagedRegistry(real.control_action_registry, scope, {"register"})

    def register_module(self, handle: ModuleHandle) -> None:
        if self._scope.handle is not None and self._scope.handle is not handle:
            raise ValueError("a plugin generation may publish exactly one module handle")
        self._scope.handle = handle

    def unregister_module(self, handle: ModuleHandle) -> bool:
        if not self._scope.published and self._scope.handle is handle:
            self._scope.handle = None
            return True
        return self._real.unregister_module(handle)

    def require_port(self, key: str) -> Any:
        return self._real.require_port(key)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@dataclass
class PluginScope:
    core_context: Any
    plugin_id: str
    cleanups: list[Cleanup] = field(default_factory=list)
    handle: ModuleHandle | None = None
    published: bool = False

    def __post_init__(self) -> None:
        self.context = StagedMainContext(self.core_context, self)

    def defer(self, cleanup: Cleanup) -> Cleanup:
        self.cleanups.append(cleanup)
        return cleanup

    def track_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        def cancel() -> None:
            if not task.done():
                task.cancel()

        self.defer(cancel)
        return task

    def absorb_handle_cleanups(self, handle: ModuleHandle) -> None:
        for callback in handle.cleanup_callbacks:
            self.defer(callback)
        handle.cleanup_callbacks.clear()
        if callable(handle.shutdown_async):
            self.defer(handle.shutdown_async)
            handle.shutdown_async = None
            handle.shutdown_sync = None
        elif callable(handle.shutdown_sync):
            self.defer(handle.shutdown_sync)
            handle.shutdown_sync = None

    def close(self) -> list[str]:
        errors: list[str] = []
        retry: list[Cleanup] = []
        for cleanup in reversed(self.cleanups):
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    _run_awaitable(result)
            except Exception as exc:  # cleanup is best-effort but fully reported
                errors.append(f"{exc.__class__.__name__}: {exc}")
                retry.append(cleanup)
        self.cleanups[:] = reversed(retry)
        return errors


def _run_awaitable(value: Awaitable[Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    result: list[Any] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(value))
        except BaseException as exc:  # pragma: no cover - defensive thread bridge
            error.append(exc)

    thread = threading.Thread(target=runner, name="pal-plugin-await", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0] if result else None


@dataclass
class PluginGeneration:
    number: int
    instance: Any
    scope: PluginScope
    handle: ModuleHandle
    cleanup_errors: tuple[str, ...] = ()
