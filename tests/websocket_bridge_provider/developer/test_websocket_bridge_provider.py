"""Developer tests for the Pal-to-Pal LAN WebSocket bridge channel provider.

Coverage focus:
* ``build_channel_provider`` entrypoint and provider metadata.
* ``create_endpoint`` builds a transport-lifecycle endpoint from a
  ``channel_endpoints`` row and rejects foreign channel kinds.
* The endpoint performs NO message ingress (``normalize_raw``/``send_reply``).
* Provider lifecycle: attach registers + starts, detach is reversible,
  restart recreates, missing rows are not-found.
* Provider-owned introspection (snapshot/auth/backlog/health) surfaces
  ``provider_id`` and never leaks secret material.
* Sidecar subprocess ownership: ``start_async`` spawns, ``stop_async``
  terminates, and health reflects live vs dead sidecar state.
* The default sidecar command + serialized config match the declared
  ``websocket_sidecar`` entrypoint contract.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from pal.channel.contracts import ChannelDeliveryError, EndpointConfig
from pal.channel.provider_manager import ChannelProviderContext
from pal.channel.runtime import ChannelRuntime
from pal.shared import RuntimeStatus

from websocket_bridge.runtime import (
    ENDPOINT_TYPE,
    PROVIDER_ID,
    WebSocketBridgeEndpoint,
    WebSocketBridgeProvider,
    build_channel_provider,
)


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class _FakeRepository:
    """In-memory stand-in for ``ChannelEndpointRepository`` (no DB binding)."""

    def __init__(self) -> None:
        self.rows: dict[str, SimpleNamespace] = {}

    def set_attached(self, endpoint_id: str, attached: bool) -> SimpleNamespace | None:
        record = self.rows.get(endpoint_id)
        if record is None:
            return None
        record.detached_at = None if attached else "2024-01-01T00:00:00+00:00"
        return record

    def get(self, endpoint_id: str) -> SimpleNamespace | None:
        return self.rows.get(endpoint_id)

    def merge_binding_metadata(self, endpoint_id: str, patch: dict[str, Any]) -> SimpleNamespace | None:
        record = self.rows.get(endpoint_id)
        if record is None:
            return None
        merged = dict(record.binding_metadata or {})
        merged.update(dict(patch))
        record.binding_metadata = merged
        return record


def _record(
    endpoint_id: str = "wb_main",
    channel_kind: str = ENDPOINT_TYPE,
    binding_key: str = "lan:peer",
    enabled: bool = True,
    attached: bool = True,
    binding_metadata: dict[str, Any] | None = None,
) -> Any:
    return SimpleNamespace(
        endpoint_id=endpoint_id,
        channel_kind=channel_kind,
        binding_key=binding_key,
        enabled=enabled,
        detached_at=None if attached else "2024-01-01T00:00:00+00:00",
        binding_metadata=dict(
            binding_metadata
            or {"bind_host": "0.0.0.0", "bind_port": 8765, "peer_url": "ws://peer:8765"}
        ),
        send_policy_blob={},
    )


def _context(
    provider: WebSocketBridgeProvider,
    repository: _FakeRepository,
    runtime_root: Path,
) -> ChannelProviderContext:
    runtime = ChannelRuntime()
    # Provider lifecycle hooks run behind manager discovery in production.
    # Reproduce that physical lifecycle boundary before invoking the provider
    # directly in these contract tests.
    for record in repository.rows.values():
        runtime.ensure_endpoint_hub(
            record.endpoint_id,
            provider_id=provider.provider_id,
            channel_kind=record.channel_kind,
            binding_key=record.binding_key,
        )
    return ChannelProviderContext(
        runtime=runtime,
        repository=repository,  # type: ignore[arg-type]
        runtime_root=runtime_root,
    )


def _endpoint_config(endpoint_id: str = "wb_main") -> EndpointConfig:
    return EndpointConfig(
        endpoint_id=endpoint_id,
        channel_kind=ENDPOINT_TYPE,
        binding_key="lan:peer",
        send_policy={},
    )


def _make_endpoint(
    runtime_root: Path,
    *,
    binding_metadata: dict[str, Any] | None = None,
    endpoint_id: str = "wb_main",
) -> WebSocketBridgeEndpoint:
    endpoint = WebSocketBridgeEndpoint(
        endpoint=_endpoint_config(endpoint_id),
        socket_path=Path(runtime_root) / "data" / "channel" / endpoint_id / "channel.sock",
    )
    endpoint.runtime_root = runtime_root
    endpoint.data_root = Path(runtime_root) / "data" / "channel" / endpoint_id
    endpoint.binding_metadata = dict(
        binding_metadata
        or {"bind_host": "0.0.0.0", "bind_port": 8765, "peer_url": "ws://peer:8765"}
    )
    return endpoint


_FAKE_SIDECAR_BOOTSTRAP = '''import asyncio, json, os, pathlib
from pal.foundation.sidecar import (
    SidecarEndpoint,
    cleanup_sidecar_endpoint,
    dispatch_sidecar_request,
    handle_sidecar_client,
    start_sidecar_server,
)

payload = json.loads(os.environ["PAL_WEBSOCKET_BRIDGE_CONFIG"])
endpoint = SidecarEndpoint(
    runtime_root=pathlib.Path(payload["runtime_root"]),
    name="websocket_bridge",
    runtime_dir_override=pathlib.Path(payload["data_root"]),
)
stop = asyncio.Event()


async def call_method(method, params):
    if method == "health":
        return {"listener_bound": True, "connected_peers": 1, "last_error": "", "process_running": True}
    if method == "shutdown":
        stop.set()
        return {}
    raise RuntimeError("unknown method " + method)


async def dispatch(request):
    return await dispatch_sidecar_request(request, call_method)


async def handler(reader, writer):
    await handle_sidecar_client(reader, writer, dispatch)


async def main():
    server, _info = await start_sidecar_server(endpoint, handler)
    try:
        await stop.wait()
    finally:
        server.close()
        await server.wait_closed()
        await cleanup_sidecar_endpoint(endpoint)


asyncio.run(main())
'''


# ---------------------------------------------------------------------------
# Provider entrypoint + metadata
# ---------------------------------------------------------------------------


class BuildChannelProviderTests(unittest.TestCase):
    def test_build_returns_websocket_bridge_provider(self) -> None:
        provider = build_channel_provider(object())
        self.assertIsInstance(provider, WebSocketBridgeProvider)
        self.assertEqual(provider.provider_id, PROVIDER_ID)
        self.assertEqual(provider.endpoint_types, (ENDPOINT_TYPE,))
        self.assertEqual(
            provider.reload_modules, ("runtime", "sidecar", "sidecar_main", "protocol")
        )


# ---------------------------------------------------------------------------
# create_endpoint
# ---------------------------------------------------------------------------


class CreateEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_wsb_create_"))
        self.provider = WebSocketBridgeProvider()
        self.repository = _FakeRepository()

    def test_builds_transport_lifecycle_endpoint_from_row(self) -> None:
        record = _record(binding_metadata={"bind_host": "0.0.0.0", "bind_port": 9000})
        context = _context(self.provider, self.repository, self.runtime_root)
        endpoint = self.provider.create_endpoint(record, context)
        self.assertIsInstance(endpoint, WebSocketBridgeEndpoint)
        assert isinstance(endpoint, WebSocketBridgeEndpoint)
        self.assertEqual(endpoint.endpoint.endpoint_id, "wb_main")
        self.assertEqual(endpoint.endpoint.channel_kind, ENDPOINT_TYPE)
        self.assertEqual(endpoint.runtime_root, self.runtime_root)
        self.assertEqual(
            endpoint.socket_path,
            self.runtime_root / "data" / "channel" / "wb_main" / "channel.sock",
        )
        self.assertEqual(endpoint.binding_metadata["bind_port"], 9000)
        self.assertTrue(endpoint.paired)
        self.assertTrue(endpoint.enabled)

    def test_rejects_foreign_channel_kind(self) -> None:
        record = _record(channel_kind="socket")
        context = _context(self.provider, self.repository, self.runtime_root)
        self.assertIsNone(self.provider.create_endpoint(record, context))


# ---------------------------------------------------------------------------
# Dedicated ingress
# ---------------------------------------------------------------------------


class DedicatedIngressTests(unittest.TestCase):
    def test_normalize_raw_appends_receiver_owned_peer_identity(self) -> None:
        endpoint = WebSocketBridgeEndpoint(endpoint=_endpoint_config())
        normalized = endpoint.normalize_raw({"text": "  hello  ", "from": "spoofed"})
        self.assertEqual(normalized["text"], "  hello  \n\n--wb_main")
        self.assertNotIn("from", normalized)


# ---------------------------------------------------------------------------
# Provider lifecycle + introspection
# ---------------------------------------------------------------------------


class ProviderLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_wsb_life_"))
        self.provider = WebSocketBridgeProvider()
        self.repository = _FakeRepository()
        self.record = _record()
        self.repository.rows[self.record.endpoint_id] = self.record
        self.context = _context(self.provider, self.repository, self.runtime_root)

    def test_attach_missing_row_returns_not_found(self) -> None:
        result = self.provider.attach_endpoint("does_not_exist", self.context)
        self.assertEqual(result.status, RuntimeStatus.NOT_FOUND)

    def test_attach_registers_runtime_endpoint(self) -> None:
        result = self.provider.attach_endpoint("wb_main", self.context)
        self.assertEqual(result.status, RuntimeStatus.OK)
        self.assertIn("reload_modules", result.structured or {})
        endpoint = self.context.runtime.get_endpoint("wb_main")
        self.assertIsNotNone(endpoint)
        assert endpoint is not None
        self.assertTrue(endpoint.attached)

    def test_detach_is_reversible(self) -> None:
        self.provider.attach_endpoint("wb_main", self.context)
        self.assertIsNotNone(self.context.runtime.get_endpoint("wb_main"))

        detach = self.provider.detach_endpoint("wb_main", self.context)
        self.assertEqual(detach.status, RuntimeStatus.OK)
        self.assertIsNone(self.context.runtime.get_endpoint("wb_main"))

        # Re-attach proves the lifecycle is reversible (not a one-way transition).
        reattach = self.provider.attach_endpoint("wb_main", self.context)
        self.assertEqual(reattach.status, RuntimeStatus.OK)
        self.assertIsNotNone(self.context.runtime.get_endpoint("wb_main"))

    def test_detach_missing_row_and_runtime_returns_not_found(self) -> None:
        result = self.provider.detach_endpoint("missing", self.context)
        self.assertEqual(result.status, RuntimeStatus.NOT_FOUND)

    def test_restart_recreates_runtime_endpoint(self) -> None:
        self.provider.attach_endpoint("wb_main", self.context)
        first = self.context.runtime.get_endpoint("wb_main")
        self.assertIsNotNone(first)

        result = self.provider.restart_endpoint("wb_main", self.context)
        self.assertEqual(result.status, RuntimeStatus.OK)
        second = self.context.runtime.get_endpoint("wb_main")
        self.assertIsNotNone(second)
        assert second is not None
        self.assertIsNot(second, first)

    def test_inspect_methods_surface_provider_id_without_secrets(self) -> None:
        self.provider.attach_endpoint("wb_main", self.context)

        snapshot = self.provider.inspect_endpoint("wb_main", self.context)
        self.assertEqual(snapshot.status, RuntimeStatus.OK)
        assert snapshot.structured is not None
        self.assertEqual(snapshot.structured["provider_id"], PROVIDER_ID)
        self.assertEqual(snapshot.structured["endpoint_type"], ENDPOINT_TYPE)

        auth = self.provider.inspect_auth_state("wb_main", self.context)
        self.assertEqual(auth.status, RuntimeStatus.OK)
        assert auth.structured is not None
        self.assertEqual(auth.structured["provider_id"], PROVIDER_ID)
        for secret in ("token", "secret", "bot_token", "password"):
            self.assertNotIn(secret, auth.structured)

        backlog = self.provider.inspect_backlog("wb_main", self.context)
        self.assertEqual(backlog.status, RuntimeStatus.OK)

        health = self.provider.inspect_health("wb_main", self.context)
        self.assertEqual(health.status, RuntimeStatus.OK)
        assert health.structured is not None
        # Sidecar never started in this (not-started runtime) context.
        self.assertFalse(health.structured.get("healthy"))
        self.assertEqual(health.structured["provider_id"], PROVIDER_ID)

    def test_inspect_missing_endpoint_returns_not_found(self) -> None:
        for method in (
            self.provider.inspect_endpoint,
            self.provider.inspect_auth_state,
            self.provider.inspect_backlog,
            self.provider.inspect_health,
        ):
            with self.subTest(method=method.__name__):
                result = method("missing", self.context)
                self.assertEqual(result.status, RuntimeStatus.NOT_FOUND)


# ---------------------------------------------------------------------------
# Sidecar config + command shape (no subprocess)
# ---------------------------------------------------------------------------


class SidecarConfigShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_wsb_cfg_"))

    def test_default_command_runs_declared_serve_entrypoint(self) -> None:
        endpoint = _make_endpoint(self.runtime_root)
        command = endpoint._sidecar_command()
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1], "-c")
        self.assertIn("configure_process_logging", command[2])
        self.assertIn("from sidecar import serve, SidecarConfig", command[2])
        self.assertIn("asyncio.run(serve(", command[2])

    @patch("websocket_bridge.runtime.subprocess.Popen")
    def test_spawn_inherits_stdout_and_stderr_for_service_logging(self, popen: Any) -> None:
        endpoint = _make_endpoint(self.runtime_root)
        endpoint._spawn_sidecar()

        kwargs = popen.call_args.kwargs
        self.assertNotIn("stdout", kwargs)
        self.assertNotIn("stderr", kwargs)

    def test_config_payload_carries_runtime_root_and_binding(self) -> None:
        endpoint = _make_endpoint(
            self.runtime_root,
            binding_metadata={"bind_host": "0.0.0.0", "bind_port": 8765, "peer_url": "ws://peer:8765"},
        )
        payload = endpoint._sidecar_config_payload()
        self.assertEqual(payload["runtime_root"], str(self.runtime_root))
        self.assertEqual(
            payload["bridge_socket_path"],
            str(self.runtime_root / "data" / "channel" / "wb_main" / "channel.sock"),
        )
        self.assertNotIn("socket_channel_path", payload)
        self.assertEqual(payload["bind_host"], "0.0.0.0")
        self.assertEqual(payload["bind_port"], 8765)
        self.assertEqual(payload["peer_url"], "ws://peer:8765")
        self.assertIn("reconnect_initial_delay_seconds", payload)
        self.assertIn("reconnect_max_delay_seconds", payload)
        self.assertNotIn("message_timeout_seconds", payload)

    def test_manager_endpoint_uses_repo_sidecar_convention(self) -> None:
        endpoint = _make_endpoint(self.runtime_root)
        manager = endpoint._manager_endpoint()
        self.assertEqual(
            manager.runtime_dir,
            self.runtime_root / "data" / "channel" / "wb_main",
        )
        self.assertEqual(manager.socket_path, manager.runtime_dir / "manager.sock")


# ---------------------------------------------------------------------------
# Sidecar subprocess ownership (async)
# ---------------------------------------------------------------------------


class SidecarLifecycleAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_async_without_runtime_root_records_error(self) -> None:
        endpoint = WebSocketBridgeEndpoint(endpoint=_endpoint_config())
        endpoint.runtime_root = None
        endpoint.socket_path = None
        await endpoint.start_async()
        self.assertIsNone(endpoint._process)
        self.assertIn("missing", endpoint._startup_error)

    async def test_start_async_spawns_and_stop_async_terminates(self) -> None:
        runtime_root = Path(tempfile.mkdtemp(prefix="pw_spawn_", dir="/tmp"))
        endpoint = _make_endpoint(runtime_root)
        endpoint.startup_probe_seconds = 0.4
        # A long-lived placeholder process stands in for the sidecar.
        endpoint._sidecar_command_override = (
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        )

        await endpoint.start_async()
        self.assertIsNotNone(endpoint._process)
        process = endpoint._process
        assert process is not None
        self.assertIsNone(process.poll())  # running

        health = endpoint.inspect_health()
        self.assertTrue(health["process_running"])
        # No manager socket -> listener not bound -> not healthy (correctly reported).
        self.assertFalse(health["healthy"])
        with self.assertRaises(ChannelDeliveryError):
            endpoint.validate_replacement_startup()

        await endpoint.stop_async()
        self.assertIsNone(endpoint._process)
        # The process must have been terminated by the provider.
        self.assertIsNotNone(process.poll())

    async def test_health_reflects_dead_sidecar(self) -> None:
        runtime_root = Path(tempfile.mkdtemp(prefix="pw_dead_", dir="/tmp"))
        endpoint = _make_endpoint(runtime_root)
        endpoint.startup_probe_seconds = 0.4
        endpoint._sidecar_command_override = (
            sys.executable,
            "-c",
            "import sys; sys.exit(2)",
        )

        await endpoint.start_async()
        health = endpoint.inspect_health()
        self.assertFalse(health["process_running"])
        self.assertFalse(health["healthy"])
        self.assertTrue(health["last_error"])

    async def test_health_reflects_live_sidecar_via_manager_socket(self) -> None:
        runtime_root = Path(tempfile.mkdtemp(prefix="pw_live_", dir="/tmp"))
        endpoint = _make_endpoint(runtime_root)
        endpoint._sidecar_command_override = (sys.executable, "-c", _FAKE_SIDECAR_BOOTSTRAP)

        await endpoint.start_async()
        self.assertEqual(endpoint._startup_error, "")
        endpoint.validate_replacement_startup()
        health = endpoint.inspect_health()
        self.assertTrue(health["process_running"])
        self.assertTrue(health["listener_bound"])
        self.assertEqual(health["connected_peers"], 1)
        self.assertTrue(health["healthy"])

        await endpoint.stop_async()
        self.assertIsNone(endpoint._process)


if __name__ == "__main__":
    unittest.main()
