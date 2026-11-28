from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from pal.cache_reminder.presentation import cache_policy_panel_delivery
from pal.cache_reminder.settings import CACHE_POLICY_MODE_KEY
from pal.control import ControlPlane, register_with_core as register_control
from pal.control.contracts import ControlEvent, ControlRoute, InteractionResult
from pal.shared import EventKind
from tests.cache_reminder_factory import _Settings, attach_cache_reminder
from tests.core_factory import build_core


def test_policy_menu_opens_switches_in_place_and_returns_to_control_panel(monkeypatch):
    core = build_core()
    plane = ControlPlane()
    register_control(core.context, plane)
    settings = _Settings()
    manager = attach_cache_reminder(core, settings)
    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr(core, "_deliver_control_delivery_async", delivered)
    route = ControlRoute("telegram_main", "telegram", {"chat_id": "42"}, "telegram:42")

    async def slash(text):
        action = plane.parse_event(ControlEvent(EventKind.SLASH_COMMAND, "channel", {"text": text}, route=route))
        await core.handle_control_action_async(action)
        return delivered.await_args.args[0]

    async def click(spec, row):
        button = spec.buttons[row][0]
        action = plane.handle_interaction(InteractionResult(spec.interaction_id, spec.interaction_kind,
            button.action_key, button.action_args, route))
        await core.handle_control_action_async(action)
        return delivered.await_args.args[0]

    async def scenario():
        first = await slash("/cache_policy")
        assert first.delivery_kind == "interactive_update"
        spec = first.interaction
        assert spec is not None
        assert spec.interaction_kind == "control_panel"
        assert [row[0].label for row in spec.buttons] == ["> reminder", "keep-alive", "Back"]
        assert "No paid heartbeats" in spec.text
        assert "automatic Compact" in spec.text
        assert "without changing L1 or thinking effort" in spec.text
        assert settings.get(CACHE_POLICY_MODE_KEY) is None  # opening is read-only

        switched = await click(spec, 1)
        assert switched.delivery_kind == "interactive_update"
        second = switched.interaction
        assert second is not None and second.interaction_id == spec.interaction_id
        assert [row[0].label for row in second.buttons] == ["reminder", "> keep-alive", "Back"]
        assert settings.get(CACHE_POLICY_MODE_KEY) == "keep-alive"
        assert manager.inspect()["mode"] == "keep-alive"
        assert "Estimated cost:" in second.text

        back = await click(second, 2)
        assert back.interaction.interaction_id == spec.interaction_id
        assert any(button.label == "Cache Policy" for row in back.interaction.buttons for button in row)

        direct = await slash("/cache_policy reminder")
        assert direct.interaction.interaction_id == spec.interaction_id
        assert direct.interaction.buttons[0][0].label == "> reminder"
        assert manager.inspect()["mode"] == "reminder"
        assert not core.state.active_turns

    try:
        asyncio.run(scenario())
    finally:
        core.close()


def test_policy_menu_text_fallback_reports_disabled_state():
    state = {"mode": "keep-alive", "enabled": False,
             "keepalive": {"renewals": 2, "max_actions": 19, "estimated_cost": .0125, "reason": "keepalive"},
             "seconds_until_notice": 45}
    delivery = cache_policy_panel_delivery(None, state)
    assert delivery.delivery_kind == "reply"
    from telegramify_markdown import convert
    plain, _ = convert(delivery.text)
    assert "reminder\n> keep-alive" in plain
    assert "Both policies are currently disabled" in delivery.text
    assert "Renewals: 2; action limit: 19" in delivery.text
    assert "$0.012500" in delivery.text
    assert "Next action in 45s" in delivery.text


def test_model_panel_markdown_preserves_literal_ids_and_groups_providers():
    from types import SimpleNamespace
    from telegramify_markdown import convert
    from pal.llm.control_presentation import build_model_panel_interaction

    endpoints = [
        SimpleNamespace(provider=provider, model_id=model, endpoint_id=f"{provider}-{model}",
                        display_name=f"{provider} {model} [custom]", wire_shape="openai_response",
                        context_window=128000, max_output_tokens=32000)
        for provider, model in (("openrouter", "openai/gpt-6.2-sol"),
                                ("openai", "gpt-6.2-sol"), ("openrouter", "model_v2"))
    ]
    active = endpoints[0].endpoint_id
    route = ControlRoute("telegram", "telegram", {"chat_id": "42"})
    spec = build_model_panel_interaction(route, endpoints, active)
    plain, _ = convert(spec.text)
    assert "Active: " + active in plain
    assert spec.text.count("**openrouter**") == 1
    assert "**openai**" in spec.text
    for endpoint in endpoints:
        assert f"{endpoint.display_name} ({endpoint.endpoint_id})" in plain
    assert [row[0].action_args["endpoint_id"] for row in spec.buttons[:-1]] == [
        endpoint.endpoint_id for endpoint in endpoints]
