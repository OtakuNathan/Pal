"""Resident owner for human CLI memory backend maintenance."""
from __future__ import annotations

import asyncio

from pal.shared import IntrospectionCall, RuntimeStatus


class MemoryBackendOwner:
    def __init__(self, core, service):
        self.core = core
        self.service = service
        self._lock = asyncio.Lock()

    @property
    def runtime(self):
        return self.core.context.execution_runtime

    def _provider(self, name):
        provider = self.runtime.l3_plugin_registry.get(name)
        if provider is None:
            raise ValueError(f"Unknown memory provider: {name}")
        return provider

    async def dispatch(self, command: str, params: dict) -> dict:
        allowed = {
            "providers": set(), "inspect": {"name"}, "attach": {"name"},
            "detach": {"name"}, "select": {"name"},
            "refresh-indexes": {"name", "limit", "retry_failed"},
        }
        if command not in allowed or set(params) - allowed[command]:
            raise ValueError("Unknown memory operation or unexpected arguments")
        if command in {"attach", "detach", "select"} and not params.get("name"):
            raise ValueError("Provider name is required")
        if "name" in params and (not isinstance(params["name"], str) or not params["name"].strip()):
            raise ValueError("Provider name must be nonempty text")
        limit = params.get("limit", 32)
        retry_failed = params.get("retry_failed", False)
        if type(limit) is not int or not 1 <= limit <= 10000 or type(retry_failed) is not bool:
            raise ValueError("Invalid index refresh arguments")
        async with self._lock, self.runtime.lifecycle_gate.read_async():
            if command == "providers":
                return self._result({"active_provider_id": self.service.l3_selector.active_provider_id,
                    "items": [{"provider_id": name, "mounted": bool(getattr(provider, "mounted", True))}
                              for name, provider in sorted(self.runtime.l3_plugin_registry.plugins.items())]})
            name = params.get("name") or self.service.l3_selector.active_provider_id
            provider = self._provider(name)
            if command == "inspect":
                state = await self._thread(provider.inspect)
                return self._result({"provider_id": name, **state})
            if command == "refresh-indexes":
                if not getattr(provider, "mounted", True) or getattr(provider, "read_only", False):
                    raise RuntimeError("Provider is detached or read-only")
                state = await self._thread(lambda: provider.refresh_indexes(limit=limit, retry_failed=retry_failed))
                failed = bool(state.get("last_embedding_error") or state.get("read_only")
                              or state.get("detail") == "embedding-provider-unavailable")
                return self._result({"provider_id": name, **state}, failed=failed)
            # Fast lifecycle controls run on the resident loop under the ingress
            # transition lock; no new conversation can enter between check/apply.
            async with self.core.state.channel_turn_transition_lock:
                if self.core.memory_maintenance_busy():
                    raise RuntimeError("Pal is busy; retry memory backend maintenance when idle")
                if command == "select":
                    if not getattr(provider, "mounted", True):
                        raise RuntimeError("Attach the provider before selecting it")
                    self.service.l3_selector.active_provider_id = name
                    return {**self._result({"active_provider_id": name}), "selection_scope": "until_restart"}
                result = getattr(provider, command)(IntrospectionCall(name="memory_cli", args={}))
                return self._result({"provider_id": name, **dict(result.structured or {})},
                                    failed=result.status != RuntimeStatus.OK)

    @staticmethod
    async def _thread(function):
        # Cancellation cannot release the plugin generation lease while the
        # worker thread still owns its database/provider objects.
        task = asyncio.create_task(asyncio.to_thread(function))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            raise

    @staticmethod
    def _result(state, *, failed=False):
        return {"status": "error" if failed else "ok", "activation": "live", "state": state}
