"""Own live assignment tasks and readiness independently of business recovery."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pal.bunshin.v2.contracts import DeferredEffectError

Effect = Mapping[str, Any]
EffectHandler = Callable[[Effect], Awaitable[Effect]]
AssignmentLoop = Callable[[Effect, EffectHandler], Awaitable[Effect]]


@dataclass
class BackgroundAssignments:
    _tasks: dict[str, asyncio.Task[Effect]] = field(default_factory=dict, init=False)
    _ready: dict[str, asyncio.Event] = field(default_factory=dict, init=False)
    _assignments: dict[str, str] = field(default_factory=dict, init=False)
    _stopping: bool = field(default=False, init=False)

    @property
    def stopping(self) -> bool:
        return self._stopping

    @property
    def active_count(self) -> int:
        return sum(not task.done() for task in self._tasks.values())

    def request_stop(self) -> None:
        self._stopping = True

    def tasks(self) -> tuple[asyncio.Task[Effect], ...]:
        return tuple(self._tasks.values())

    def task(self, effect_key: str) -> asyncio.Task[Effect] | None:
        return self._tasks.get(effect_key)

    def assignment_id(self, effect_key: str, default: str = '') -> str:
        return self._assignments.get(effect_key, default)

    def bind(self, effect_key: str, assignment_id: str) -> None:
        self._assignments[effect_key] = assignment_id

    def signal_ready(self, effect: Effect, assignment_id: str) -> None:
        key = str(effect.get('effect_key') or effect.get('effect_id') or '')
        if not key:
            return
        self.bind(key, assignment_id)
        event = self._ready.get(key)
        if event is not None:
            event.set()

    def track(self, effect_key: str, task: asyncio.Task[Effect]) -> None:
        self._tasks[effect_key] = task
        task.add_done_callback(lambda done: self._done(effect_key, done))

    def recover(self, effect_key: str, assignment_id: str, task: asyncio.Task[Effect]) -> None:
        self.bind(effect_key, assignment_id)
        ready = asyncio.Event()
        ready.set()
        self._ready[effect_key] = ready
        self.track(effect_key, task)

    def _done(self, effect_key: str, task: asyncio.Task[Effect]) -> None:
        if not task.cancelled():
            task.exception()
        if self._tasks.get(effect_key) is task:
            self._tasks.pop(effect_key, None)
            self._ready.pop(effect_key, None)

    async def drain(self, *, timeout_seconds: float) -> tuple[str, ...]:
        self.request_stop()
        tracked = tuple(self._tasks.items())
        if tracked:
            tasks = tuple(task for _, task in tracked)
            _, pending = await asyncio.wait(tasks, timeout=max(0.0, float(timeout_seconds)))
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return tuple(key for key, _ in tracked)

    async def launch(
        self,
        effect: Mapping[str, Any],
        runner: EffectHandler,
        run_loop: AssignmentLoop,
    ) -> Mapping[str, Any]:
        if self.stopping:
            raise DeferredEffectError("worker supervisor is stopping")
        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "").strip()
        if not effect_key:
            raise ValueError("background worker effect requires an effect key")
        existing = self._tasks.get(effect_key)
        if existing is not None and not existing.done():
            return {
                "provider_request_id": self._assignments.get(effect_key, effect_key),
                "status": "already_running",
            }
        ready = asyncio.Event()
        self._ready[effect_key] = ready
        task = asyncio.create_task(
            run_loop(effect, runner),
            name=f"bunshin-v2-assignment-{hashlib.sha256(effect_key.encode()).hexdigest()[:12]}",
        )
        self._tasks[effect_key] = task
        task.add_done_callback(
            lambda completed, key=effect_key: self._done(key, completed)
        )
        ready_wait = asyncio.create_task(ready.wait())
        try:
            done, _pending = await asyncio.wait(
                {task, ready_wait},
                timeout=120.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # The background assignment remains owned by this registry when
            # its caller is cancelled; the caller's temporary waiter does not.
            ready_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ready_wait
        if task in done:
            return dict(task.result())
        if ready.is_set():
            return {
                "provider_request_id": self._assignments.get(effect_key, effect_key),
                "status": "assignment_started",
            }
        raise RuntimeError("role assignment was not durably created within 120 seconds")

