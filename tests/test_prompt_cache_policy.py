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
from pal.llm.prompt_cache import (
    PromptCacheCoordinator,
    PromptCacheDialect,
    PromptCachePlan,
)
from pal.llm.shapes.base import ShapeContext
from pal.llm.shapes.anthropic_messages import AnthropicMessagesCodec
from pal.llm.shapes.openai_response import OpenAIResponseCodec
from pal.llm.shapes.openai_completion import OpenAICompletionCodec
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolCallIR, ToolDefinitionIR, ToolResultIR


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
                MessageRole.DEVELOPER,
                (TextPartIR("dynamic reminder"),),
                semantic_kind="runtime_reminder",
                prompt_region=PromptRegionIR.ACTIVE_DYNAMIC,
            ),
        ),
        tools=(),
        policy=GenerationPolicyIR(max_output_tokens=128),
        logical_scope_id="pal:resident",
    )


def _next_request(request: LLMRequestIR) -> LLMRequestIR:
    return replace(
        request,
        messages=(
            request.messages[0],
            request.messages[1],
            replace(
                request.messages[2],
                prompt_region=PromptRegionIR.SETTLED_HISTORY,
            ),
            LLMMessageIR(
                MessageRole.ASSISTANT,
                (TextPartIR("reply " * 200),),
                prompt_region=PromptRegionIR.SETTLED_HISTORY,
            ),
            LLMMessageIR(
                MessageRole.USER,
                (TextPartIR("next " * 200),),
                prompt_region=PromptRegionIR.ACTIVE_INPUT,
            ),
            request.messages[-1],
        ),
    )


def _active_tool_request(request: LLMRequestIR, *, suffix: str = "one") -> LLMRequestIR:
    call_id = f"call-{suffix}"
    return replace(
        request,
        messages=(
            request.messages[0],
            request.messages[1],
            request.messages[2],
            LLMMessageIR(
                MessageRole.ASSISTANT,
                (ToolCallIR(call_id, "probe", {"suffix": suffix}),),
                prompt_region=PromptRegionIR.ACTIVE_HISTORY,
            ),
            LLMMessageIR(
                MessageRole.TOOL,
                (
                    ToolResultIR(
                        call_id=call_id,
                        name="probe",
                        content=("result " * 900),
                        ok=True,
                    ),
                ),
                prompt_region=PromptRegionIR.ACTIVE_HISTORY,
            ),
            request.messages[-1],
        ),
    )


def _extend_active_tool_request(
    request: LLMRequestIR,
    *,
    suffix: str,
) -> LLMRequestIR:
    call_id = f"call-{suffix}"
    return replace(
        request,
        messages=(
            *request.messages[:-1],
            LLMMessageIR(
                MessageRole.ASSISTANT,
                (ToolCallIR(call_id, "probe", {"suffix": suffix}),),
                prompt_region=PromptRegionIR.ACTIVE_HISTORY,
            ),
            LLMMessageIR(
                MessageRole.TOOL,
                (
                    ToolResultIR(
                        call_id=call_id,
                        name="probe",
                        content=(f"result-{suffix} " * 900),
                        ok=True,
                    ),
                ),
                prompt_region=PromptRegionIR.ACTIVE_HISTORY,
            ),
            request.messages[-1],
        ),
    )


