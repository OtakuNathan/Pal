"""A resident configuration owner endpoint, independent of conversation turns."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.foundation.sidecar import (
    SidecarEndpoint, cleanup_sidecar_endpoint, dispatch_sidecar_request,
    pack_sidecar_message, read_sidecar_message, start_sidecar_server,
)


def configuration_endpoint(runtime_root: Path) -> SidecarEndpoint:
    return SidecarEndpoint(runtime_root=Path(runtime_root).expanduser().resolve(), name="control",
                           socket_filename="config.sock", port_filename="config.port")


@dataclass
class CoreConfigurationService:
    runtime_root: Path
    cache_reminder: Any
    identity_service: Any = None
    control_plane: Any = None
    _server: Any = field(default=None, init=False, repr=False)
    _clients: dict[asyncio.Task, asyncio.StreamWriter] = field(default_factory=dict, init=False, repr=False)
    _closing: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        self.runtime_root = Path(self.runtime_root).expanduser().resolve()

    async def start_async(self) -> None:
        if self._server is not None:
            return
        self._server, _ = await start_sidecar_server(configuration_endpoint(self.runtime_root), self._handle_client)
        self._closing = False

    async def stop_async(self) -> None:
        self._closing = True
        server = self._server
        if server is None:
            return
        self._server = None
        server.close()
        await server.wait_closed()
        clients = dict(self._clients)
        for writer in clients.values():
            writer.close()
        # No await occurs inside configure: writes finish on the owner loop
        # before shutdown can enter. Idle readers are woken by closing writers.
        if clients:
            await asyncio.gather(*clients, return_exceptions=True)
        await cleanup_sidecar_endpoint(configuration_endpoint(self.runtime_root))

    async def _call_method(self, method: str, params: dict) -> dict:
        if self._closing:
            raise RuntimeError("Core configuration service is stopping")
        if method in {"identity.inspect", "identity.refresh", "control.inspect"}:
            if params:
                raise ValueError("inspection and refresh take no arguments")
            if method == "control.inspect":
                if self.control_plane is None:
                    raise RuntimeError("Control owner is unavailable")
                registry = self.control_plane.registry
                state = {"command_count": len(registry.commands), "alias_count": len(registry.aliases),
                         "commands": sorted(registry.commands), "source": "resident_control_registry"}
            else:
                if self.identity_service is None:
                    raise RuntimeError("Identity owner is unavailable")
                from dataclasses import asdict
                if method == "identity.refresh":
                    persona, preferences = self.identity_service.refresh_projection()
                else:
                    persona = self.identity_service.get_persona()
                    preferences = self.identity_service.get_preferences()
                state = {"has_persona": persona is not None, "has_preferences": preferences is not None,
                         "persona": asdict(persona) if persona is not None else None,
                         "preferences": asdict(preferences) if preferences is not None else None}
            result = {"status": "ok", "activation": "live", "state": state}
            if method.startswith("identity."):
                result.update(refresh_scope="resident_projection", startup_pinned_fields=["display_name", "core_policy"])
            return result
        if method == "cache_reminder.inspect":
            if params:
                raise ValueError("inspect takes no arguments")
            return {"status": "ok", "activation": "live", "state": self.cache_reminder.inspect()}
        if method != "cache_reminder.configure":
            raise ValueError("Unknown core configuration operation")
        from pal.execution.generated_tool_models import CoreCapabilitiesCoreIntrospectionProviderConfigureCacheWarmDeadlineInput
        model = CoreCapabilitiesCoreIntrospectionProviderConfigureCacheWarmDeadlineInput.model_validate(params)
        patch = model.model_dump(exclude_unset=True)
        if not patch:
            raise ValueError("configure requires at least one setting")
        return {"status": "ok", "activation": "live", "state": self.cache_reminder.configure(**patch)}

    async def _handle_client(self, reader, writer) -> None:
        task = asyncio.current_task()
        self._clients[task] = writer
        try:
            while not self._closing:
                request = await read_sidecar_message(reader)
                response = await dispatch_sidecar_request(request, self._call_method)
                writer.write(pack_sidecar_message(response))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
            pass
        finally:
            self._clients.pop(task, None)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
