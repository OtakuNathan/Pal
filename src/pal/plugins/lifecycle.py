from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Generic, TypeVar, overload

from pal.shared.ports import PortKey

if TYPE_CHECKING:
    from pal.core.main_context import MainContext
    from pal.core.event_source_registry import EventSourceRegistry
    from pal.core.event_handler_registry import EventHandlerRegistry
    from pal.core.prompt_fragment_registry import PromptFragmentRegistry
    from pal.core.control_action_registry import ControlActionHandlerRegistry

T = TypeVar("T")
R = TypeVar("R")

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


class _StagedRegistry(Generic[R]):
    def __init__(self, real: R, scope: "PluginScope") -> None:
        self._real = real
        self._scope = scope


class _StagedEventSources(_StagedRegistry["EventSourceRegistry"]):
    def attach(self, module_id, source) -> None:
        if self._scope.published:
            self._real.attach(module_id, source)

    def iter_sources(self):
        return self._real.iter_sources()

    def detach_module(self, module_id):
        return self._real.detach_module(module_id)

    @property
    def sources(self):
        return self._real.sources


class _StagedEventHandlers(_StagedRegistry["EventHandlerRegistry"]):
    def register(self, event_kind, handler, *, module_id=None) -> None:
        if self._scope.published:
            self._real.register(event_kind, handler, module_id=module_id)

    def matching(self, event_kind):
        return self._real.matching(event_kind)

    def detach_module(self, module_id):
        return self._real.detach_module(module_id)

    @property
    def handlers(self):
        return self._real.handlers


class _StagedPromptFragments(_StagedRegistry["PromptFragmentRegistry"]):
    def register(self, provider) -> None:
        if self._scope.published:
            self._real.register(provider)

    def unregister(self, provider_id) -> None:
        self._real.unregister(provider_id)

    def unregister_module(self, module_id):
        return self._real.unregister_module(module_id)

    def list_for_prompt(self):
        return self._real.list_for_prompt()

    @property
    def providers(self):
        return self._real.providers


class _StagedControlActions(_StagedRegistry["ControlActionHandlerRegistry"]):
    def register(self, module_id, action_kind, handler) -> None:
        if self._scope.published:
            self._real.register(module_id, action_kind, handler)

    def unregister_module(self, module_id):
        return self._real.unregister_module(module_id)

    async def handle(self, action):
        return await self._real.handle(action)

    @property
    def handlers(self):
        return self._real.handlers


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

    def __init__(self, real: MainContext, scope: "PluginScope") -> None:
        self._real = real
        self._scope = scope
        self.module_registry = _StagedModuleRegistry(real.module_registry, scope)
        self.execution_runtime = _StagedExecutionRuntime(real.execution_runtime, scope)
        self.event_source_registry = _StagedEventSources(real.event_source_registry, scope)
        self.event_handler_registry = _StagedEventHandlers(real.event_handler_registry, scope)
        self.prompt_fragment_registry = _StagedPromptFragments(real.prompt_fragment_registry, scope)
        self.control_action_registry = _StagedControlActions(real.control_action_registry, scope)

    def register_module(self, handle: ModuleHandle) -> None:
        if self._scope.handle is not None and self._scope.handle is not handle:
            raise ValueError("a plugin generation may publish exactly one module handle")
        self._scope.handle = handle

    def unregister_module(self, handle: ModuleHandle) -> bool:
        if not self._scope.published and self._scope.handle is handle:
            self._scope.handle = None
            return True
        return self._real.unregister_module(handle)

    @overload
    def require_port(self, key: PortKey[T]) -> T: ...

    @overload
    def require_port(self, key: str) -> Any: ...

    def require_port(self, key: PortKey[T] | str) -> T | Any:
        return self._real.require_port(key)

    @property
    def port_registry(self):
        return self._real.port_registry

    @property
    def introspection_registry(self):
        return self._real.introspection_registry

    @property
    def lifecycle_owner_registry(self):
        return self._real.lifecycle_owner_registry

    @property
    def turn_event_bus(self):
        return self._real.turn_event_bus

    @property
    def core_event_bus(self):
        return self._real.core_event_bus

    @property
    def capability_registry(self):
        return self._real.capability_registry

@dataclass
class PluginScope:
    core_context: Any
    plugin_id: str
    cleanups: list[Cleanup] = field(default_factory=list)
    handle: ModuleHandle | None = None
    published: bool = False
    _core_subscriptions: list[Any] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.context = StagedMainContext(self.core_context, self)

    def subscribe_core_events(self, topics=None, *, max_pending=128):
        """Read system observations in a plugin-owned worker, never inline I/O."""
        from pal.core.core_events import ALL_CORE_TOPICS

        subscription = self.core_context.core_event_bus.open_subscription(
            ALL_CORE_TOPICS if topics is None else topics,
            max_pending=max_pending, active=self.published,
        )
        self._core_subscriptions.append(subscription)
        self.defer(subscription.close)
        return subscription

    def publish_core_subscriptions(self):
        for subscription in self._core_subscriptions:
            subscription.activate()

    def defer(self, cleanup: Cleanup) -> Cleanup:
        self.cleanups.append(cleanup)
        return cleanup

    def track_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        cancellation_requested = False

        def cancel() -> None:
            nonlocal cancellation_requested
            if task.done():
                return
            loop = task.get_loop()
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is loop:
                if not cancellation_requested:
                    cancellation_requested = True
                    task.cancel()
                # A synchronous lifecycle call cannot join its own event loop.
                # Keep this cleanup for retry instead of releasing live resources.
                raise RuntimeError("Plugin task cancellation is pending; retry detach after the task exits")
            if not loop.is_running():
                raise RuntimeError("Plugin task loop is stopped with unfinished work")
            finished = threading.Event()
            def request_cancel():
                nonlocal cancellation_requested
                task.add_done_callback(lambda _: finished.set())
                if not cancellation_requested:
                    cancellation_requested = True
                    task.cancel()
            loop.call_soon_threadsafe(request_cancel)
            if not finished.wait(timeout=10):
                raise RuntimeError("Plugin task has not completed cancellation; retry detach")

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
