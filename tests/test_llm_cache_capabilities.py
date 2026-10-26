from dataclasses import replace

import pytest

from pal.llm.cache_policy import CacheProfileError, PromptCacheDialect, available_modes, resolve_dialect
from pal.llm.prompt_cache import PromptCacheCoordinator
from pal.llm.ir import WireShape
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from tests.test_prompt_cache_policy import _request


def context(options=None, **kwargs):
    return ShapeContext(WireShape.OPENAI_COMPLETION, "ep", "custom-model", provider_id="deepseek",
                        capabilities={"prompt_cache": options or {}}, **kwargs)


def test_unknown_endpoint_uses_general_without_cache_controls():
    ctx = context()
    assert ctx.cache_capabilities.protocol == "general"
    assert available_modes(ctx) == ("implicit",)
    coordinator = PromptCacheCoordinator()
    request = _request()
    raw = codec_for_shape(ctx.wire_shape).encode(request, ctx)
    plan = coordinator.plan(request, ctx, raw)
    wire = coordinator.inject(raw, plan)
    assert plan.mode == "implicit"
    assert plan.dialect == PromptCacheDialect.NONE
    assert dict(wire.payload) == dict(raw.payload)
    assert dict(wire.extra_body) == dict(raw.extra_body)


@pytest.mark.parametrize("provider,model,url", [
    ("deepseek", "custom-model", "https://example.test"),
    ("proxy", "alias", "https://other.test"),
])
def test_explicit_capability_is_independent_of_provider_model_and_url(provider, model, url):
    ctx = replace(context({"protocol": "openai_chat_explicit", "mode": "explicit"}),
                  provider_id=provider, model_id=model, base_url=url)
    assert resolve_dialect(ctx) == PromptCacheDialect.OPENAI_CHAT_EXPLICIT
    assert "explicit" in available_modes(ctx)
    coordinator = PromptCacheCoordinator()
    request = _request()
    raw = codec_for_shape(ctx.wire_shape).encode(request, ctx)
    plan = coordinator.plan(request, ctx, raw)
    wire = coordinator.inject(raw, plan)
    assert plan.dialect == PromptCacheDialect.OPENAI_CHAT_EXPLICIT
    assert wire.applied_cache_breakpoint_message_ids


def test_general_overrides_known_provider_presets():
    ctx = replace(context({"protocol": "general"}), provider_id="openai", model_id="gpt-6-astra")
    assert available_modes(ctx) == ("implicit",)
    assert resolve_dialect(ctx) == PromptCacheDialect.NONE
    with pytest.raises(CacheProfileError, match="unavailable"):
        resolve_dialect(replace(ctx, capabilities={"prompt_cache": {"protocol": "general", "mode": "explicit"}}))


@pytest.mark.parametrize("options", [
    {"protocol": "invented"}, {"protocol": "anthropic_explicit"},
    {"reminder_ttl_seconds": 0}, {"reminder_ttl_seconds": -1},
    {"reminder_ttl_seconds": True}, {"reminder_ttl_seconds": "900"},
])
def test_invalid_capabilities_fail_before_invocation(options):
    with pytest.raises(CacheProfileError):
        context(options)


def test_general_reminder_lifetime_is_configurable_and_never_cache_evidence():
    ctx = context({"protocol": "general", "reminder_ttl_seconds": 900})
    coordinator = PromptCacheCoordinator()
    plan = coordinator.plan(_request(), ctx)
    assert plan.reminder.ttl_seconds == 900
    assert plan.reminder.ttl_source == "endpoint_policy"
    assert not coordinator.confirmed_anchor_request()


def test_endpoint_reminder_cannot_outlive_explicit_wire_ttl():
    from pal.llm.cache_reminder import reminder_ttl

    assert reminder_ttl("deepseek", "custom", ttl_seconds=900, requested_ttls=("5m",)) == (300, "request_cache_ttl")
    assert reminder_ttl("deepseek", "custom", ttl_seconds=120, requested_ttls=("5m",)) == (120, "endpoint_policy")
