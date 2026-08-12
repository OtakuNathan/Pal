from __future__ import annotations

from dataclasses import replace

from pal.llm.ir import (
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    LLMUsageIR,
    MessageRole,
    PromptRegionIR,
    TextPartIR,
    WireShape,
)
from pal.llm.prompt_cache import PromptCacheCoordinator, PromptCacheDialect
from pal.llm.shapes.base import ShapeContext
from pal.llm.shapes.anthropic_messages import AnthropicMessagesCodec
from pal.llm.shapes.openai_response import OpenAIResponseCodec
from pal.llm.shapes.openai_completion import OpenAICompletionCodec
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolDefinitionIR


def _request() -> LLMRequestIR:
    return LLMRequestIR(
        messages=(
            LLMMessageIR(
                MessageRole.SYSTEM,
                (TextPartIR("stable " * 900),),
                prompt_region=PromptRegionIR.STABLE_SYSTEM,
            ),
            LLMMessageIR(
                MessageRole.USER,
                (TextPartIR("history " * 900),),
                prompt_region=PromptRegionIR.SETTLED_HISTORY,
            ),
            LLMMessageIR(
                MessageRole.USER,
                (TextPartIR("current " * 900),),
                prompt_region=PromptRegionIR.ACTIVE_INPUT,
            ),
            LLMMessageIR(
                MessageRole.USER,
                (TextPartIR("dynamic reminder"),),
                prompt_region=PromptRegionIR.ACTIVE_DYNAMIC,
            ),
        ),
        tools=(),
        policy=GenerationPolicyIR(max_output_tokens=128),
        logical_scope_id="pal:resident",
    )


def test_openai_responses_explicit_cache_is_injected_centrally() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openai",
        model_id="gpt-5.6-sol",
        provider_id="OpenAI",
        base_url="https://api.openai.com/v1",
    )
    coordinator = PromptCacheCoordinator()

    plan = coordinator.plan(request, context)
    encoded = coordinator.inject(OpenAIResponseCodec().encode(request, context), plan)
    payload = thaw_json(encoded.payload)

    assert plan.dialect == PromptCacheDialect.OPENAI_RESPONSES_EXPLICIT
    assert [item.label for item in plan.breakpoints] == ["stable", "settled", "active"]
    assert encoded.extra_body["prompt_cache_key"].startswith("pal-")
    assert encoded.extra_body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    marked = [
        block
        for item in payload["input"]
        for block in item.get("content", [])
        if isinstance(block, dict) and "prompt_cache_breakpoint" in block
    ]
    assert len(marked) == 3


def test_openrouter_uses_automatic_key_without_openai_explicit_fields() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openrouter",
        model_id="openai/gpt-5.6-sol",
        provider_id="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
    )
    coordinator = PromptCacheCoordinator()

    plan = coordinator.plan(request, context)
    encoded = coordinator.inject(OpenAIResponseCodec().encode(request, context), plan)

    assert plan.dialect == PromptCacheDialect.OPENROUTER_AUTOMATIC
    assert plan.decision == "provider_automatic"
    assert plan.breakpoints == ()
    assert "session_id" in encoded.extra_body
    assert "prompt_cache_key" not in encoded.extra_body
    assert "prompt_cache_options" not in encoded.extra_body


def test_openrouter_automatic_cache_does_not_report_inert_breakpoints() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openrouter",
        model_id="openai/gpt-5.6-sol",
        provider_id="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
    )
    coordinator = PromptCacheCoordinator()

    first = coordinator.plan(request, context)
    coordinator.observe(
        first,
        LLMUsageIR(
            input_tokens=10_000,
            cache_write_input_tokens=10_000,
            reported=True,
        ),
    )
    second = coordinator.plan(request, context)

    assert second.decision == "provider_automatic"
    assert second.breakpoints == ()


def test_openrouter_anthropic_messages_keeps_sticky_session_and_breakpoints() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.ANTHROPIC_MESSAGES,
        endpoint_id="openrouter-anthropic",
        model_id="anthropic/claude-sonnet-4",
        provider_id="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
    )
    coordinator = PromptCacheCoordinator()

    plan = coordinator.plan(request, context)
    encoded = coordinator.inject(AnthropicMessagesCodec().encode(request, context), plan)
    payload = thaw_json(encoded.payload)

    assert plan.dialect == PromptCacheDialect.OPENROUTER_ANTHROPIC_EXPLICIT
    assert encoded.extra_body["session_id"].startswith("pal-")
    assert payload["system"][-1]["cache_control"]["ttl"] == "1h"
    assert sum(
        "cache_control" in block
        for message in payload["messages"]
        for block in message["content"]
    ) == 2


