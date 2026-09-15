"""PR2: cache evidence layering, usage conservation, and profile selection.

Offline only: real codecs and coordinator, no paid calls. Covers the plan's
acceptance items — usage merge conservation (§6), submission vs observation
evidence split with lifecycle timestamps (§5.1/§5.2), stall suspicion as a
warn-only diagnostic (§5.3), and A/B/C cache profile wire snapshots with dual
stable routing keys (§3.3/§4).
"""
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
from pal.llm.prompt_cache import PromptCacheCoordinator
from pal.llm.shapes.base import ShapeContext
from pal.llm.shapes.builder import merge_usage, usage_from_mapping
from pal.llm.shapes.openai_response import OpenAIResponseCodec
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR


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


def _astra_context(capabilities: dict | None = None) -> ShapeContext:
    return ShapeContext(
        wire_shape=WireShape.OPENAI_RESPONSE,
        endpoint_id="openrouter-astra",
        model_id="openai/gpt-6-astra",
        provider_id="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        capabilities=capabilities or {},
    )


def _marked_blocks(payload: dict) -> list[dict]:
    return [
        block
        for item in payload["input"]
        for block in item.get("content", [])
        if isinstance(block, dict) and "prompt_cache_breakpoint" in block
    ]


def _record_round(
    coordinator: PromptCacheCoordinator,
    request: LLMRequestIR,
    context: ShapeContext,
    *,
    request_id: str,
    usage: LLMUsageIR,
) -> tuple:
    codec = OpenAIResponseCodec()
    plan = coordinator.plan(request, context)
    encoded = coordinator.inject(codec.encode(request, context), plan)
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
        provider_generation_id=f"resp-{request_id}",
    )
    return plan, encoded


def test_merge_usage_recomputes_derived_split_from_final_classification() -> None:
    # Plan §6 regression: an intermediate frame without cache fields must not
    # keep the derived uncached split inflated after the final frame reports
    # full classification.
    intermediate = usage_from_mapping({"input_tokens": 10000})
    final = usage_from_mapping(
        {
            "input_tokens": 10000,
            "prompt_tokens_details": {
                "cached_tokens": 8000,
                "cache_write_tokens": 2000,
            },
        }
    )
    merged = merge_usage(intermediate, final)
    assert merged.input_tokens == 10000
    assert merged.cached_input_tokens == 8000
    assert merged.cache_write_input_tokens == 2000
    assert merged.uncached_input_tokens == 0
    assert merged.usage_anomaly == ""
    assert merged.input_tokens == (
        merged.uncached_input_tokens
        + merged.cached_input_tokens
        + merged.cache_write_input_tokens
    )


def test_merge_usage_flags_impossible_counts_as_anomaly() -> None:
    left = usage_from_mapping({"input_tokens": 5000})
    right = usage_from_mapping(
        {
            "input_tokens": 5000,
            "prompt_tokens_details": {
                "cached_tokens": 4000,
                "cache_write_tokens": 2000,
            },
        }
    )
    assert right.usage_anomaly == "input_lt_read_write"
    assert right.uncached_input_tokens == 0
    merged = merge_usage(left, right)
    assert merged.uncached_input_tokens == 0
    assert merged.usage_anomaly == "input_lt_read_write"


def test_wire_snapshots_for_legacy_implicit_and_hybrid_profiles() -> None:
    codec = OpenAIResponseCodec()
    base = _request()

    # Group A (default): explicit mode with anchor/frontier markers.
    context = _astra_context()
    coordinator = PromptCacheCoordinator()
    plan = coordinator.plan(base, context)
    encoded = coordinator.inject(codec.encode(base, context), plan)
    payload = thaw_json(encoded.payload)
    assert plan.dialect.value == "openrouter_openai_explicit"
    assert plan.profile_id == ""
    assert encoded.extra_body["prompt_cache_key"].startswith("pal-")
    assert encoded.extra_body["session_id"] == encoded.extra_body["prompt_cache_key"]
    assert encoded.extra_body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert len(_marked_blocks(payload)) >= 2

    # Group B: provider implicit; both stable keys, no options, no markers.
    context_b = _astra_context(
        {"prompt_cache": {"cache_profile": "openrouter_astra_provider_implicit"}}
    )
    coordinator_b = PromptCacheCoordinator()
    plan_b = coordinator_b.plan(base, context_b)
    encoded_b = coordinator_b.inject(codec.encode(base, context_b), plan_b)
    payload_b = thaw_json(encoded_b.payload)
    assert plan_b.dialect.value == "openrouter_automatic"
    assert plan_b.decision == "provider_automatic"
    assert plan_b.profile_id == "openrouter_astra_provider_implicit"
    assert plan_b.strategy == "provider_implicit"
    assert encoded_b.extra_body["prompt_cache_key"].startswith("pal-")
    assert encoded_b.extra_body["session_id"] == encoded_b.extra_body["prompt_cache_key"]
    assert "prompt_cache_options" not in encoded_b.extra_body
    assert _marked_blocks(payload_b) == []
    _record_round(
        coordinator_b,
        base,
        context_b,
        request_id="llm-b",
        usage=LLMUsageIR(reported=True, input_tokens=1000, uncached_input_tokens=1000),
    )
    record_b = coordinator_b.snapshot()["recent_attempts"][-1]
    assert record_b["profile_id"] == "openrouter_astra_provider_implicit"
    assert record_b["cache_mode"] == "automatic"

    # Group C: hybrid — implicit plus exactly one verified stable anchor.
    context_c = _astra_context(
        {"prompt_cache": {"cache_profile": "openrouter_astra_hybrid_anchor"}}
    )
    coordinator_c = PromptCacheCoordinator()
    plan_c = coordinator_c.plan(base, context_c)
    encoded_c = coordinator_c.inject(codec.encode(base, context_c), plan_c)
    payload_c = thaw_json(encoded_c.payload)
    assert plan_c.dialect.value == "openrouter_automatic"
    assert plan_c.decision == "hybrid_stable_anchor"
    assert plan_c.profile_id == "openrouter_astra_hybrid_anchor"
    assert [item.label for item in plan_c.breakpoints] == ["stable_anchor"]
    assert encoded_c.extra_body["prompt_cache_key"].startswith("pal-")
    assert encoded_c.extra_body["session_id"] == encoded_c.extra_body["prompt_cache_key"]
    assert "prompt_cache_options" not in encoded_c.extra_body
    marked = _marked_blocks(payload_c)
    assert len(marked) == 1
    assert encoded_c.applied_cache_breakpoint_message_ids == (
        plan_c.breakpoints[0].message_id,
    )


