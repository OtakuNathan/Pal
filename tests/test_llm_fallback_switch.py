"""Endpoint fallback switch: default-off, explicit policy precedence, command registration."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from pal.control.contracts import ControlCommandInvocation
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
        return None

    def set_active_llm_endpoint_id(self, endpoint_id: str) -> None:
        self.values["active"] = endpoint_id

    def get_think_level(self, endpoint_id: str) -> str | None:
        return None

    def set_think_level(self, endpoint_id: str, value: str) -> None:
        _ = endpoint_id, value

    def get_llm_endpoint_fallback(self) -> bool:
        value = str(self.values.get(LLM_ENDPOINT_FALLBACK_SETTING_KEY) or "").strip().lower()
        return value in {"on", "true", "1", "enabled", "yes"}

    def set_llm_endpoint_fallback(self, enabled: bool) -> None:
        self.values[LLM_ENDPOINT_FALLBACK_SETTING_KEY] = "on" if enabled else "off"


def _runtime(repository: _SwitchSettingsRepository, active: str | None = None) -> LLMRuntime:
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


class EndpointFallbackSwitchTests(unittest.TestCase):
    def test_default_off_restricts_to_active_endpoint(self) -> None:
        runtime = _runtime(_SwitchSettingsRepository(fallback=None), active="alpha")
        endpoints = runtime._enabled_endpoints_for_preference()
        self.assertEqual([item.endpoint_id for item in endpoints], ["alpha"])

    def test_switch_on_restores_full_fallback_order(self) -> None:
        runtime = _runtime(_SwitchSettingsRepository(fallback=True), active="alpha")
        endpoints = runtime._enabled_endpoints_for_preference()
        self.assertEqual(
            [item.endpoint_id for item in endpoints],
            ["alpha", "beta"],
        )

    def test_explicit_policy_beats_global_switch_in_both_directions(self) -> None:
        off_runtime = _runtime(_SwitchSettingsRepository(fallback=True))
        endpoints = off_runtime._enabled_endpoints_for_preference(
            endpoint_fallback_policy="none"
        )
        # Explicit "none" wins even though the global switch is on.
        self.assertEqual(len(endpoints), 1)

    def test_runtime_setter_flips_live_behavior(self) -> None:
        repository = _SwitchSettingsRepository(fallback=None)
        runtime = _runtime(repository, active="alpha")
        self.assertFalse(runtime.llm_endpoint_fallback_enabled())
        runtime.set_llm_endpoint_fallback(True)
        self.assertTrue(runtime.llm_endpoint_fallback_enabled())
        self.assertEqual(
            [item.endpoint_id for item in runtime._enabled_endpoints_for_preference()],
            ["alpha", "beta"],
        )
        runtime.set_llm_endpoint_fallback(False)
        self.assertEqual(
            [item.endpoint_id for item in runtime._enabled_endpoints_for_preference()],
            ["alpha"],
        )

    def test_command_registered_with_on_off_args(self) -> None:
        plane = ControlPlane()
        invocation = ControlCommandInvocation(
            command_name="llm_fallback",
            argv=["on"],
            route=None,
        )
        action = plane._handle_llm_fallback(invocation)  # noqa: SLF001 - direct handler test
        self.assertEqual(action.action_kind, "set_llm_fallback")
        self.assertTrue(action.args["enabled"])

        status = plane._handle_llm_fallback(
            ControlCommandInvocation(command_name="llm_fallback", argv=[], route=None)
        )
        self.assertEqual(status.action_kind, "show_llm_fallback")

        invalid = plane._handle_llm_fallback(
            ControlCommandInvocation(command_name="llm_fallback", argv=["maybe"], route=None)
        )
        self.assertEqual(invalid.action_kind, "invalid_command")


if __name__ == "__main__":
    unittest.main()
