"""A resident configuration owner endpoint, independent of conversation turns."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from pal.control.configuration_contracts import ConfigurationOperations
from pal.foundation.lifecycle_gate import WriterPreferredRWGate
from pal.foundation.workers import join_owned_task

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
    lifecycle_gate: WriterPreferredRWGate
    operations: ConfigurationOperations = field(default_factory=dict)
    _server: Any = field(default=None, init=False, repr=False)
    _clients: dict[asyncio.Task, asyncio.StreamWriter] = field(default_factory=dict, init=False, repr=False)
    _closing: bool = field(default=True, init=False)
    _stop_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.runtime_root = Path(self.runtime_root).expanduser().resolve()

    async def start_async(self) -> None:
        if self._server is not None:
            return
        self._server, _ = await start_sidecar_server(configuration_endpoint(self.runtime_root), self._handle_client)
        self._stop_task = None
        self._closing = False

    async def stop_async(self) -> None:
        self._closing = True
        if (self._stop_task is None or (self._stop_task.done()
                and (self._stop_task.cancelled() or self._stop_task.exception() is not None))):
            self._stop_task = asyncio.create_task(self._stop(), name="configuration-stop")
        await join_owned_task(self._stop_task)

    async def _stop(self) -> None:
        server = self._server
        if server is None:
            return
        server.close()
        clients = dict(self._clients)
        for writer in clients.values():
            writer.close()
        # Some event loops wait for accepted TCP transports in wait_closed().
        # Close idle clients first so they cannot keep server shutdown pending.
        await server.wait_closed()
        # Join in-flight owner operations before dependency teardown. Closing
        # writers wakes idle readers without cancelling a provider worker.
        if clients:
            await asyncio.gather(*clients, return_exceptions=True)
        await cleanup_sidecar_endpoint(configuration_endpoint(self.runtime_root))
        self._server = None

    async def _call_method(self, method: str, params: dict) -> dict:
        if self._closing:
            raise RuntimeError("Core configuration service is stopping")
        operation = self.operations.get(method)
        if operation is None:
            raise ValueError("Unknown configuration operation")
        async with self.lifecycle_gate.read_async():
            if self._closing:
                raise RuntimeError("Core configuration service is stopping")
            if self.operations.get(method) is not operation:
                raise ValueError("Configuration operation was withdrawn")
            return await operation[1](params)

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