def _record_success(
    coordinator: PromptCacheCoordinator,
    plan: PromptCachePlan,
    usage: LLMUsageIR,
    *,
    applied_cache_breakpoint_message_ids: tuple[str, ...] | None = None,
) -> None:
    applied = (
        tuple(item.message_id for item in plan.breakpoints)
        if applied_cache_breakpoint_message_ids is None
        else applied_cache_breakpoint_message_ids
    )
    coordinator.record_success(
        plan,
        usage,
        applied_cache_breakpoint_message_ids=applied,
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
    assert [item.label for item in plan.breakpoints] == [
        "stable",
        "anchor_candidate",
    ]
    assert encoded.extra_body["prompt_cache_key"].startswith("pal-")
    assert encoded.extra_body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    marked = [
        block
        for item in payload["input"]
        for block in item.get("content", [])
        if isinstance(block, dict) and "prompt_cache_breakpoint" in block
    ]
    assert len(marked) == 2
    assert set(encoded.applied_cache_breakpoint_message_ids) == {
        item.message_id for item in plan.breakpoints
    }
    reminder = payload["input"][-1]
    assert reminder["role"] == "developer"
    assert "prompt_cache_breakpoint" not in reminder["content"][0]
    current = next(
        item
        for item in payload["input"]
        if item.get("role") == "user"
        and str(item.get("content", [{}])[0].get("text") or "").startswith("current ")
    )
    assert "prompt_cache_breakpoint" in current["content"][0]


def test_openrouter_gpt56_uses_explicit_cache_and_sticky_session() -> None:
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
    payload = thaw_json(encoded.payload)

    assert plan.dialect == PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT
    assert plan.anchor.decision == "economic_advance"
    assert [item.label for item in plan.breakpoints] == [
        "stable",
        "anchor_candidate",
    ]
    assert encoded.extra_body["session_id"] == encoded.extra_body["prompt_cache_key"]
    assert encoded.extra_body["prompt_cache_key"].startswith("pal-")
    assert encoded.extra_body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    marked = [
        block
        for item in payload["input"]
        for block in item.get("content", [])
        if isinstance(block, dict) and "prompt_cache_breakpoint" in block
    ]
    assert len(marked) == 2


def test_openrouter_pre56_automatic_cache_does_not_report_inert_breakpoints() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openrouter",
        model_id="openai/gpt-5.5",
        provider_id="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
    )
    coordinator = PromptCacheCoordinator()

    first = coordinator.plan(request, context)
    _record_success(
        coordinator,
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


def test_openrouter_gpt56_chat_uses_explicit_cache_and_sticky_session() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_COMPLETION,
        endpoint_id="openrouter-chat",
        model_id="openai/gpt-5.6-luna",
        provider_id="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
    )
    coordinator = PromptCacheCoordinator()

    plan = coordinator.plan(request, context)
    encoded = coordinator.inject(OpenAICompletionCodec().encode(request, context), plan)
    payload = thaw_json(encoded.payload)

    assert plan.dialect == PromptCacheDialect.OPENROUTER_OPENAI_EXPLICIT
    assert encoded.extra_body["session_id"] == encoded.extra_body["prompt_cache_key"]
    assert encoded.extra_body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    marked = [
        block
        for message in payload["messages"]
        for block in message.get("content", [])
        if isinstance(block, dict) and "prompt_cache_breakpoint" in block
    ]
    assert len(marked) == 2
    user_blocks = next(
        message["content"]
        for message in payload["messages"]
        if message["role"] == "user"
    )
    assert user_blocks[-1]["text"] == "dynamic reminder"
    assert "prompt_cache_breakpoint" not in user_blocks[-1]
    current_block = next(
        block
        for block in user_blocks
        if str(block.get("text") or "").startswith("current ")
    )
    assert "prompt_cache_breakpoint" in current_block


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
    assert plan.anchor.decision == "economic_batching"
    assert sum(
        "cache_control" in block
        for message in payload["messages"]
        for block in message["content"]
    ) == 0
    assert all(
        "dynamic reminder" not in str(block.get("text") or "")
        for block in payload["system"]
    )
    assert payload["messages"][-1]["content"][-1]["text"] == "dynamic reminder"
    assert "cache_control" not in payload["messages"][-1]["content"][-1]
    current_block = next(
        block
        for message in payload["messages"]
        for block in message["content"]
        if str(block.get("text") or "").startswith("current ")
    )
    assert "cache_control" not in current_block


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
    assert len(marked) == 2


def test_anthropic_cache_control_uses_long_stable_and_short_rolling_ttl() -> None:
    request = _active_tool_request(_request())
    context = ShapeContext(
        wire_shape=WireShape.ANTHROPIC_MESSAGES,
        endpoint_id="anthropic",
        model_id="claude-sonnet",
        provider_id="Anthropic",
    )
    coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)

    plan = coordinator.plan(request, context)
    encoded = coordinator.inject(AnthropicMessagesCodec().encode(request, context), plan)
    payload = thaw_json(encoded.payload)

    assert payload["system"][-1]["cache_control"]["ttl"] == "1h"
    assert plan.anchor.decision == "economic_batching"
    assert plan.frontier.decision == "economic_advance"
    rolling_markers = [
        block["cache_control"]["ttl"]
        for message in payload["messages"]
        for block in message["content"]
        if isinstance(block, dict) and "cache_control" in block
    ]
    assert rolling_markers == ["5m"]


