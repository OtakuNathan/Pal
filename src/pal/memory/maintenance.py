"""Resident owner for human CLI memory backend maintenance."""
from __future__ import annotations

import asyncio

from pal.shared import IntrospectionCall, RuntimeStatus
from pal.control.runtime_contracts import CORE_CONTROL
from pal.memory.swap import MemorySwapManager
from pal.foundation.workers import run_worker


class MemoryBackendOwner:
    def __init__(self, context, service):
        self.context = context
        self.host = context.port_registry.observe(CORE_CONTROL)
        self.service = service
        self._lock = asyncio.Lock()
        self.swaps = MemorySwapManager(service, self.host)
        self.timers = None
        self._recovery_timer = None
        service.on_generation_work = self._schedule_recovery

    def _schedule_recovery(self):
        if self.swaps.closing:
            return
        if self.timers is None:
            host = self.context.port_registry.optional(CORE_CONTROL)
            if host is None:
                return
            owner = self.context.module_registry.require("memory")
            self.timers = host.open_timer_scope()
            self.timers.bind_owner(lambda: self.context.module_registry.is_current(owner))
        if self._recovery_timer is not None:
            self._recovery_timer.cancel()
        self._recovery_timer = self.timers.schedule(0, self.swaps.recover_pending)

    async def close(self):
        if self.timers is not None:
            self.timers.close()
        self.service.on_generation_work = None
        await self.swaps.close()

    @property
    def runtime(self):
        return self.context.execution_runtime

    def _provider(self, name):
        provider = self.service.providers.get(name)
        if provider is None:
            raise ValueError(f"Unknown memory provider: {name}")
        return provider

    async def dispatch(self, command: str, params: dict) -> dict:
        allowed = {
            "providers": set(), "inspect": {"name"}, "attach": {"name"},
            "detach": {"name"}, "swap": {"name"},
            "swap-status": {"job_id"}, "swap-resume": {"job_id"}, "swap-cancel": {"job_id"},
            "refresh-indexes": {"name", "limit", "retry_failed"},
        }
        if command not in allowed or set(params) - allowed[command]:
            raise ValueError("Unknown memory operation or unexpected arguments")
        if command in {"attach", "detach", "swap"} and not params.get("name"):
            raise ValueError("Provider name is required")
        if "name" in params and (not isinstance(params["name"], str) or not params["name"].strip()):
            raise ValueError("Provider name must be nonempty text")
        if command in {"swap-status", "swap-resume", "swap-cancel"} and (not isinstance(params.get("job_id"), str) or not params["job_id"].strip()):
            raise ValueError("Migration job_id is required")
        limit = params.get("limit", 32)
        retry_failed = params.get("retry_failed", False)
        if type(limit) is not int or not 1 <= limit <= 10000 or type(retry_failed) is not bool:
            raise ValueError("Invalid index refresh arguments")
        async with self._lock, self.runtime.lifecycle_gate.read_async():
            if command == "swap-status":
                return self.swaps.status(params["job_id"])
            if command == "swap-resume":
                return self.swaps.resume(params["job_id"])
            if command == "swap-cancel":
                return await self.swaps.cancel(params["job_id"])
            if command == "swap":
                return self.swaps.start(params["name"])
            if command == "providers":
                return self._result({"active_provider_id": self.service.l3_selector.active_provider_id,
                    "items": [{"provider_id": name, "mounted": provider.mounted}
                              for name, provider in sorted(self.service.providers.plugins.items())]})
            name = params.get("name") or self.service.l3_selector.active_provider_id
            provider = self._provider(name)
            if command == "inspect":
                state = await self._thread(provider.inspect)
                return self._result({"provider_id": name, **state})
            if command == "refresh-indexes":
                if not provider.mounted:
                    raise RuntimeError("Provider is detached")
                state = await self._thread(lambda: provider.refresh_indexes(limit=limit, retry_failed=retry_failed))
                failed = bool(state.get("last_embedding_error") or state.get("read_only")
                              or state.get("detail") == "embedding-provider-unavailable")
                return self._result({"provider_id": name, **state}, failed=failed)
            # Fast lifecycle controls run on the resident loop under the ingress
            # transition lock; no new conversation can enter between check/apply.
            def apply():
                if command == "detach" and self.service.l3_selector.active_provider_id == name:
                    raise RuntimeError("Complete a managed memory swap before detaching the active backend")
                call = IntrospectionCall(name="memory_cli", args={})
                result = self.host.current.idle_module_operation(f"l3.{name}", lambda:
                    provider.attach(call) if command == "attach" else provider.detach(call))
                return self._result({"provider_id": name, **dict(result.structured or {})},
                                    failed=result.status != RuntimeStatus.OK)
            return await self.host.current.idle_operation(apply)

    def configuration_handlers(self):
        def handler(command):
            async def execute(params):
                return await self.dispatch(command, params)
            return execute
        return {f"memory.{command}": handler(command) for command in (
            "providers", "inspect", "attach", "detach", "swap", "swap-status", "swap-resume", "swap-cancel", "refresh-indexes")}

    @staticmethod
    async def _thread(function):
        # Cancellation cannot release the plugin generation lease while the
        # worker thread still owns its database/provider objects.
        return await run_worker(function)

    @staticmethod
    def _result(state, *, failed=False):
        return {"status": "error" if failed else "ok", "activation": "live", "state": state}
