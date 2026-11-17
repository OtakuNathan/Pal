"""Pending controls own independent expiry events, never a main-loop scan."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pal.core.contracts import ControlScopeState, PendingControlRequest
from pal.shared.timers import TimerHandlePort

if TYPE_CHECKING:
    from pal.core.runtime import PalCore


class ControlRequests:
    def __init__(self, core: PalCore) -> None:
        self._core = core
        self._timers = core.main_loop.timers.open_scope()
        self._expirations: dict[tuple[str, str], TimerHandlePort] = {}

    def get(self, scope: str, kind: str) -> PendingControlRequest | None:
        state = self._core.state.control_scopes.get(scope)
        return state.pending_requests.get(kind) if state is not None else None

    def put(self, request: PendingControlRequest) -> None:
        self.remove(request.control_scope_key, request.request_kind)
        state = self._core.state.control_scopes.setdefault(request.control_scope_key, ControlScopeState())
        state.pending_requests[request.request_kind] = request
        if not request.executing:
            key = (request.control_scope_key, request.request_kind)

            async def expire() -> None:
                await self.expire(request)

            self._expirations[key] = self._timers.schedule(
                (request.expires_at - datetime.now(timezone.utc)).total_seconds(), expire)

    def remove(self, scope: str, kind: str) -> PendingControlRequest | None:
        timer = self._expirations.pop((scope, kind), None)
        if timer is not None:
            timer.cancel()
        state = self._core.state.control_scopes.get(scope)
        return state.pending_requests.pop(kind, None) if state is not None else None

    def is_current(self, request: PendingControlRequest) -> bool:
        return self.get(request.control_scope_key, request.request_kind) is request

    def set_executing(self, request: PendingControlRequest, executing: bool) -> None:
        if self.is_current(request):
            request.executing = executing
            self.put(request)

    async def expire(self, request: PendingControlRequest) -> None:
        if self.is_current(request) and not request.executing:
            self.remove(request.control_scope_key, request.request_kind)
            if request.expired_delivery is not None:
                await self._core.control_runtime.deliver(request.expired_delivery)

    def invalidate_context(self) -> None:
        for scope in self._core.state.control_scopes.values():
            for request in tuple(scope.pending_requests.values()):
                if request.context_sensitive:
                    self.remove(request.control_scope_key, request.request_kind)

    def close(self) -> None:
        self._timers.close()
        self._expirations.clear()
