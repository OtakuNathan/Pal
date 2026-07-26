"""Runtime-root channel provider declarations for the WebSocket bridge (review_guarded).

Responsibility: own the WebSocket sidecar subprocess lifecycle and expose it as a
TRANSPORT-LIFECYCLE channel endpoint through ``ChannelEndpointProviderManager``,
reusing the existing socket channel for ALL message semantics.

This is intentionally NOT a parallel WebSocket channel. The bridge endpoint
performs no direct message ingress: inbound WebSocket messages are delivered by
the sidecar into the existing socket channel user-message path, and replies flow
back over the socket channel. Active send is exposed through the standard
endpoint ``send_message(message)`` contract; provider-specific peer addressing
remains private. No new WebSocket message kind, envelope schema, control
protocol, or message semantics is introduced.

This file declares the public provider and endpoint surface only. Subprocess
supervision, sidecar RPC health/shutdown, and introspection wiring are private
implementation owned by the Coder. It is loaded as a runtime-root provider via
``provider.toml`` + ``build_channel_provider`` and discovered by the channel
provider manager (installation into ``<runtime_root>/channel/providers/``
follows repo bootstrap conventions).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.channel.channel_endpoint_queue_base import ChannelEndpointQueueBase
from pal.channel.contracts import (
    ChannelDeliveryError,
    ChannelMessageReceipt,
    EndpointConfig,
    ResponseHandle,
)
from pal.channel.models import ChannelEndpointModel
from pal.channel.provider_manager import (
    ChannelProvider,
    ChannelProviderContext,
)
from pal.foundation.sidecar import (
    SidecarEndpoint,
    SidecarRpcClient,
    SidecarRpcError,
    cleanup_sidecar_endpoint,
    python_subprocess_env,
)
from pal.shared import IntrospectionResult, RuntimeStatus
from pal.shared.result_rendering import render_titled_structured_for_llm


logger = logging.getLogger(__name__)

PROVIDER_ID = "websocket_bridge"
ENDPOINT_TYPE = "websocket_bridge"

# The sidecar process and its manager socket live under
# ``<runtime_root>/data/websocket_bridge/`` (the repo SidecarEndpoint convention).
SIDECAR_NAME = "websocket_bridge"

# Environment variable used to hand the serialized ``SidecarConfig`` to the child
# process. The child reconstructs the config and runs ``sidecar.serve``.
_CONFIG_ENV = "PAL_WEBSOCKET_BRIDGE_CONFIG"

# Bootstrap executed inside the sidecar subprocess. It imports the sibling
# ``sidecar`` module (the provider directory is placed on PYTHONPATH) and runs
# the declared ``serve`` entrypoint with the reconstructed config. The provider
# never inspects the sidecar's private implementation; it owns only the process
# handle and the manager-socket supervision.
_SIDECAR_BOOTSTRAP = (
    "import asyncio, json, os, pathlib\n"
    "from sidecar import serve, SidecarConfig\n"
    "payload = json.loads(os.environ['PAL_WEBSOCKET_BRIDGE_CONFIG'])\n"
    "payload['runtime_root'] = pathlib.Path(payload['runtime_root'])\n"
    "payload['socket_channel_path'] = pathlib.Path(payload['socket_channel_path'])\n"
    "asyncio.run(serve(SidecarConfig(**payload)))\n"
)

# Directory containing this module. Resolved at import time so subprocess spawns
# work both for in-repo use and after the provider is copied into a runtime root.
PROVIDER_DIR = Path(__file__).resolve().parent


@dataclass
class WebSocketBridgeEndpoint(ChannelEndpointQueueBase):
    """Transport-lifecycle channel endpoint owning the sidecar subprocess.

    Performs no direct message ingress and adds no channel semantics. It starts/stops
    the sidecar process on ``start_async``/``stop_async`` (called by
    ``ChannelRuntime``) and reports provider-owned health/auth/backlog. All
    message semantics are reused from the existing socket channel.

    Like the sibling ``TelegramChannelEndpoint``/``SocketChannelEndpoint``, this
    is a ``@dataclass`` subclass whose added fields all carry defaults, so it
    stays constructible with the base ``ChannelEndpointQueueBase`` constructor
    (whose only required field is ``endpoint``). The provider's ``create_endpoint``
    factory populates ``runtime_root``/``socket_channel_path``/``binding_metadata``
    from the ``channel_endpoints`` row, mirroring how the telegram factory injects
    ``runtime_root`` and binding data.
    """

    runtime_root: Any = None
    socket_channel_path: Any = None
    binding_metadata: dict[str, Any] = field(default_factory=dict)
    # Lifecycle tunables (overridable per-instance, e.g. in tests).
    startup_probe_seconds: float = 5.0
    shutdown_rpc_seconds: float = 3.0
    shutdown_wait_seconds: float = 2.0
    rpc_timeout_seconds: float = 3.0
    message_timeout_seconds: float = 3000.0
    _process: Any = field(default=None, init=False, repr=False)
    _startup_error: str = field(default="", init=False, repr=False)
    # Test/injection hook: when set, spawn this command instead of the default
    # sidecar bootstrap. Production leaves it as ``None``.
    _sidecar_command_override: tuple[str, ...] | None = field(default=None, init=False, repr=False)

    async def start_async(self) -> None:
        """Spawn and supervise the sidecar subprocess (repo sidecar conventions)."""
        if self._process is not None and self._process.poll() is None:
            return
        if not self.runtime_root or not self.socket_channel_path:
            self._startup_error = "bridge endpoint missing runtime_root or socket_channel_path"
            return
        self._startup_error = ""
        try:
            process = self._spawn_sidecar()
        except Exception as exc:  # pragma: no cover - defensive spawn guard
            self._startup_error = f"sidecar spawn failed: {exc.__class__.__name__}: {exc}"
            self._process = None
            logger.exception("websocket bridge sidecar failed to spawn")
            return
        self._process = process
        await self._await_sidecar_ready(process)

    async def stop_async(self) -> None:
        """Cleanly shut down the sidecar subprocess and release the manager socket."""
        process = self._process
        if process is None:
            return
        loop = asyncio.get_running_loop()
        # Best-effort clean shutdown over the manager socket RPC.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self._rpc_client().request("shutdown"),
                timeout=self.shutdown_rpc_seconds,
            )
        exited = await self._await_exit(process, loop, self.shutdown_wait_seconds)
        if not exited:
            _force_terminate(process)
        self._process = None
        with contextlib.suppress(Exception):
            await cleanup_sidecar_endpoint(self._manager_endpoint())

    def normalize_raw(self, payload: Any) -> dict[str, Any]:
        """No ingress: the bridge never normalizes inbound messages into the runtime."""
        return {}

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        """No channel replies: replies flow over the existing socket channel."""
        return

    async def send_message(self, message: str) -> ChannelMessageReceipt:
        """Initiate one ordinary peer message and return its socket reply."""
        process = self._process
        if process is None or process.poll() is not None:
            raise ChannelDeliveryError(
                "websocket bridge sidecar is not running",
                permanent=False,
                reason="sidecar_unavailable",
            )
        try:
            result = await self._rpc_client(
                request_timeout_seconds=self.message_timeout_seconds + 5.0
            ).request("send_message", {"message": message})
        except SidecarRpcError as exc:
            reason = "response_timeout" if exc.kind == "timeout" else "websocket_send_failed"
            raise ChannelDeliveryError(
                str(exc),
                permanent=False,
                reason=reason,
            ) from exc
        return ChannelMessageReceipt(
            endpoint_id=self.endpoint.endpoint_id,
            message_id=str(result.get("message_id") or ""),
            status="completed",
            response_text=str(result.get("response") or ""),
        )

    def inspect_health(self) -> dict[str, Any]:
        """Report sidecar process and connection health."""
        process = self._process
        process_running = process is not None and process.poll() is None
        health: dict[str, Any] = {
            "endpoint_id": self.endpoint.endpoint_id,
            "process_running": process_running,
            "listener_bound": False,
            "connected_peers": 0,
            "last_error": self._startup_error,
            "healthy": False,
        }
        if not process_running:
            if not health["last_error"] and process is not None:
                health["last_error"] = _exit_reason(process)
            return health
        try:
            result = self._rpc_client().request_sync("health")
        except Exception as exc:
            health["last_error"] = f"health probe failed: {exc.__class__.__name__}"
            return health
        health["listener_bound"] = bool(result.get("listener_bound"))
        health["connected_peers"] = int(result.get("connected_peers") or 0)
        health["last_error"] = str(result.get("last_error") or "")
        health["healthy"] = bool(health["listener_bound"])
        return health

    def inspect_auth_state(self) -> dict[str, Any]:
        """Report trusted-LAN peer pairing/authorization state without secrets."""
        metadata = dict(self.binding_metadata or {})
        return {
            "endpoint_id": self.endpoint.endpoint_id,
            "paired": bool(self.paired),
            "attached": bool(self.attached),
            "authorized": bool(self.paired),
            "peer_url": _clean_str(metadata.get("peer_url")) or None,
            "bind_host": _clean_str(metadata.get("bind_host")) or "0.0.0.0",
            "bind_port": _as_int(metadata.get("bind_port"), 0),
        }

    # -- private sidecar supervision ----------------------------------------

    def _manager_endpoint(self) -> SidecarEndpoint:
        return SidecarEndpoint(
            runtime_root=Path(str(self.runtime_root)),
            name=SIDECAR_NAME,
        )

    def _rpc_client(self, *, request_timeout_seconds: float | None = None) -> SidecarRpcClient:
        return SidecarRpcClient(
            endpoint=self._manager_endpoint(),
            request_timeout_seconds=(
                self.rpc_timeout_seconds
                if request_timeout_seconds is None
                else request_timeout_seconds
            ),
        )

    def _sidecar_command(self) -> list[str]:
        if self._sidecar_command_override:
            return list(self._sidecar_command_override)
        return [sys.executable, "-c", _SIDECAR_BOOTSTRAP]

    def _sidecar_config_payload(self) -> dict[str, Any]:
        metadata = dict(self.binding_metadata or {})
        return {
            "runtime_root": str(Path(str(self.runtime_root))),
            "socket_channel_path": str(Path(str(self.socket_channel_path))),
            "bind_host": _clean_str(metadata.get("bind_host")) or "0.0.0.0",
            "bind_port": _as_int(metadata.get("bind_port"), 0),
            "peer_url": _clean_str(metadata.get("peer_url")) or None,
            "reconnect_initial_delay_seconds": _as_float(
                metadata.get("reconnect_initial_delay_seconds"), 1.0
            ),
            "reconnect_max_delay_seconds": _as_float(
                metadata.get("reconnect_max_delay_seconds"), 30.0
            ),
            "message_timeout_seconds": _as_float(
                metadata.get("message_timeout_seconds"), self.message_timeout_seconds
            ),
            "binding_metadata": metadata,
        }

    def _spawn_sidecar(self) -> subprocess.Popen[bytes]:
        env = python_subprocess_env()
        existing_path = [
            entry for entry in str(env.get("PYTHONPATH") or "").split(os.pathsep) if entry
        ]
        provider_dir = str(PROVIDER_DIR)
        if provider_dir not in existing_path:
            env["PYTHONPATH"] = os.pathsep.join([provider_dir, *existing_path])
        env[_CONFIG_ENV] = json.dumps(self._sidecar_config_payload())
        runtime_dir = self._manager_endpoint().runtime_dir
        runtime_dir.mkdir(parents=True, exist_ok=True)
        return subprocess.Popen(
            self._sidecar_command(),
            env=env,
            cwd=provider_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    async def _await_sidecar_ready(self, process: subprocess.Popen[bytes]) -> None:
        socket_path = self._manager_endpoint().socket_path
        deadline = time.monotonic() + self.startup_probe_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._startup_error = _exit_reason(process)
                return
            if socket_path.exists():
                return
            await asyncio.sleep(0.1)
        # Timed out waiting for the manager socket while the process is still
        # alive: assume it is still starting. Health probes report the live state.

    async def _await_exit(
        self,
        process: subprocess.Popen[bytes],
        loop: asyncio.AbstractEventLoop,
        timeout: float,
    ) -> bool:
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, process.wait),
                timeout=timeout,
            )
            return True
        except asyncio.TimeoutError:
            return process.poll() is not None


@dataclass
class WebSocketBridgeProvider:
    """Custom ChannelProvider owning the bridge sidecar lifecycle and introspection."""

    provider_id: str = PROVIDER_ID
    endpoint_types: tuple[str, ...] = (ENDPOINT_TYPE,)
    reload_modules: tuple[str, ...] = ("runtime", "sidecar", "sidecar_main", "protocol")

    def create_endpoint(
        self,
        record: ChannelEndpointModel,
        context: ChannelProviderContext,
    ) -> ChannelEndpointQueueBase | None:
        """Build the transport-lifecycle endpoint from its channel_endpoints row."""
        if str(record.channel_kind or "").strip() != ENDPOINT_TYPE:
            return None
        runtime_root = Path(str(context.runtime_root))
        endpoint = WebSocketBridgeEndpoint(
            endpoint=EndpointConfig(
                endpoint_id=str(record.endpoint_id),
                channel_kind=str(record.channel_kind),
                binding_key=str(record.binding_key),
                send_policy=dict(record.send_policy_blob or {}),
            )
        )
        endpoint.runtime_root = runtime_root
        endpoint.socket_channel_path = runtime_root / "pal.sock"
        endpoint.binding_metadata = dict(record.binding_metadata or {})
        endpoint.message_timeout_seconds = _as_float(
            endpoint.binding_metadata.get("message_timeout_seconds"),
            endpoint.message_timeout_seconds,
        )
        endpoint.enabled = bool(record.enabled)
        endpoint.attached = record.detached_at is None
        endpoint.paired = True
        return endpoint

    def attach_endpoint(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        """Attach the endpoint and start the sidecar."""
        record = context.repository.set_attached(endpoint_id, True)
        if record is None:
            return _not_found(endpoint_id)
        endpoint = self.create_endpoint(record, context)
        if endpoint is None:
            return _provider_missing(endpoint_id, str(record.channel_kind))
        _preserve_state(context.runtime.get_endpoint(endpoint_id), endpoint)
        endpoint.attached = True
        context.runtime.replace_endpoint(endpoint)
        return _ok(
            "WebSocket bridge endpoint attached",
            {
                "endpoint_id": endpoint_id,
                "endpoint_type": record.channel_kind,
                "provider_id": self.provider_id,
                "reload_modules": list(self.reload_modules),
                "attached": True,
                "enabled": bool(endpoint.enabled),
            },
        )

    def detach_endpoint(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        """Stop the sidecar and detach the endpoint (reversible lifecycle)."""
        endpoint = context.runtime.get_endpoint(endpoint_id)
        record = context.repository.get(endpoint_id)
        record = context.repository.set_attached(endpoint_id, False)
        if record is None and endpoint is None:
            return _not_found(endpoint_id)
        if endpoint is not None:
            endpoint.detach()
        removed = context.runtime.remove_endpoint(endpoint_id)
        endpoint_type = _endpoint_type_of(record, endpoint)
        return _ok(
            "WebSocket bridge endpoint detached",
            {
                "endpoint_id": endpoint_id,
                "endpoint_type": endpoint_type,
                "provider_id": self.provider_id,
                "attached": False,
                "removed_runtime_endpoint": bool(removed),
            },
        )

    def restart_endpoint(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        """Restart the endpoint runtime instance and the sidecar."""
        record = context.repository.get(endpoint_id)
        if record is None:
            return _not_found(endpoint_id)
        _drop_module_cache(self.reload_modules)
        endpoint = self.create_endpoint(record, context)
        if endpoint is None:
            return _provider_missing(endpoint_id, str(record.channel_kind))
        _preserve_state(context.runtime.get_endpoint(endpoint_id), endpoint)
        context.runtime.replace_endpoint(endpoint)
        return _ok(
            "WebSocket bridge endpoint restarted",
            {
                "endpoint_id": endpoint_id,
                "endpoint_type": record.channel_kind,
                "provider_id": self.provider_id,
                "reload_modules": list(self.reload_modules),
                "attached": bool(endpoint.attached),
                "enabled": bool(endpoint.enabled),
            },
        )

    def inspect_endpoint(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        record = context.repository.get(endpoint_id)
        endpoint = context.runtime.get_endpoint(endpoint_id)
        if record is None and endpoint is None:
            return _not_found(endpoint_id)
        payload = _snapshot(endpoint_id, record, endpoint)
        payload["provider_id"] = self.provider_id
        return _ok("WebSocket bridge endpoint snapshot", payload)

    def inspect_auth_state(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        record = context.repository.get(endpoint_id)
        endpoint = context.runtime.get_endpoint(endpoint_id)
        if record is None and endpoint is None:
            return _not_found(endpoint_id)
        if endpoint is None:
            payload = {
                "endpoint_id": endpoint_id,
                "provider_id": self.provider_id,
                "paired": False,
                "attached": record.detached_at is None if record is not None else False,
                "authorized": False,
            }
            return _ok("WebSocket bridge authorization state", payload)
        payload = _sanitize(dict(endpoint.inspect_auth_state()))
        payload.setdefault("endpoint_id", endpoint_id)
        payload.setdefault("provider_id", self.provider_id)
        return _ok("WebSocket bridge authorization state", payload)

    def set_auth_material(
        self,
        endpoint_id: str,
        material: dict[str, Any],
        context: ChannelProviderContext,
    ) -> IntrospectionResult:
        endpoint = context.runtime.get_endpoint(endpoint_id)
        if endpoint is None:
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="channel endpoint runtime not found",
                llm_text="channel endpoint runtime not found",
            )
        auth_state = endpoint.apply_auth_material(dict(material))
        with contextlib.suppress(Exception):
            context.repository.merge_binding_metadata(
                endpoint_id,
                {
                    "auth_keys": sorted(str(key) for key in material.keys()),
                    "paired": bool(endpoint.paired),
                },
            )
        payload = _sanitize(dict(auth_state))
        payload.setdefault("endpoint_id", endpoint_id)
        payload.setdefault("provider_id", self.provider_id)
        payload.setdefault("accepted_keys", sorted(str(key) for key in material.keys()))
        return _ok("WebSocket bridge auth material updated", payload)

    def inspect_backlog(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        record = context.repository.get(endpoint_id)
        endpoint = context.runtime.get_endpoint(endpoint_id)
        if record is None and endpoint is None:
            return _not_found(endpoint_id)
        if endpoint is None:
            payload = {
                "endpoint_id": endpoint_id,
                "provider_id": self.provider_id,
                "inbox_size": 0,
                "outbox_size": 0,
            }
            return _ok("WebSocket bridge backlog state", payload)
        payload = dict(endpoint.inspect_backlog())
        payload.setdefault("endpoint_id", endpoint_id)
        payload.setdefault("provider_id", self.provider_id)
        return _ok("WebSocket bridge backlog state", payload)

    def inspect_health(self, endpoint_id: str, context: ChannelProviderContext) -> IntrospectionResult:
        record = context.repository.get(endpoint_id)
        endpoint = context.runtime.get_endpoint(endpoint_id)
        if record is None and endpoint is None:
            return _not_found(endpoint_id)
        if endpoint is None:
            payload = {
                "endpoint_id": endpoint_id,
                "provider_id": self.provider_id,
                "attached": record.detached_at is None if record is not None else False,
                "enabled": bool(record.enabled) if record is not None else False,
                "healthy": False,
                "reason": "runtime_endpoint_missing",
            }
            return _ok("WebSocket bridge health", payload)
        payload = _sanitize(dict(endpoint.inspect_health()))
        payload.setdefault("endpoint_id", endpoint_id)
        payload.setdefault("provider_id", self.provider_id)
        payload.setdefault("attached", bool(endpoint.attached))
        payload.setdefault("enabled", bool(endpoint.enabled))
        return _ok("WebSocket bridge health", payload)


def build_channel_provider(context: Any) -> ChannelProvider:
    """Runtime-root provider entrypoint consumed by ChannelEndpointProviderManager."""
    _ = context
    return WebSocketBridgeProvider()


# -- private provider helpers -------------------------------------------------


def _ok(text: str, payload: dict[str, Any]) -> IntrospectionResult:
    return IntrospectionResult(
        status=RuntimeStatus.OK,
        text=text,
        structured=payload,
        llm_text=render_titled_structured_for_llm(text, payload),
    )


def _not_found(endpoint_id: str) -> IntrospectionResult:
    return IntrospectionResult(
        status=RuntimeStatus.NOT_FOUND,
        text="channel endpoint not found",
        structured={"endpoint_id": endpoint_id},
        llm_text="channel endpoint not found",
    )


def _provider_missing(endpoint_id: str, endpoint_type: str) -> IntrospectionResult:
    return IntrospectionResult(
        status=RuntimeStatus.NOT_FOUND,
        text="channel provider not found",
        structured={
            "endpoint_id": endpoint_id,
            "endpoint_type": endpoint_type,
            "channel_kind": endpoint_type,
        },
        llm_text="channel provider not found",
    )


def _sanitize(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    for key in ("token", "secret", "bot_token", "password"):
        sanitized.pop(key, None)
    return sanitized


def _preserve_state(
    old_endpoint: ChannelEndpointQueueBase | None,
    new_endpoint: ChannelEndpointQueueBase,
) -> None:
    if old_endpoint is None or old_endpoint is new_endpoint:
        return
    if getattr(old_endpoint, "paired", False):
        new_endpoint.paired = True
    pairing_metadata = dict(getattr(old_endpoint, "pairing_metadata", {}) or {})
    if pairing_metadata and hasattr(new_endpoint, "pairing_metadata"):
        new_endpoint.pairing_metadata.update(pairing_metadata)


def _drop_module_cache(prefixes: tuple[str, ...]) -> None:
    clean = tuple(dict.fromkeys(str(prefix).strip() for prefix in prefixes if str(prefix).strip()))
    if not clean:
        return
    importlib.invalidate_caches()
    for module_name in list(sys.modules):
        if any(module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in clean):
            sys.modules.pop(module_name, None)


def _snapshot(
    endpoint_id: str,
    record: ChannelEndpointModel | None,
    endpoint: ChannelEndpointQueueBase | None,
) -> dict[str, Any]:
    endpoint_type = _endpoint_type_of(record, endpoint)
    binding_key = (
        record.binding_key
        if record is not None
        else endpoint.endpoint.binding_key
        if endpoint is not None
        else ""
    )
    enabled = (
        bool(record.enabled)
        if record is not None
        else bool(endpoint.enabled)
        if endpoint is not None
        else False
    )
    attached = (
        bool(endpoint.attached)
        if endpoint is not None
        else record.detached_at is None
        if record is not None
        else False
    )
    return {
        "endpoint_id": endpoint_id,
        "endpoint_type": endpoint_type,
        "channel_kind": endpoint_type,
        "binding_key": binding_key,
        "enabled": enabled,
        "attached": attached,
        "paired": bool(getattr(endpoint, "paired", False)) if endpoint is not None else False,
        "runtime_endpoint_present": endpoint is not None,
    }


def _endpoint_type_of(
    record: ChannelEndpointModel | None,
    endpoint: ChannelEndpointQueueBase | None,
) -> str:
    if record is not None:
        return str(record.channel_kind)
    if endpoint is not None:
        return endpoint.endpoint.channel_kind
    return ENDPOINT_TYPE


def _exit_reason(process: subprocess.Popen[bytes]) -> str:
    code = process.returncode
    return f"sidecar process exited with code {code}"


def _force_terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        process_group = os.getpgid(process.pid)
    except (OSError, ProcessLookupError):
        process_group = 0
    owns_group = bool(process_group) and process_group == process.pid
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if process.poll() is not None:
            break
        with contextlib.suppress(ProcessLookupError, OSError):
            if owns_group:
                os.killpg(process_group, sig)
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            continue
        break


def _clean_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "PROVIDER_ID",
    "ENDPOINT_TYPE",
    "WebSocketBridgeEndpoint",
    "WebSocketBridgeProvider",
    "build_channel_provider",
]
