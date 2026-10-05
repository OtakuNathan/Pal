"""Legacy settings and request metadata cannot enable endpoint fallback."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from pal.control.service import ControlPlane
from pal.llm import EndpointResolver, LLMRuntime
from pal.llm.repository import LLM_ENDPOINT_FALLBACK_SETTING_KEY


def _fake_endpoint(endpoint_id: str):
    return SimpleNamespace(
        endpoint_id=endpoint_id,
        model_id=f"{endpoint_id}-model",
        provider="openai",
        display_name=f"{endpoint_id}-model",
        wire_shape="openai_completion",
        base_url="https://example.test/v1",
        auth_kind="api_key_ref",
        credential_ref=f"{endpoint_id.upper()}_API_KEY",
        capabilities_blob={},
        thinking_levels_blob=["off"],
        default_thinking_level="off",
        supports_tools=True,
        supports_streaming=False,
        supports_vision=False,
        max_output_tokens=1024,
        context_window=8192,
        input_modalities_blob=[],
        output_modalities_blob=[],
        priority=0,
        enabled=True,
        notes=None,
    )


class _SwitchSettingsRepository:
    def __init__(self, fallback: bool | None = None) -> None:
        self.values: dict[str, str | None] = {}
        if fallback is not None:
            self.values[LLM_ENDPOINT_FALLBACK_SETTING_KEY] = "on" if fallback else "off"

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def get_active_llm_endpoint_id(self) -> str | None:
        return self.values.get("active")

    def set_active_llm_endpoint_id(self, endpoint_id: str) -> None:
        self.values["active"] = endpoint_id

    def get_think_level(self, endpoint_id: str) -> str | None:
        return None

    def set_think_level(self, endpoint_id: str, value: str) -> None:
        _ = endpoint_id, value


def _runtime(repository: _SwitchSettingsRepository, active: str | None = None) -> LLMRuntime:
    repository.values["active"] = active
    return LLMRuntime(
        endpoint_resolver=EndpointResolver(
            endpoints=(
                _fake_endpoint("alpha"),
                _fake_endpoint("beta"),
            )
        ),
        settings_repository=repository,
        endpoint_invoker=object(),
        active_endpoint_id=active,
    )


class EndpointFallbackRemovalTests(unittest.TestCase):
    def test_legacy_settings_and_request_overrides_cannot_enable_fallback(self) -> None:
        for setting in (None, False, True):
            for policy in (None, "none", "enabled", "on", "anything"):
                with self.subTest(setting=setting, policy=policy):
                    runtime = _runtime(_SwitchSettingsRepository(setting), active="alpha")
                    endpoints = runtime._enabled_endpoints_for_preference(endpoint_fallback_policy=policy)
                    self.assertEqual([item.endpoint_id for item in endpoints], ["alpha"])

    def test_missing_selection_does_not_choose_the_first_endpoint(self) -> None:
        for active in (None, "deleted"):
            runtime = _runtime(_SwitchSettingsRepository(True), active=active)
            self.assertIsNone(runtime.active_endpoint())
            self.assertEqual(runtime._enabled_endpoints_for_preference(), [])
            self.assertEqual(runtime._enabled_endpoints_for_preference(preferred_endpoint_id="deleted"), [])

    def test_explicit_request_target_is_the_only_endpoint(self) -> None:
        runtime = _runtime(_SwitchSettingsRepository(True), active="alpha")
        endpoints = runtime._enabled_endpoints_for_preference(
            preferred_endpoint_id="beta", endpoint_fallback_policy="enabled",
        )
        self.assertEqual([item.endpoint_id for item in endpoints], ["beta"])
        self.assertEqual(runtime.active_endpoint_id, "alpha")

    def test_fallback_setter_and_commands_are_removed(self) -> None:
        runtime = _runtime(_SwitchSettingsRepository(), active="alpha")
        self.assertFalse(hasattr(runtime, "set_llm_endpoint_fallback"))
        plane = ControlPlane()
        names = {item.name for item in plane.list_panel_commands()}
        self.assertNotIn("llm_fallback", names)
        self.assertNotIn("fallback", names)


if __name__ == "__main__":
    unittest.main()
