"""Adversarial verification tests for the Pal-to-Pal LAN WebSocket bridge provider.

These extend the developer corpus for demonstrated gaps in the provider-owned
auth/lifecycle/ownership protocol:

* ``set_auth_material`` is exercised (apply, persist, sanitize, accepted_keys,
  and the not-found path) -- uncovered by the developer corpus.
* restart preserves provider-owned pairing state across endpoint recreation
  (``_preserve_state``), proving the lifecycle is reversible without loss.
* ``create_endpoint`` propagates ``enabled``/``attached`` truthfully from the
  ``channel_endpoints`` row, including a disabled/detached row.
* The no-secret invariant holds across EVERY introspection surface when the
  binding metadata carries secret-like keys -- the developer corpus only
  spot-checks one auth path.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pal.channel.contracts import EndpointConfig
from pal.channel.provider_manager import ChannelProviderContext
from pal.channel.runtime import ChannelRuntime
from pal.shared import RuntimeStatus

from websocket_bridge.runtime import (
    ENDPOINT_TYPE,
    PROVIDER_ID,
    WebSocketBridgeEndpoint,
    WebSocketBridgeProvider,
)


# ---------------------------------------------------------------------------
# Test fakes (mirror developer corpus shape; no DB binding, no subprocess)
# ---------------------------------------------------------------------------


class _FakeRepository:
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
    *,
    channel_kind: str = ENDPOINT_TYPE,
    enabled: bool = True,
    attached: bool = True,
    binding_metadata: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        endpoint_id=endpoint_id,
        channel_kind=channel_kind,
        binding_key="lan:peer",
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
    return ChannelProviderContext(
        runtime=ChannelRuntime(),
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


# ---------------------------------------------------------------------------
# set_auth_material (provider-owned auth application + persistence)
# ---------------------------------------------------------------------------


class SetAuthMaterialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_wsb_auth_"))
        self.provider = WebSocketBridgeProvider()
        self.repository = _FakeRepository()
        self.record = _record()
        self.repository.rows[self.record.endpoint_id] = self.record
        self.context = _context(self.provider, self.repository, self.runtime_root)
        # Attach so a runtime endpoint exists for set_auth_material to target.
        self.provider.attach_endpoint("wb_main", self.context)

    def test_applies_material_pairs_and_surfaces_accepted_keys(self) -> None:
        result = self.provider.set_auth_material(
            "wb_main", {"token": "supersecret", "peer_label": "my-peer"}, self.context
        )
        self.assertEqual(result.status, RuntimeStatus.OK)
        assert result.structured is not None
        # accepted_keys reflects the submitted material keys, sorted.
        self.assertEqual(result.structured["accepted_keys"], ["peer_label", "token"])
        # apply_auth_material -> pair() sets paired True -> authorized True.
        self.assertTrue(result.structured["paired"])
        self.assertTrue(result.structured["authorized"])
        self.assertEqual(result.structured["provider_id"], PROVIDER_ID)

    def test_never_leaks_secret_values(self) -> None:
        result = self.provider.set_auth_material(
            "wb_main",
            {"token": "leak-me", "secret": "leak-me-too", "password": "pw", "bot_token": "bt"},
            self.context,
        )
        assert result.structured is not None
        rendered = repr(result.structured)
        for secret_value in ("leak-me", "leak-me-too", "pw", "bt"):
            self.assertNotIn(secret_value, rendered)
        for secret_key in ("token", "secret", "bot_token", "password"):
            self.assertNotIn(secret_key, result.structured)

    def test_persists_auth_keys_to_repository(self) -> None:
        self.provider.set_auth_material(
            "wb_main", {"alpha": "1", "beta": "2"}, self.context
        )
        record = self.repository.get("wb_main")
        assert record is not None
        merged = dict(record.binding_metadata or {})
        self.assertIn("auth_keys", merged)
        self.assertEqual(merged["auth_keys"], ["alpha", "beta"])
        self.assertTrue(merged["paired"])

    def test_missing_runtime_endpoint_returns_not_found(self) -> None:
        # No runtime endpoint registered for this id.
        result = self.provider.set_auth_material(
            "does_not_exist", {"token": "x"}, self.context
        )
        self.assertEqual(result.status, RuntimeStatus.NOT_FOUND)


# ---------------------------------------------------------------------------
# restart preserves provider-owned pairing state across recreation
# ---------------------------------------------------------------------------


class RestartStatePreservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_wsb_restart_"))
        self.provider = WebSocketBridgeProvider()
        self.repository = _FakeRepository()
        self.record = _record()
        self.repository.rows[self.record.endpoint_id] = self.record
        self.context = _context(self.provider, self.repository, self.runtime_root)

    def test_restart_preserves_pairing_metadata(self) -> None:
        self.provider.attach_endpoint("wb_main", self.context)
        endpoint = self.context.runtime.get_endpoint("wb_main")
        assert endpoint is not None
        # Simulate auth application that wrote pairing metadata onto the endpoint.
        endpoint.pairing_metadata = {"auth_nonce": "abc-123", "peer_fingerprint": "fp"}

        result = self.provider.restart_endpoint("wb_main", self.context)
        self.assertEqual(result.status, RuntimeStatus.OK)

        refreshed = self.context.runtime.get_endpoint("wb_main")
        assert refreshed is not None
        # A brand-new instance would have empty pairing_metadata; preservation
        # must copy it forward so the lifecycle is reversible without loss.
        self.assertEqual(refreshed.pairing_metadata.get("auth_nonce"), "abc-123")
        self.assertEqual(refreshed.pairing_metadata.get("peer_fingerprint"), "fp")
        self.assertTrue(refreshed.paired)

    def test_restart_creates_new_instance(self) -> None:
        self.provider.attach_endpoint("wb_main", self.context)
        first = self.context.runtime.get_endpoint("wb_main")
        assert first is not None
        self.provider.restart_endpoint("wb_main", self.context)
        second = self.context.runtime.get_endpoint("wb_main")
        assert second is not None
        self.assertIsNot(first, second)


# ---------------------------------------------------------------------------
# create_endpoint truthfully propagates row state
# ---------------------------------------------------------------------------


class CreateEndpointRowStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_wsb_row_"))
        self.provider = WebSocketBridgeProvider()
        self.repository = _FakeRepository()

    def test_disabled_detached_row_propagates_state(self) -> None:
        record = _record(enabled=False, attached=False)
        context = _context(self.provider, self.repository, self.runtime_root)
        endpoint = self.provider.create_endpoint(record, context)
        assert isinstance(endpoint, WebSocketBridgeEndpoint)
        self.assertFalse(endpoint.enabled)
        self.assertFalse(endpoint.attached)
        # create_endpoint always pairs the bridge endpoint (trusted-LAN peer).
        self.assertTrue(endpoint.paired)
        self.assertEqual(endpoint.runtime_root, self.runtime_root)

    def test_whitespace_channel_kind_is_normalized_accepted(self) -> None:
        # create_endpoint strips the channel_kind, so padded whitespace is
        # normalized to ENDPOINT_TYPE and accepted (defensive normalization).
        record = _record(channel_kind="  websocket_bridge  ")
        context = _context(self.provider, self.repository, self.runtime_root)
        endpoint = self.provider.create_endpoint(record, context)
        self.assertIsInstance(endpoint, WebSocketBridgeEndpoint)
        # A genuinely different kind is still rejected.
        self.assertIsNone(self.provider.create_endpoint(_record(channel_kind="telegram"), context))


# ---------------------------------------------------------------------------
# No-secret invariant across every introspection surface
# ---------------------------------------------------------------------------


class NoSecretLeakAcrossIntrospectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_wsb_nosec_"))
        self.provider = WebSocketBridgeProvider()
        self.repository = _FakeRepository()
        # Seed binding metadata with secret-like keys that must never surface.
        self.record = _record(
            binding_metadata={
                "bind_host": "0.0.0.0",
                "bind_port": 8765,
                "peer_url": "ws://peer:8765",
                "token": "must-not-leak",
                "secret": "must-not-leak",
                "password": "must-not-leak",
                "bot_token": "must-not-leak",
            }
        )
        self.repository.rows[self.record.endpoint_id] = self.record
        self.context = _context(self.provider, self.repository, self.runtime_root)
        self.provider.attach_endpoint("wb_main", self.context)

    def _leaked(self) -> set[str]:
        leaked: set[str] = set()
        for method in (
            self.provider.inspect_endpoint,
            self.provider.inspect_auth_state,
            self.provider.inspect_backlog,
            self.provider.inspect_health,
        ):
            result = method("wb_main", self.context)
            assert result.structured is not None
            for value in repr(result.structured), getattr(result, "llm_text", ""):
                if "must-not-leak" in value:
                    leaked.add(method.__name__)
        return leaked

    def test_no_introspection_surface_leaks_secret_values(self) -> None:
        self.assertEqual(self._leaked(), set())


if __name__ == "__main__":
    unittest.main()
