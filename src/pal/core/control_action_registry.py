"""Module-owned control actions; the dispatcher has no command-name branches."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from contextlib import nullcontext
from typing import Any

from pal.control.contracts import ControlAction
from pal.execution.tool_facade import LifecycleAccess
from pal.foundation.lifecycle_gate import WriterPreferredRWGate

ControlActionHandler = Callable[[ControlAction], Awaitable[None]]
ControlObserver = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class ControlActionSpec:
    handler: ControlActionHandler
    allow_during_maintenance: bool = False
    lifecycle_access: LifecycleAccess = LifecycleAccess.SHARED


@dataclass
class ControlActionHandlerRegistry:
    lifecycle_gate: WriterPreferredRWGate
    handlers: dict[str, tuple[str, ControlActionSpec]] = field(default_factory=dict)
    status_providers: dict[str, Callable[[], str]] = field(default_factory=dict)
    observers: dict[str, dict[str, ControlObserver]] = field(default_factory=dict)

    def register(self, module_id: str, action_kind: str, spec: ControlActionSpec) -> None:
        module_id, action_kind = module_id.strip(), action_kind.strip()
        if not module_id or not action_kind:
            raise ValueError("module_id and action_kind are required")
        if not isinstance(spec, ControlActionSpec):
            raise TypeError("control actions require ControlActionSpec")
        existing = self.handlers.get(action_kind)
        if existing is not None and existing[0] != module_id:
            raise ValueError(f"control action handler already registered: {action_kind}")
        self.handlers[action_kind] = (module_id, spec)

    def resolve(self, action_kind: str) -> ControlActionSpec | None:
        entry = self.handlers.get(action_kind)
        return entry[1] if entry is not None else None

    def unregister_module(self, module_id: str, *, expected: Mapping[str, ControlActionSpec],
                          status_provider: Callable[[], str] | None = None,
                          observers: Mapping[str, ControlObserver] | None = None) -> None:
        for name, spec in expected.items():
            current = self.handlers.get(name)
            if current is not None and current[0] == module_id and current[1] is spec:
                self.handlers.pop(name)
        if self.status_providers.get(module_id) is status_provider:
            self.status_providers.pop(module_id, None)
        for topic, callback in (observers or {}).items():
            callbacks = self.observers.get(topic, {})
            if callbacks.get(module_id) is callback:
                callbacks.pop(module_id)

    async def notify(self, topic: str, payload: dict[str, Any]) -> dict[str, Exception]:
        errors = {}
        for owner, callback in tuple(self.observers.get(topic, {}).items()):
            try:
                async with self.lifecycle_gate.read_async():
                    if self.observers.get(topic, {}).get(owner) is callback:
                        await callback(dict(payload))
            except Exception as exc:
                errors[owner] = exc
        return errors

    async def handle(self, action: ControlAction) -> bool:
        entry = self.handlers.get(action.action_kind)
        if entry is None:
            return False
        spec = entry[1]
        access = spec.lifecycle_access
        gate = (nullcontext() if access is LifecycleAccess.OWNER_MANAGED
                else self.lifecycle_gate.write_async() if access is LifecycleAccess.EXCLUSIVE
                else self.lifecycle_gate.read_async())
        async with gate:
            if self.handlers.get(action.action_kind) is not entry:
                return False
            await spec.handler(action)
        return True
