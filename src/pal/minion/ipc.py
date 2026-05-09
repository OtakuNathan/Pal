from __future__ import annotations

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


def minion_runtime_dir(runtime_root: Path) -> Path:
    return Path(runtime_root) / "data" / "minion"


def minion_socket_path(runtime_root: Path) -> Path:
    return _minion_endpoint(runtime_root).socket_path


def minion_port_path(runtime_root: Path) -> Path:
    return _minion_endpoint(runtime_root).port_path


def minion_log_path(runtime_root: Path) -> Path:
    return minion_runtime_dir(runtime_root) / "manager.log"


def minion_runner_log_path(runtime_root: Path, work_order_id: str, profile: str) -> Path:
    work_order_part = _safe_log_component(work_order_id) or "work_order"
    profile_part = _safe_log_component(profile) or "generic"
    return Path(runtime_root) / f"{work_order_part}.{profile_part}.log"


def _safe_log_component(value: str) -> str:
    text = str(value or "").strip()
    safe = []
    for char in text:
        if char.isalnum() or char in {"-", "_", "."}:
            safe.append(char)
        else:
            safe.append("_")
    return "".join(safe).strip("._")[:120]


class MinionManagerRpcError(SidecarRpcError):
    pass


@dataclass
class MinionManagerClient:
    runtime_root: Path
    request_timeout_seconds: float = 30.0
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

    def request_sync(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return run_blocking(self.request(method, params))

    def health_sync(self) -> dict[str, Any]:
        return self.request_sync("health")

    def list_runs_sync(self) -> dict[str, Any]:
        return self.request_sync("list_runs")

    def read_run_sync(self, run_id: str) -> dict[str, Any]:
        return self.request_sync("read_run", {"run_id": run_id})

    def spawn_sync(self, task_context_pack: dict[str, Any]) -> dict[str, Any]:
        return self.request_sync("spawn", {"task_context_pack": dict(task_context_pack)})

    def kill_sync(self, run_id: str, reason: str = "") -> dict[str, Any]:
        return self.request_sync("kill", {"run_id": run_id, "reason": reason})

    def send_decision_sync(self, decision: dict[str, Any]) -> dict[str, Any]:
        return self.request_sync("send_decision", {"decision": dict(decision)})

    def finalize_work_order_sync(self, work_order_id: str, **params: Any) -> dict[str, Any]:
        return self.request_sync("finalize_work_order", {"work_order_id": work_order_id, **dict(params)})

    def shutdown_sync(self) -> dict[str, Any]:
        return self.request_sync("shutdown")


async def open_manager_connection(runtime_root: Path):
    return await open_sidecar_connection(_minion_endpoint(runtime_root))


async def start_manager_server(runtime_root: Path, handler):
    return await start_sidecar_server(_minion_endpoint(runtime_root), handler)


async def cleanup_manager_endpoint(runtime_root: Path) -> None:
    await cleanup_sidecar_endpoint(_minion_endpoint(runtime_root))


def _minion_endpoint(runtime_root: Path) -> SidecarEndpoint:
    return SidecarEndpoint(runtime_root=Path(runtime_root), name="minion")