def test_openai_responses_frontier_marks_tool_output_and_rolls_within_turn() -> None:
    request = _active_tool_request(_request())
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openai",
        model_id="gpt-5.6-luna",
        provider_id="OpenAI",
    )
    coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)

    first = coordinator.plan(request, context)
    raw = OpenAIResponseCodec().encode(request, context)
    encoded = coordinator.inject(raw, first)
    payload = thaw_json(encoded.payload)
    tool_output = next(
        item for item in payload["input"] if item.get("type") == "function_call_output"
    )

    assert first.anchor.candidate_message_id == request.messages[2].message_id
    assert first.frontier.candidate_message_id == request.messages[-2].message_id
    assert tool_output["output"][0]["type"] == "input_text"
    assert "prompt_cache_breakpoint" in tool_output["output"][0]
    _record_success(coordinator, first, LLMUsageIR(reported=True))

    reused = coordinator.plan(request, context)
    assert reused.anchor.decision == "confirmed_reuse"
    assert reused.frontier.decision == "confirmed_reuse"

    extended = _extend_active_tool_request(request, suffix="two")
    advanced = coordinator.plan(extended, context)
    assert advanced.anchor.decision == "confirmed_reuse"
    assert advanced.frontier.decision == "economic_advance"
    assert [item.label for item in advanced.breakpoints] == [
        "stable",
        "anchor_confirmed",
        "frontier_confirmed",
        "frontier_candidate",
    ]


def test_frontier_resets_when_active_input_changes_but_anchor_survives() -> None:
    request = _active_tool_request(_request())
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openai",
        model_id="gpt-5.6-luna",
        provider_id="OpenAI",
    )
    coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
    first = coordinator.plan(request, context)
    _record_success(coordinator, first, LLMUsageIR(reported=True))

    next_request = replace(
        request,
        messages=(
            request.messages[0],
            request.messages[1],
            replace(
                request.messages[2],
                prompt_region=PromptRegionIR.SETTLED_HISTORY,
            ),
            replace(
                request.messages[3],
                prompt_region=PromptRegionIR.SETTLED_HISTORY,
            ),
            replace(
                request.messages[4],
                prompt_region=PromptRegionIR.SETTLED_HISTORY,
            ),
            LLMMessageIR(
                MessageRole.USER,
                (TextPartIR("next user " * 900),),
                prompt_region=PromptRegionIR.ACTIVE_INPUT,
            ),
            request.messages[-1],
        ),
    )
    next_plan = coordinator.plan(next_request, context)

    assert next_plan.anchor.confirmed_message_id == request.messages[2].message_id
    assert next_plan.frontier.confirmed_message_id == ""
    assert next_plan.frontier.decision == "unavailable"
    assert "frontier_confirmed" not in {
        item.label for item in next_plan.breakpoints
    }


def test_anthropic_four_slots_prioritize_anchor_before_frontier_candidate() -> None:
    context = ShapeContext(
        wire_shape=WireShape.ANTHROPIC_MESSAGES,
        endpoint_id="anthropic",
        model_id="claude-sonnet",
        provider_id="Anthropic",
    )
    coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
    first_turn = _request()
    first = coordinator.plan(first_turn, context)
    _record_success(coordinator, first, LLMUsageIR(reported=True))
    anchor_write = coordinator.plan(first_turn, context)
    _record_success(coordinator, anchor_write, LLMUsageIR(reported=True))

    second_turn = replace(
        _active_tool_request(_request(), suffix="turn-two"),
        messages=(
            first_turn.messages[0],
            first_turn.messages[1],
            replace(
                first_turn.messages[2],
                prompt_region=PromptRegionIR.SETTLED_HISTORY,
            ),
            LLMMessageIR(
                MessageRole.ASSISTANT,
                (TextPartIR("settled reply " * 900),),
                prompt_region=PromptRegionIR.SETTLED_HISTORY,
            ),
            LLMMessageIR(
                MessageRole.USER,
                (TextPartIR("turn two " * 900),),
                prompt_region=PromptRegionIR.ACTIVE_INPUT,
            ),
            *_active_tool_request(_request(), suffix="turn-two").messages[3:],
        ),
    )
    frontier_write = coordinator.plan(second_turn, context)
    assert frontier_write.anchor.decision == "economic_batching"
    assert frontier_write.frontier.decision == "economic_advance"
    _record_success(coordinator, frontier_write, LLMUsageIR(reported=True))

    both_economic = coordinator.plan(
        _extend_active_tool_request(second_turn, suffix="turn-two-more"),
        context,
    )
    assert both_economic.anchor.decision == "economic_advance"
    assert both_economic.frontier.decision == "slot_deferred"
    assert [item.label for item in both_economic.breakpoints] == [
        "stable",
        "anchor_confirmed",
        "anchor_candidate",
        "frontier_confirmed",
    ]
    assert [item.ttl for item in both_economic.breakpoints] == [
        "1h",
        "1h",
        "1h",
        "5m",
    ]


