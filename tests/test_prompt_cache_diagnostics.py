"""Offline regression for per-attempt prompt-cache diagnostics (PR1).

Covers the zero-write counterexample from the cache plan: explicit markers
applied and accepted, yet usage reports no cache read and no cache write.
Diagnostics must expose that state without changing confirmation semantics.
All fixtures are local; no paid calls are made.
"""
from __future__ import annotations

import json

import pytest
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
from pal.llm.prompt_cache import PromptCacheCoordinator
from pal.llm.shapes.base import ShapeContext, _JSONFrame
from pal.llm.shapes.openai_response import OpenAIResponseCodec, OpenAIResponseDecoder
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR


def _base_request() -> LLMRequestIR:
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


def _active_tool_request(request: LLMRequestIR, *, suffix: str) -> LLMRequestIR:
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


def _extend_active_tool_request(request: LLMRequestIR, *, suffix: str) -> LLMRequestIR:
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


def _astra_context() -> ShapeContext:
    return ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openrouter-astra",
        model_id="openai/gpt-6-astra",
        provider_id="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
    )


def _record_round(
    coordinator: PromptCacheCoordinator,
    request: LLMRequestIR,
    context: ShapeContext,
    *,
    request_id: str,
    usage: LLMUsageIR,
    actual_provider: str = "fixture-provider",
    previous_gap: float | None = None,
) -> None:
    codec = OpenAIResponseCodec()
    plan = coordinator.plan(request, context)
    encoded = coordinator.inject(codec.encode(request, context), plan)
    diagnostics = coordinator.start_attempt(plan, request=request, context=context,
        raw_encoded=codec.encode(request, context), encoded=encoded, request_id=request_id)
    if previous_gap is not None:
        diagnostics["previous_request_gap"] = previous_gap
    coordinator.record_success(
        plan,
        usage,
        applied_cache_breakpoint_message_ids=encoded.applied_cache_breakpoint_message_ids,
        request_id=request_id,
        endpoint_id=context.endpoint_id,
        model_id=context.model_id,
        provider_id=context.provider_id,
        wire_shape="openai_response",
        finish_reason="tool_calls",
        elapsed_seconds=0.5,
        provider_generation_id=f"resp-{request_id}", actual_provider=actual_provider,
        **diagnostics,
    )


def test_zero_write_counterexample_is_exposed_without_semantics_change() -> None:
    coordinator = PromptCacheCoordinator()
    context = _astra_context()
    request = _active_tool_request(_base_request(), suffix="one")

    codec = OpenAIResponseCodec()
    plan = coordinator.plan(request, context)
    encoded = coordinator.inject(codec.encode(request, context), plan)
    assert encoded.applied_cache_breakpoint_message_ids

    coordinator.record_success(
        plan,
        LLMUsageIR(
            reported=True,
            reported_fields=("input_tokens", "cached_input_tokens", "cache_write_input_tokens"),
            input_tokens=50_000,
            uncached_input_tokens=50_000,
            cached_input_tokens=0,
            cache_write_input_tokens=0,
        ),
        applied_cache_breakpoint_message_ids=encoded.applied_cache_breakpoint_message_ids,
        request_id="llm-zero-write",
        endpoint_id=context.endpoint_id,
        model_id=context.model_id,
        provider_id=context.provider_id,
        wire_shape="openai_response",
        finish_reason="tool_calls",
        elapsed_seconds=1.0,
        provider_generation_id="resp-zero-write",
    )

    snapshot = coordinator.snapshot()
    record = snapshot["recent_attempts"][-1]
    assert record["attempt_id"] == "llm-zero-write"
    assert record["provider_generation_id"] == "resp-zero-write"
    assert record["status"] == "success"
    assert record["cache_mode"] == "explicit"
    assert record["dialect"] == "openrouter_openai_explicit"
    assert record["applied_marker_ids"]
    assert record["usage_reported"] is True
    assert record["read_observed"] is False
    assert record["write_observed"] is False
    # The counterexample the plan demands: markers were applied, the provider
    # reported usage, and nothing was read or written. The diagnostic says so.
    assert record["zero_read_write_with_applied_markers"] is True
    # Receipt success is not evidence of a readable boundary.
    assert snapshot["confirmed_checkpoint"] is False
    assert snapshot["handoff"]["promotions"] == 0


def test_multi_round_records_keep_stable_key_and_bound_the_ring() -> None:
    coordinator = PromptCacheCoordinator(max_attempt_records=4)
    context = _astra_context()
    base = _base_request()
    rounds = [
        base,
        _active_tool_request(base, suffix="one"),
    ]
    for suffix in ("two", "three", "four", "five"):
        rounds.append(_extend_active_tool_request(rounds[-1], suffix=suffix))

    usages = [
        # Round 0: cold write of the stable+anchor prefix.
        LLMUsageIR(
            reported=True,
            input_tokens=90_000,
            cache_write_input_tokens=70_000,
            uncached_input_tokens=20_000,
        ),
    ]
    for index in range(1, len(rounds)):
        usages.append(
            LLMUsageIR(
                reported=True,
                input_tokens=90_000 + index * 5_000,
                cached_input_tokens=70_000 + index * 3_000,
                uncached_input_tokens=20_000,
            )
        )

    for index, (request, usage) in enumerate(zip(rounds, usages)):
        _record_round(coordinator, request, context, request_id=f"llm-r{index}", usage=usage)

    snapshot = coordinator.snapshot()
    records = snapshot["recent_attempts"]
    assert snapshot["attempt_record_count"] == 4
    assert [item["attempt_id"] for item in records] == [
        "llm-r2",
        "llm-r3",
        "llm-r4",
        "llm-r5",
    ]
    # One scope, one endpoint: the wire cache key must stay stable.
    assert len({item["cache_key_hash"] for item in records}) == 1
    assert all(item["cache_mode"] == "explicit" for item in records)
    assert all(
        any(marker["label"] == "stable" for marker in item["planned_markers"])
        for item in records
    )
    assert any(item["read_observed"] for item in records)
    assert len({item["provider_generation_id"] for item in records}) == 4
    assert len({item["attempt_id"] for item in records}) == 4


