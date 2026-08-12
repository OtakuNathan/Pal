from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.foundation.sidecar import (
    SidecarEndpoint,
    SidecarRpcClient,
    SidecarRpcError,
    cleanup_sidecar_endpoint,
    open_sidecar_connection,
    python_subprocess_env,
    run_blocking,
    start_sidecar_server,
)


ROLE_GATEWAY_TOKEN_ENV = "PAL_MINION_ROLE_ASSIGNMENT_TOKEN"
MINION_RUNTIME_DB_PATH_ENV = "PAL_MINION_RUNTIME_DB_PATH"


def minion_runtime_dir(runtime_root: Path) -> Path:
    return Path(runtime_root) / "data" / "minion"


def minion_socket_path(runtime_root: Path) -> Path:
    return _minion_endpoint(runtime_root).socket_path


def minion_port_path(runtime_root: Path) -> Path:
    return _minion_endpoint(runtime_root).port_path


def minion_role_socket_path(runtime_root: Path) -> Path:
    return _minion_role_endpoint(runtime_root).socket_path


def minion_role_port_path(runtime_root: Path) -> Path:
    return _minion_role_endpoint(runtime_root).port_path


class MinionManagerRpcError(SidecarRpcError):
    pass


DEFAULT_MINION_MANAGER_REQUEST_TIMEOUT_SECONDS = 300.0