def test_unknown_profile_falls_back_to_default_policy() -> None:
    context = _astra_context({"prompt_cache": {"cache_profile": "bogus_profile"}})
    coordinator = PromptCacheCoordinator()
    plan = coordinator.plan(_request(), context)
    assert plan.dialect.value == "openrouter_openai_explicit"
    assert plan.profile_id == ""


def test_usage_missing_and_reported_zero_are_distinct_observations() -> None:
    coordinator = PromptCacheCoordinator()
    context = _astra_context()
    request = _active_tool_request(_request(), suffix="one")

    _record_round(
        coordinator,
        request,
        context,
        request_id="llm-missing",
        usage=LLMUsageIR(reported=False),
    )
    snapshot = coordinator.snapshot()
    assert snapshot["observation"]["state"] == "usage_missing"
    assert snapshot["observation"]["usage_observed_at"] == 0.0
    assert snapshot["recent_attempts"][-1]["observation"] == "usage_missing"

    _record_round(
        coordinator,
        request,
        context,
        request_id="llm-zero",
        usage=LLMUsageIR(reported=True, input_tokens=1000, uncached_input_tokens=1000),
    )
    snapshot = coordinator.snapshot()
    assert snapshot["observation"]["state"] == "reported_zero"
    assert snapshot["observation"]["usage_observed_at"] > 0.0
    assert snapshot["observation"]["last_request_at"] > 0.0
    assert snapshot["recent_attempts"][-1]["observation"] == "reported_zero"


def test_stall_suspected_after_three_flat_rounds_and_clears_on_recovery() -> None:
    coordinator = PromptCacheCoordinator()
    context = _astra_context()
    rounds = [_active_tool_request(_request(), suffix="one")]
    for suffix in ("two", "three"):
        rounds.append(_extend_active_tool_request(rounds[-1], suffix=suffix))
    flat_usage = LLMUsageIR(
        reported=True,
        input_tokens=90_000,
        cached_input_tokens=8_000,
        uncached_input_tokens=82_000,
    )
    for index, request in enumerate(rounds):
        _record_round(
            coordinator,
            request,
            context,
            request_id=f"llm-s{index}",
            usage=flat_usage,
        )
    snapshot = coordinator.snapshot()
    assert snapshot["observation"]["stall_suspected"] is True
    assert snapshot["observation"]["stall_rounds"] == 3

    recovery = _extend_active_tool_request(rounds[-1], suffix="four")
    _record_round(
        coordinator,
        recovery,
        context,
        request_id="llm-s3",
        usage=LLMUsageIR(
            reported=True,
            input_tokens=95_000,
            cached_input_tokens=20_000,
            uncached_input_tokens=75_000,
        ),
    )
    snapshot = coordinator.snapshot()
    assert snapshot["observation"]["stall_suspected"] is False
    assert snapshot["observation"]["stall_rounds"] == 0


def test_frontier_marker_submitted_without_observed_read() -> None:
    # Plan §10.1: markers injected and accepted, usage reports zero read and
    # zero write — the track records submission, never observed readability,
    # and planning semantics stay unchanged in PR2.
    coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
    context = _astra_context()
    request = _active_tool_request(_request(), suffix="one")
    _record_round(
        coordinator,
        request,
        context,
        request_id="llm-zw",
        usage=LLMUsageIR(
            reported=True,
            input_tokens=50_000,
            uncached_input_tokens=50_000,
        ),
    )
    snapshot = coordinator.snapshot()
    assert snapshot["frontier"]["submitted"] is True
    assert snapshot["frontier"]["submitted_at"] > 0.0
    assert snapshot["observation"]["state"] == "reported_zero"
    assert snapshot["observation"]["last_observed_read_at"] == 0.0
    record = snapshot["recent_attempts"][-1]
    assert record["zero_read_write_with_applied_markers"] is True
    assert record["usage_invariant_violation"] is False
    # PR2 is evidence exposure only: confirmation stays as-is.
    assert snapshot["confirmed_checkpoint"] is True