def test_openrouter_anthropic_alias_is_classified_by_wire_shape() -> None:
    context = ShapeContext(
        wire_shape=WireShape.ANTHROPIC_MESSAGES,
        endpoint_id="openrouter-anthropic-alias",
        model_id="claude-company-alias",
        provider_id="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
    )

    plan = PromptCacheCoordinator().plan(_request(), context)

    assert plan.dialect == PromptCacheDialect.OPENROUTER_ANTHROPIC_EXPLICIT
    assert plan.breakpoints


def test_routing_key_survives_system_and_tool_face_changes() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openrouter",
        model_id="openai/gpt-5.6-sol",
        provider_id="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
    )
    changed = replace(
        request,
        messages=(
            replace(
                request.messages[0],
                parts=(TextPartIR("a different stable system"),),
            ),
            *request.messages[1:],
        ),
        tools=(
            ToolDefinitionIR(
                name="new_tool",
                description="A changed tool face.",
                input_schema={"type": "object"},
            ),
        ),
    )
    coordinator = PromptCacheCoordinator()

    before = coordinator.plan(request, context)
    after = coordinator.plan(changed, context)

    assert before.cache_key == after.cache_key


def test_openai_chat_uses_supported_text_block_breakpoints() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_COMPLETION,
        endpoint_id="openai-chat",
        model_id="gpt-5.6-luna",
        provider_id="OpenAI",
    )
    coordinator = PromptCacheCoordinator()

    plan = coordinator.plan(request, context)
    encoded = coordinator.inject(OpenAICompletionCodec().encode(request, context), plan)
    payload = thaw_json(encoded.payload)

    assert plan.dialect == PromptCacheDialect.OPENAI_CHAT_EXPLICIT
    marked = [
        block
        for message in payload["messages"]
        for block in message.get("content", [])
        if "prompt_cache_breakpoint" in block
    ]
    assert len(marked) == 3


def test_anthropic_cache_control_uses_long_stable_and_short_rolling_ttl() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.ANTHROPIC_MESSAGES,
        endpoint_id="anthropic",
        model_id="claude-sonnet",
        provider_id="Anthropic",
    )
    coordinator = PromptCacheCoordinator()

    plan = coordinator.plan(request, context)
    encoded = coordinator.inject(AnthropicMessagesCodec().encode(request, context), plan)
    payload = thaw_json(encoded.payload)

    assert payload["system"][-1]["cache_control"]["ttl"] == "1h"
    rolling_markers = [
        block["cache_control"]["ttl"]
        for message in payload["messages"]
        for block in message["content"]
        if isinstance(block, dict) and "cache_control" in block
    ]
    assert rolling_markers == ["5m", "5m"]


def test_rolling_write_is_disabled_after_an_uneconomic_probe() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openai",
        model_id="gpt-5.6-sol",
        provider_id="OpenAI",
    )
    coordinator = PromptCacheCoordinator()
    first = coordinator.plan(request, context)
    coordinator.observe(
        first,
        LLMUsageIR(
            input_tokens=10_000,
            cache_write_input_tokens=10_000,
            reported=True,
        ),
    )

    second = coordinator.plan(request, context)

    assert second.decision == "rolling_not_economic"
    assert [item.label for item in second.breakpoints] == ["stable"]


def test_unknown_openai_compatible_provider_gets_no_unsupported_cache_fields() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_COMPLETION,
        endpoint_id="compatible",
        model_id="vendor-model",
        provider_id="SomeVendor",
    )
    coordinator = PromptCacheCoordinator()

    plan = coordinator.plan(request, context)
    encoded = coordinator.inject(OpenAICompletionCodec().encode(request, context), plan)

    assert plan.dialect == PromptCacheDialect.NONE
    assert encoded.extra_body == {}


def test_cache_economics_use_only_the_latest_eight_observations() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openai",
        model_id="gpt-5.6-sol",
        provider_id="OpenAI",
    )
    coordinator = PromptCacheCoordinator()
    plan = coordinator.plan(request, context)
    for _ in range(10):
        coordinator.observe(
            plan,
            LLMUsageIR(input_tokens=1_000, cached_input_tokens=1_000, reported=True),
        )

    assert coordinator.snapshot()["observed_request_count"] == 8


def test_prompt_cache_scope_accounting_is_lru_bounded() -> None:
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openai",
        model_id="gpt-5.6-sol",
        provider_id="OpenAI",
    )
    coordinator = PromptCacheCoordinator(max_scope_count=8)
    for index in range(40):
        coordinator.plan(
            replace(_request(), logical_scope_id=f"minion:{index}"),
            context,
        )

    assert coordinator.snapshot()["scope_count"] == 8