def test_anthropic_anchor_is_gated_then_survives_the_turn_boundary() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.ANTHROPIC_MESSAGES,
        endpoint_id="anthropic",
        model_id="claude-sonnet",
        provider_id="Anthropic",
    )
    coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
    first = coordinator.plan(request, context)
    assert first.anchor.decision == "economic_batching"
    _record_success(coordinator, first, LLMUsageIR(reported=True))

    second = coordinator.plan(request, context)
    assert second.anchor.decision == "economic_advance"
    _record_success(coordinator, second, LLMUsageIR(reported=True))

    next_request = _next_request(request)
    third = coordinator.plan(next_request, context)
    encoded = coordinator.inject(
        AnthropicMessagesCodec().encode(next_request, context),
        third,
    )
    payload = thaw_json(encoded.payload)

    assert [item.label for item in third.breakpoints] == [
        "stable",
        "anchor_confirmed",
    ]
    assert third.anchor.confirmed_message_id == request.messages[2].message_id
    assert third.anchor.decision == "economic_batching"
    assert sum(
        "cache_control" in block
        for block in payload["system"]
    ) == 1
    assert sum(
        "cache_control" in block
        for message in payload["messages"]
        for block in message["content"]
    ) == 1
    assert payload["messages"][-1]["content"][-1]["text"] == "dynamic reminder"
    assert "cache_control" not in payload["messages"][-1]["content"][-1]


def test_rolling_batching_keeps_confirmed_until_accumulated_tail_is_economic() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openai",
        model_id="gpt-5.6-sol",
        provider_id="OpenAI",
    )
    coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=2_000)
    first = coordinator.plan(request, context)
    _record_success(
        coordinator,
        first,
        LLMUsageIR(
            input_tokens=10_000,
            cache_write_input_tokens=5_000,
            reported=True,
        ),
    )
    next_request = _next_request(request)

    second = coordinator.plan(next_request, context)
    assert second.anchor.decision == "economic_batching"
    assert [item.label for item in second.breakpoints] == [
        "stable",
        "anchor_confirmed",
    ]
    _record_success(coordinator, second, LLMUsageIR(input_tokens=1_000, reported=True))

    advanced = None
    for _ in range(20):
        candidate = coordinator.plan(next_request, context)
        if candidate.candidate_message_id:
            advanced = candidate
            break
        _record_success(
            coordinator,
            candidate,
            LLMUsageIR(input_tokens=1_000, reported=True),
        )

    assert advanced is not None
    assert advanced.anchor.decision == "economic_advance"
    assert [item.label for item in advanced.breakpoints] == [
        "stable",
        "anchor_confirmed",
        "anchor_candidate",
    ]
    _record_success(coordinator, advanced, LLMUsageIR(input_tokens=1_000, reported=True))
    reused = coordinator.plan(next_request, context)
    assert reused.anchor.decision == "confirmed_reuse"
    assert [item.label for item in reused.breakpoints] == [
        "stable",
        "anchor_confirmed",
    ]


def test_failed_candidate_is_not_promoted() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openai",
        model_id="gpt-5.6-sol",
        provider_id="OpenAI",
    )
    coordinator = PromptCacheCoordinator()

    failed = coordinator.plan(request, context)
    retry = coordinator.plan(request, context)

    assert failed.anchor.decision == "economic_advance"
    assert retry.anchor.decision == "economic_advance"
    assert [item.label for item in retry.breakpoints] == [
        "stable",
        "anchor_candidate",
    ]


def test_success_without_applied_candidate_marker_is_not_promoted() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openai",
        model_id="gpt-5.6-sol",
        provider_id="OpenAI",
    )
    coordinator = PromptCacheCoordinator()
    candidate = coordinator.plan(request, context)
    raw = OpenAIResponseCodec().encode(request, context)
    encoded = coordinator.inject(
        replace(
            raw,
            message_spans=tuple(
                replace(span, cache_targets=())
                if span.message_id == candidate.candidate_message_id
                else span
                for span in raw.message_spans
            ),
        ),
        candidate,
    )

    assert candidate.candidate_message_id
    assert (
        candidate.candidate_message_id
        not in encoded.applied_cache_breakpoint_message_ids
    )
    coordinator.record_success(
        candidate,
        LLMUsageIR(reported=True),
        applied_cache_breakpoint_message_ids=encoded.applied_cache_breakpoint_message_ids,
    )

    retry = coordinator.plan(request, context)
    assert retry.anchor.decision == "economic_advance"
    assert retry.confirmed_message_id == ""


