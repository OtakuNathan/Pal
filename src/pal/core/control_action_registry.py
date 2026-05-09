from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pal.control.contracts import ControlAction


ControlActionHandler = Callable[[ControlAction], Any | Awaitable[Any]]


@dataclass(frozen=True)
class ControlActionHandlerResult:
    handled: bool
    message: str = ""
    structured: dict[str, Any] = field(default_factory=dict)


@dataclass
class ControlActionHandlerRegistry:
    handlers: dict[str, tuple[str, ControlActionHandler]] = field(default_factory=dict)
    by_module: dict[str, list[str]] = field(default_factory=dict)

    def register(self, module_id: str, action_kind: str, handler: ControlActionHandler) -> None:
        normalized_module = str(module_id or "").strip()
        normalized_kind = str(action_kind or "").strip()
        if not normalized_module or not normalized_kind:
            raise ValueError("module_id and action_kind are required")
        existing = self.handlers.get(normalized_kind)
        if existing is not None and existing[0] != normalized_module:
            raise ValueError(f"control action handler already registered: {normalized_kind}")
        self.handlers[normalized_kind] = (normalized_module, handler)
        bucket = self.by_module.setdefault(normalized_module, [])
        if normalized_kind not in bucket:
            bucket.append(normalized_kind)

    def unregister_module(self, module_id: str) -> list[str]:
        normalized_module = str(module_id or "").strip()
        names = list(self.by_module.pop(normalized_module, []))
        for name in names:
            existing = self.handlers.get(name)
            if existing is not None and existing[0] == normalized_module:
                self.handlers.pop(name, None)
        return names

    async def handle(self, action: ControlAction) -> ControlActionHandlerResult:
        existing = self.handlers.get(str(action.action_kind or "").strip())
        if existing is None:
            return ControlActionHandlerResult(handled=False)
        _module_id, handler = existing
        result = handler(action)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ControlActionHandlerResult):
            return result
        if result is None:
            return ControlActionHandlerResult(handled=True)
        if isinstance(result, dict):
            return ControlActionHandlerResult(
                handled=True,
                message=str(result.get("message") or result.get("text") or ""),
                structured=dict(result),
            )
        return ControlActionHandlerResult(handled=True, message=str(result))