@dataclass
class MinionManagerClient:
    runtime_root: Path
    request_timeout_seconds: float = DEFAULT_MINION_MANAGER_REQUEST_TIMEOUT_SECONDS
    _client: SidecarRpcClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = SidecarRpcClient(
            endpoint=_minion_endpoint(self.runtime_root),
            request_timeout_seconds=self.request_timeout_seconds,
        )

    @property
    def socket_path(self) -> Path:
        return minion_socket_path(self.runtime_root)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await self._client.request(method, params)
        except SidecarRpcError as exc:
            raise MinionManagerRpcError(str(exc), kind=exc.kind, payload=exc.payload) from exc

    async def stream(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            async for item in self._client.stream(method, params):
                yield item
        except SidecarRpcError as exc:
            raise MinionManagerRpcError(str(exc), kind=exc.kind, payload=exc.payload) from exc

    def request_sync(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return run_blocking(self.request(method, params))

    def stream_sync(self, method: str, params: dict[str, Any] | None = None):
        return self._client.stream_sync(method, params)

    def health_sync(self) -> dict[str, Any]:
        return self.request_sync("health")

    def list_runs_sync(self) -> dict[str, Any]:
        return self.request_sync("list_runs")

    def read_run_sync(self, run_id: str) -> dict[str, Any]:
        return self.request_sync("read_run", {"run_id": run_id})

    def send_decision_sync(self, decision: dict[str, Any]) -> dict[str, Any]:
        return self.request_sync("send_decision", {"decision": dict(decision)})

    def send_clarification_sync(self, clarification: dict[str, Any]) -> dict[str, Any]:
        return self.request_sync("send_clarification", {"clarification": dict(clarification)})

    def reload_runtime_config_sync(self) -> dict[str, Any]:
        return self.request_sync("reload_runtime_config")

    async def refresh_llm_endpoints(self) -> dict[str, Any]:
        return await self.request("refresh_llm_endpoints")

    def replace_harness_registry_sync(
        self,
        generation: dict[str, Any],
    ) -> dict[str, Any]:
        return self.request_sync(
            "replace_harness_registry",
            {"generation": dict(generation)},
        )

    def catalog_snapshot_sync(
        self,
        *,
        kind: str = "all",
        query: str = "",
        include_definitions: bool = False,
    ) -> dict[str, Any]:
        return self.request_sync(
            "catalog_snapshot",
            {"kind": kind, "query": query, "include_definitions": include_definitions},
        )

    def refresh_catalog_sync(self, *, actor: str = "pal") -> dict[str, Any]:
        return self.request_sync("catalog_refresh", {"actor": actor})

    def set_profile_override_sync(
        self,
        *,
        profile: str,
        changes: dict[str, Any],
        actor: str = "pal",
        if_generation: str = "",
    ) -> dict[str, Any]:
        return self.request_sync(
            "catalog_set_profile_override",
            {
                "profile": profile,
                "changes": dict(changes),
                "actor": actor,
                "if_generation": if_generation,
            },
        )

    def reset_profile_override_sync(
        self,
        *,
        profile: str,
        actor: str = "pal",
        if_generation: str = "",
    ) -> dict[str, Any]:
        return self.request_sync(
            "catalog_reset_profile_override",
            {"profile": profile, "actor": actor, "if_generation": if_generation},
        )

    def set_family_override_sync(
        self,
        *,
        family: str,
        changes: dict[str, Any],
        actor: str = "pal",
        if_generation: str = "",
    ) -> dict[str, Any]:
        return self.request_sync(
            "catalog_set_family_override",
            {
                "family": family,
                "changes": dict(changes),
                "actor": actor,
                "if_generation": if_generation,
            },
        )

    def reset_family_override_sync(
        self,
        *,
        family: str,
        actor: str = "pal",
        if_generation: str = "",
    ) -> dict[str, Any]:
        return self.request_sync(
            "catalog_reset_family_override",
            {"family": family, "actor": actor, "if_generation": if_generation},
        )

    def shutdown_sync(self, *, graceful: bool = True, timeout_seconds: float | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"graceful": graceful}
        if timeout_seconds is not None:
            params["timeout_seconds"] = timeout_seconds
        return self.request_sync("shutdown", params)


@dataclass
class MinionRoleGatewayClient:
    runtime_root: Path
    access_token: str
    request_timeout_seconds: float = DEFAULT_MINION_MANAGER_REQUEST_TIMEOUT_SECONDS
    _client: SidecarRpcClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not str(self.access_token or "").strip():
            raise ValueError("role gateway access token is required")
        self._client = SidecarRpcClient(
            endpoint=_minion_role_endpoint(self.runtime_root),
            request_timeout_seconds=self.request_timeout_seconds,
            unix_only=os.environ.get("PAL_MINION_SANDBOXED") == "1",
        )

    @property
    def socket_path(self) -> Path:
        return minion_role_socket_path(self.runtime_root)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {**dict(params or {}), "access_token": str(self.access_token)}
        try:
            return await self._client.request(method, payload)
        except SidecarRpcError as exc:
            raise MinionManagerRpcError(str(exc), kind=exc.kind, payload=exc.payload) from exc

    async def stream(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        payload = {**dict(params or {}), "access_token": str(self.access_token)}
        try:
            async for item in self._client.stream(method, payload):
                yield item
        except SidecarRpcError as exc:
            raise MinionManagerRpcError(str(exc), kind=exc.kind, payload=exc.payload) from exc

    def request_sync(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return run_blocking(self.request(method, params))

    def stream_sync(self, method: str, params: dict[str, Any] | None = None):
        payload = {**dict(params or {}), "access_token": str(self.access_token)}
        return self._client.stream_sync(method, payload)


async def open_manager_connection(runtime_root: Path):
    return await open_sidecar_connection(_minion_endpoint(runtime_root))


async def start_manager_server(runtime_root: Path, handler):
    return await start_sidecar_server(_minion_endpoint(runtime_root), handler)


async def cleanup_manager_endpoint(runtime_root: Path) -> None:
    await cleanup_sidecar_endpoint(_minion_endpoint(runtime_root))


async def start_role_gateway_server(runtime_root: Path, handler):
    return await start_sidecar_server(_minion_role_endpoint(runtime_root), handler)


async def cleanup_role_gateway_endpoint(runtime_root: Path) -> None:
    await cleanup_sidecar_endpoint(_minion_role_endpoint(runtime_root))


def _minion_endpoint(runtime_root: Path) -> SidecarEndpoint:
    return SidecarEndpoint(runtime_root=Path(runtime_root), name="minion")


def _minion_role_endpoint(runtime_root: Path) -> SidecarEndpoint:
    return SidecarEndpoint(
        runtime_root=Path(runtime_root),
        name="minion-role",
        socket_filename="role.sock",
        port_filename="role.port",
    )