def test_stale_success_cannot_regress_confirmed_checkpoint() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openai",
        model_id="gpt-5.6-sol",
        provider_id="OpenAI",
    )
    coordinator = PromptCacheCoordinator()
    older = coordinator.plan(request, context)
    newer = coordinator.plan(request, context)

    _record_success(coordinator, newer, LLMUsageIR(reported=True))
    _record_success(coordinator, older, LLMUsageIR(reported=True))

    reused = coordinator.plan(request, context)
    assert reused.anchor.decision == "confirmed_reuse"
    assert reused.confirmed_message_id == request.messages[2].message_id


def test_missing_confirmed_message_bootstraps_a_new_cache_epoch() -> None:
    request = _request()
    context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openai",
        model_id="gpt-5.6-sol",
        provider_id="OpenAI",
    )
    coordinator = PromptCacheCoordinator()
    first = coordinator.plan(request, context)
    _record_success(coordinator, first, LLMUsageIR(reported=True))
    compacted = replace(
        request,
        messages=(
            request.messages[0],
            LLMMessageIR(
                MessageRole.USER,
                (TextPartIR("compacted baseline " * 900),),
                prompt_region=PromptRegionIR.SETTLED_HISTORY,
            ),
            LLMMessageIR(
                MessageRole.USER,
                (TextPartIR("post compaction " * 900),),
                prompt_region=PromptRegionIR.ACTIVE_INPUT,
            ),
            request.messages[-1],
        ),
    )

    reset = coordinator.plan(compacted, context)

    assert reset.anchor.decision == "economic_advance"
    assert reset.confirmed_message_id == ""
    assert [item.label for item in reset.breakpoints] == [
        "stable",
        "anchor_candidate",
    ]


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
        _record_success(
            coordinator,
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
            replace(_request(), logical_scope_id=f"bunshin:{index}"),
            context,
        )

    assert coordinator.snapshot()["scope_count"] == 8


def test_warm_deadline_snapshot_uses_confirmed_anchor_provider_ttl() -> None:
    request = _request()
    openai_context = ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openai",
        model_id="gpt-5.6-luna",
        provider_id="OpenAI",
    )
    openai = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
    openai_plan = openai.plan(request, openai_context)
    _record_success(openai, openai_plan, LLMUsageIR(reported=True))

    openai_snapshot = openai.warm_deadline_snapshot()
    assert openai_snapshot["eligible"] is True
    assert openai_snapshot["anchor_ttl"] == "30m"
    assert openai_snapshot["anchor_ttl_seconds"] == 1_800
    assert 1_798 <= openai_snapshot["anchor_remaining_ttl_seconds"] <= 1_800
    assert openai_snapshot["anchor_epoch"]
    assert openai.warm_deadline_snapshot(
        logical_scope_id=request.logical_scope_id,
        endpoint_id=openai_context.endpoint_id,
    )["eligible"] is True
    assert openai.warm_deadline_snapshot(
        logical_scope_id=f"{request.logical_scope_id}:compaction",
        endpoint_id=openai_context.endpoint_id,
    )["eligible"] is False
    assert openai.warm_deadline_snapshot(
        logical_scope_id=request.logical_scope_id,
        endpoint_id="another-endpoint",
    )["eligible"] is False

    anthropic_context = ShapeContext(
        wire_shape=WireShape.ANTHROPIC_MESSAGES,
        endpoint_id="anthropic",
        model_id="claude-sonnet",
        provider_id="Anthropic",
    )
    anthropic = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
    first = anthropic.plan(request, anthropic_context)
    _record_success(anthropic, first, LLMUsageIR(reported=True))
    second = anthropic.plan(request, anthropic_context)
    _record_success(anthropic, second, LLMUsageIR(reported=True))

    anthropic_snapshot = anthropic.warm_deadline_snapshot()
    assert anthropic_snapshot["eligible"] is True
    assert anthropic_snapshot["anchor_ttl"] == "1h"
    assert anthropic_snapshot["anchor_ttl_seconds"] == 3_600
    assert 3_598 <= anthropic_snapshot["anchor_remaining_ttl_seconds"] <= 3_600