def test_failed_attempt_is_recorded_with_error_context() -> None:
    coordinator = PromptCacheCoordinator()
    context = _astra_context()
    plan = coordinator.plan(_base_request(), context)

    coordinator.record_attempt_failure(
        plan,
        request_id="llm-failed",
        endpoint_id=context.endpoint_id,
        model_id=context.model_id,
        provider_id=context.provider_id,
        wire_shape="openai_response",
        finish_reason="error",
        error="provider finish_reason=error",
        elapsed_seconds=2.0,
        provider_generation_id="resp-failed",
    )

    record = coordinator.snapshot()["recent_attempts"][-1]
    assert record["status"] == "failed"
    assert record["error"] == "provider finish_reason=error"
    assert record["finish_reason"] == "error"
    assert record["provider_generation_id"] == "resp-failed"
    assert record["usage_reported"] is False
    assert record["read_observed"] is False
    assert record["write_observed"] is False
    assert record["zero_read_write_with_applied_markers"] is False


def test_provider_generation_id_is_decoded_from_responses_payload() -> None:
    context = _astra_context()
    decoder = OpenAIResponseDecoder(context)
    decoder.feed(
        _JSONFrame(
            sequence=0,
            payload={
                "id": "resp_abc123",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello"}],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )
    )
    response = decoder.finish()
    assert response.text == "hello"
    assert response.provider_generation_id == "resp_abc123"


def test_attempt_records_never_contain_raw_keys() -> None:
    coordinator = PromptCacheCoordinator()
    context = _astra_context()
    request = _active_tool_request(_base_request(), suffix="one")
    plan = coordinator.plan(request, context)

    coordinator.record_success(
        plan,
        LLMUsageIR(reported=True, input_tokens=1_000, uncached_input_tokens=1_000),
        applied_cache_breakpoint_message_ids=tuple(
            item.message_id for item in plan.breakpoints
        ),
        request_id="llm-hash",
        endpoint_id=context.endpoint_id,
        model_id=context.model_id,
        provider_id=context.provider_id,
        wire_shape="openai_response",
        finish_reason="tool_calls",
    )

    record = coordinator.snapshot()["recent_attempts"][-1]
    dumped = json.dumps(record)
    assert "pal-" not in dumped
    assert plan.scope_key not in dumped
    assert plan.cache_key not in dumped
    assert record["scope_key_hash"] != plan.scope_key
    assert record["cache_key_hash"] != plan.cache_key
    assert len(record["scope_key_hash"]) == 16
    assert len(record["cache_key_hash"]) == 16


@pytest.mark.parametrize("interruption", ["missing", "provider_unknown", "provider_changed", "long_gap"])
def test_uncertain_round_breaks_consecutive_stall_evidence(interruption):
    from pal.llm.usage_normalization import usage_from_mapping
    coordinator = PromptCacheCoordinator()
    context = _astra_context()
    request = _base_request()
    usage = usage_from_mapping({"input_tokens": 100000,
        "input_tokens_details": {"cached_tokens": 2000, "cache_write_tokens": 0}})
    for index in range(3):
        _record_round(coordinator, request, context, request_id=f"before-{index}", usage=usage)
        request = _extend_active_tool_request(request, suffix=f"before-{index}")
    assert coordinator.snapshot()["observation"]["stall_suspected"]
    kwargs = {}
    if interruption == "missing":
        interrupted_usage = usage_from_mapping({"input_tokens": 100000})
    else:
        interrupted_usage = usage
    if interruption.startswith("provider_"):
        kwargs["actual_provider"] = "" if interruption == "provider_unknown" else "other-provider"
    if interruption == "long_gap":
        kwargs["previous_gap"] = 301
    _record_round(coordinator, request, context, request_id="interrupted", usage=interrupted_usage, **kwargs)
    observation = coordinator.snapshot()["observation"]
    assert not observation["stall_suspected"]
    assert observation["stall_reason"] == ("cache_fields_missing" if interruption == "missing" else interruption)
    provider = "other-provider" if interruption == "provider_changed" else "fixture-provider"
    for index in range(3):
        request = _extend_active_tool_request(request, suffix=f"after-{index}")
        _record_round(coordinator, request, context, request_id=f"after-{index}", usage=usage, actual_provider=provider)
        assert coordinator.snapshot()["observation"]["stall_suspected"] == (index == 2)
