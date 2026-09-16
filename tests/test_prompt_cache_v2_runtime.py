"""Offline provider-boundary regressions and a complete same-turn Pal fixture."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pal.core import PalCore
from pal.core.capabilities import register_with_core
from pal.core.turns import LLMRequestEffect, TurnContinuation
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.ir import LLMUsageIR, WireShape
from pal.llm.model_hooks import ModelHook, ModelHookRegistry
from pal.llm.prompt_cache import CacheProfileError, PromptCacheCoordinator
from pal.llm.response_evidence import WireResponseEvidence
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.llm.serde import response_from_payload, response_to_payload
from pal.llm.shapes.base import ShapeContext, _JSONFrame
from pal.llm.usage import LLMUsageLedger
from pal.llm.usage_normalization import merge_usage, usage_from_mapping
from pal.memory import MemoryService, register_with_core as register_memory
from pal.shared import PromptAssemblyContext, PromptFragment
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolResultIR
from tests.test_llm_endpoint_runtime_contract import _endpoint
from tests.test_prompt_cache_evidence import _request, _astra_context

PROFILES = ("openrouter_astra_legacy_explicit", "openrouter_astra_provider_implicit", "openrouter_astra_hybrid_anchor")


def endpoint(profile=PROFILES[0]):
    ep = _endpoint("astra", model_id="openai/gpt-6-astra")
    ep.provider = "OpenRouter"
    ep.wire_shape = "openai_response"
    ep.base_url = "https://openrouter.ai/api/v1"
    ep.context_window = 1_000_000
    ep.capabilities_blob = {"prompt_cache": {"cache_profile": profile}}
    ep.supports_streaming = False
    return ep


class Settings:
    def get_active_llm_endpoint_id(self):
        return "astra"

    def get_think_level(self, _endpoint_id):
        return "off"


def runtime(ep, transport, tmp_path):
    invoker = ShapeEndpointInvoker(transport=transport)
    return LLMRuntime(EndpointResolver(endpoints=(ep,)), Settings(), endpoint_invoker=invoker,
                      config=SimpleNamespace(runtime_root=tmp_path, llm_endpoint_retry_attempts=1,
                                             llm_max_output_recovery_attempts=0))


def response_payload(index=1, *, tool=False, usage=None):
    return {"id": f"generation-{index}", "model": "openai/gpt-6-astra", "provider": "fixture-provider",
            "status": "completed", "service_tier": "fixture",
            "output": ([{"type": "reasoning", "id": f"reason-{index}", "encrypted_content": f"opaque-{index}", "summary": []},
                        {"type": "function_call", "id": f"fc-{index}", "call_id": f"call-{index}",
                         "name": "fixture_probe", "arguments": "{}", "status": "completed"}]
                       if tool else [{"type": "message", "id": f"msg-{index}", "role": "assistant",
                                      "content": [{"type": "output_text", "text": "done"}]}]),
            "usage": usage if usage is not None else {"input_tokens": 100_000, "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 2000, "cache_write_tokens": 0}, "cost": 0.01}}


@pytest.mark.parametrize("profile", PROFILES)
def test_disabled_and_mismatched_profiles_fail_before_transport(profile):
    ctx = _astra_context({"prompt_cache": {"enabled": False, "cache_profile": profile}})
    assert not PromptCacheCoordinator().plan(_request(), ctx).enabled
    for override in ({"model_id": "other"}, {"provider_id": "OpenAI"}, {"wire_shape": WireShape.OPENAI_COMPLETION}):
        with pytest.raises(CacheProfileError):
            PromptCacheCoordinator().plan(_request(), replace(ctx, capabilities={"prompt_cache": {"cache_profile": profile}}, **override))


def test_final_usage_can_correct_counts_down_and_missing_is_not_zero():
    initial = usage_from_mapping({"input_tokens": 10000, "input_tokens_details": {"cached_tokens": 9000}, "cost": 0.2})
    final = usage_from_mapping({"input_tokens": 9500, "input_tokens_details": {"cached_tokens": 7000, "cache_write_tokens": 500}, "cost": 0.1}, final=True)
    usage = merge_usage(initial, final)
    assert (usage.input_tokens, usage.cached_input_tokens, usage.uncached_input_tokens, usage.cost) == (9500, 7000, 2000, 0.1)
    assert merge_usage(usage, initial) == usage
    assert usage.cache_partition_reported
    missing = usage_from_mapping({"input_tokens": 10})
    assert not missing.has("cached_input_tokens")
    assert not missing.cost_reported
    assert usage_from_mapping({"cost": 0}).cost_reported
    raw = usage_from_mapping({"input_tokens": 5, "input_tokens_details": {"cached_tokens": 10}})
    assert raw.usage_anomaly == "input_lt_read_write"
    assert dict(raw.raw_counters)["input_tokens_details.cached_tokens"] == 10


def test_anthropic_partial_final_keeps_separate_input_accounting():
    start = usage_from_mapping({"input_tokens": 100, "cache_read_input_tokens": 800,
                                "cache_creation_input_tokens": 100}, input_accounting="exclusive_cache")
    final = usage_from_mapping({"output_tokens": 50}, input_accounting="exclusive_cache", final=True)
    merged = merge_usage(start, final)
    assert (merged.input_tokens, merged.uncached_input_tokens, merged.cached_input_tokens, merged.output_tokens) == (1000, 100, 800, 50)


@pytest.mark.parametrize("kind", ["timeout", "decode", "cancel", "error", "close"])
def test_failed_attempt_keeps_partial_usage_without_double_accounting(kind):
    class Transport:
        def frames(self, *_args):
            if kind == "error":
                payload = response_payload()
                payload["status"] = "failed"
                yield _JSONFrame(0, payload)
                return
            yield _JSONFrame(0, {"type": "response.created", "response": {
                "id": "partial-generation", "usage": {"input_tokens": 123, "cost": 0.25}}})
            if kind == "close":
                yield _JSONFrame(1, {"type": "response.output_text.delta", "delta": "partial"})
                raise AssertionError("closed stream must not request another frame")
            if kind == "decode":
                yield _JSONFrame(1, {"type": "response.completed", "response": {"output": []}})
            elif kind == "cancel":
                raise asyncio.CancelledError()
            else:
                raise TimeoutError("synthetic provider timeout")
    ledger = LLMUsageLedger()
    invoker = ShapeEndpointInvoker(transport=Transport(), attempt_sink=ledger.record_attempt)
    if kind == "close":
        iterator = invoker.invoke_updates(endpoint(), _request())
        assert next(iterator).response.text == "partial"
        iterator.close()
    elif kind == "error":
        invoker.invoke(endpoint(), _request())
    else:
        with pytest.raises((TimeoutError, RuntimeError, asyncio.CancelledError)):
            invoker.invoke(endpoint(), _request())
    records = invoker.prompt_cache.snapshot()["recent_attempts"]
    assert len(records) == 1
    record = records[0]
    assert record["status"] in {"failed", "cancelled"}
    assert record["actual_cost"] == (0.01 if kind == "error" else 0.25)
    assert ledger.snapshot()["cost"] == record["actual_cost"]
    assert ledger.snapshot()["provider_request_count"] == 1


def test_failure_without_settlement_is_unknown_not_free():
    class Transport:
        def frames(self, *_args):
            raise TimeoutError("before first frame")
    invoker = ShapeEndpointInvoker(transport=Transport())
    with pytest.raises(TimeoutError):
        invoker.invoke(endpoint(), _request())
    record = invoker.prompt_cache.snapshot()["recent_attempts"][0]
    assert record["actual_cost"] is None
    assert record["cost_status"] == "unknown"


def test_retry_charges_failed_attempt_and_success_once(tmp_path):
    class Transport:
        calls = 0
        def frames(self, *_args):
            self.calls += 1
            if self.calls == 1:
                yield _JSONFrame(0, {"type": "response.created", "response": {"id": "failed", "usage": {"input_tokens": 10, "cost": 0.2}}})
                raise TimeoutError("retryable")
            yield _JSONFrame(0, response_payload())
    llm = runtime(endpoint(), Transport(), tmp_path)
    llm.endpoint_retry_attempts = 2
    result = llm.generate(_request())
    assert result.response.text == "done"
    snapshot = llm.usage_ledger.snapshot()
    assert snapshot["cost"] == pytest.approx(0.21)
    assert snapshot["provider_request_count"] == 2
    assert snapshot["successful_request_count"] == 1
    assert snapshot["failed_attempt_count"] == 1
    assert len(llm.endpoint_invoker.prompt_cache.snapshot()["recent_attempts"]) == 2
    roundtrip = response_from_payload(response_to_payload(result.response))
    assert roundtrip.provider_generation_id == "generation-1"
    assert roundtrip.usage == result.response.usage
    assert roundtrip.attempt_ids == result.response.attempt_ids


def test_profile_snapshot_and_hook_precedence(tmp_path):
    class Transport:
        def frames(self, *_args):
            yield _JSONFrame(0, response_payload())
    ep = endpoint()
    ep.capabilities_blob = {}
    llm = runtime(ep, Transport(), tmp_path)
    llm.model_hooks = ModelHookRegistry({ep.model_id: ModelHook(ep.model_id, cache_profile_ref=PROFILES[1])})
    frozen = llm.cache_policy_snapshot()
    ep.capabilities_blob = {"prompt_cache": {"cache_profile": PROFILES[2]}}
    old = llm._compile_request(ep, replace(_request(), metadata={"cache_policy_snapshot": frozen})).request
    new = llm._compile_request(ep, _request()).request
    assert old.metadata["cache_policy_selection"]["cache_profile"] == PROFILES[1]
    assert old.metadata["cache_policy_selection"]["origin"] == "model_hook"
    assert new.metadata["cache_policy_selection"]["cache_profile"] == PROFILES[2]
    assert new.metadata["cache_policy_selection"]["origin"] == "endpoint"


class FixtureFragments:
    provider_id = "fixture.cache"
    module_id = "fixture"
    changing = "stable-runtime"
    def build_prompt_fragments(self, _context):
        return [PromptFragment(section="operating_guidance", title="Fixture instructions", content="fixture stable " * 1000,
                               metadata={"prompt_target": "developer"}),
                PromptFragment(section="artifact", title="Fixture context", content="reference-only fixture context",
                               metadata={"prompt_target": "user_context"}),
                PromptFragment(section="runtime", title="Fixture runtime", content=self.changing,
                               metadata={"prompt_target": "runtime_reminder"})]


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("rounds", [8, 50])
def test_real_compiler_executor_hook_codec_same_turn_chain(profile, rounds, tmp_path, mutation=None):
    async def run():
        class Transport:
            requests = []
            def frames(self, _endpoint, request):
                self.requests.append(request)
                yield _JSONFrame(0, response_payload(len(self.requests), tool=len(self.requests) < rounds))
        transport = Transport()
        llm = runtime(endpoint(profile), transport, tmp_path)
        llm.model_hooks = ModelHookRegistry({"openai/gpt-6-astra": ModelHook(
            "openai/gpt-6-astra", developer_instructions=("Stable fixture hook.",))})
        core = PalCore()
        register_with_core(core)
        memory = MemoryService()
        register_memory(core.context, memory)
        core.context.port_registry["llm:llm"] = llm
        fragments = FixtureFragments()
        core.context.prompt_fragment_registry.register(fragments)
        continuation = TurnContinuation("fixture-turn", iter(()), "fixture-request",
                                        turn_settings_snapshot=core.turn_manager._build_turn_settings_snapshot())
        memory.begin_l1_turn(continuation.turn_id, user_text="Run the harmless fixture probes.")
        assembly = PromptAssemblyContext(task_id="fixture-task", metadata={})
        tools = [{"type": "function", "function": {"name": "fixture_probe", "description": "Harmless fixed fixture",
                                                    "parameters": {"type": "object", "properties": {}}}}]
        records = []
        try:
            for n in range(rounds):
                if mutation == "dynamic" and n >= 4:
                    fragments.changing = f"current runtime state {n}"
                if mutation == "tools" and n == 4:
                    tools[0]["function"]["description"] = "Updated fixture tool contract"
                if mutation == "profile" and n == 4:
                    llm.active_endpoint().capabilities_blob = {"prompt_cache": {"cache_profile": PROFILES[2]}}
                if mutation == "compaction" and n == 4:
                    from pal.memory.contracts import MemoryCompactRequest, L2Entry
                    memory.compact(MemoryCompactRequest(8192, 1024, summary_entry=L2Entry(
                        "fixture-summary", "summary", "session", "Fixture summary", "Compacted fixture history",
                        rendered="Compacted fixture history")))
                result = await core.turn_executor.execute_turn_effect_async(continuation, LLMRequestEffect(
                    assembly_context=assembly, max_output_tokens=128, tools_override=tools))
                response = result.payload.response
                assert response.attempt_ids
                record = llm.endpoint_invoker.prompt_cache.snapshot()["recent_attempts"][-1]
                records.append(record)
                if n < rounds - 1:
                    assert response.tool_calls[0].name == "fixture_probe"
                    memory.append_l1_tool_result(continuation.turn_id, ToolResultIR(
                        call_id=response.tool_calls[0].call_id, name="fixture_probe", content=f"fixture result {n} " * 900))
            assert len({r["cache_key_hash"] for r in records}) == 1
            assert len({r["session_key_hash"] for r in records}) == 1
            assert [r["round_index"] for r in records] == list(range(1, rounds + 1))
            assert all(r["turn_id"] == "fixture-turn" and r["task_id"] == "fixture-task" for r in records)
            assert all(r["prefix_preserved"] for i, r in enumerate(records[1:], 1)
                       if not (i == 4 and mutation in {"tools", "compaction"}))
            if mutation in {"tools", "compaction"}:
                assert records[4]["change_reason"] == ("tools_changed" if mutation == "tools" else "prefix_changed")
            if mutation == "dynamic":
                assert all(r["prefix_preserved"] for r in records[4:])
            if mutation == "profile":
                assert all(r["profile_id"] == profile for r in records)
            assert records[-1]["estimated_prefix_tokens"] > records[0]["estimated_prefix_tokens"]
            assert llm.endpoint_invoker.prompt_cache.snapshot()["observation"]["stall_suspected"] is True
            assert not llm.endpoint_invoker.prompt_cache.snapshot()["confirmed_checkpoint"]
            for index, request in enumerate(transport.requests):
                payload = thaw_json(request.payload)
                assert "<pal_context" in str(payload["input"])
                if mutation is None:
                    assert str(payload["input"]).count("stable-runtime") == 1
                    assert str(payload["input"]).count("reference-only fixture context") == 1
                assert "reference-only fixture context" in str(payload["input"])
                for old in range(1, index + 1):
                    assert next(item for item in payload["input"] if item.get("id") == f"reason-{old}")["encrypted_content"] == f"opaque-{old}"
                if profile == PROFILES[1]:
                    assert "prompt_cache_breakpoint" not in str(payload)
                if profile != PROFILES[0]:
                    assert "prompt_cache_options" not in request.extra_body
            assert llm.usage_ledger.snapshot()["provider_request_count"] == rounds
            assert llm.usage_ledger.snapshot()["cost"] == pytest.approx(rounds * 0.01)
            serialized = json.dumps(records)
            assert "opaque-1" not in serialized
            assert "fixture stable" not in serialized
        finally:
            core.close()
            llm.close()
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["dynamic", "tools", "compaction", "profile"])
def test_complete_chain_changes_are_explained_without_rewriting_active_history(mutation, tmp_path):
    test_real_compiler_executor_hook_codec_same_turn_chain(PROFILES[1], 8, tmp_path, mutation=mutation)


def test_bad_profile_does_not_fallback_or_count_a_provider_attempt(tmp_path):
    class Transport:
        def frames(self, *_args):
            raise AssertionError("invalid configuration must not call any provider")
    ep = endpoint("typo")
    llm = runtime(ep, Transport(), tmp_path)
    result = llm.generate(_request())
    assert "unknown cache_profile" in result.response.text
    assert llm.usage_ledger.snapshot()["provider_request_count"] == 0


def test_old_response_cannot_update_live_epoch_observations():
    from tests.test_prompt_cache_evidence import _active_tool_request, _extend_active_tool_request
    from pal.llm.shapes.openai_response import OpenAIResponseCodec
    coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
    context = _astra_context()
    first = _active_tool_request(_request(), suffix="one")
    old = coordinator.plan(first, context)
    second = _extend_active_tool_request(first, suffix="two")
    new = coordinator.plan(second, context)
    codec = OpenAIResponseCodec()
    for plan, request in ((new, second), (old, first)):
        encoded = coordinator.inject(codec.encode(request, context), plan)
        coordinator.record_success(plan, usage_from_mapping({"input_tokens": 90000,
            "input_tokens_details": {"cached_tokens": 8000 if plan is new else 1, "cache_write_tokens": 0}}),
            applied_cache_breakpoint_message_ids=encoded.applied_cache_breakpoint_message_ids,
            actual_provider="fixture", prefix_preserved=True)
    assert coordinator.snapshot()["frontier"]["submitted_prefix_tokens"] == new.frontier.target_prefix_tokens
    assert coordinator.snapshot()["observation"]["last_observed_sequence"] == new.plan_sequence


def test_provider_sequences_and_final_settlement_ignore_replayed_intermediate():
    evidence = WireResponseEvidence(WireShape.OPENAI_RESPONSE)
    evidence.observe(_JSONFrame(0, {"type": "response.completed", "sequence_number": 9,
        "response": {"id": "generation", "usage": {"input_tokens": 50, "cost": 0.1}}}))
    evidence.observe(_JSONFrame(1, {"type": "response.created", "sequence_number": 1,
        "response": {"id": "generation", "usage": {"input_tokens": 100, "cost": 0.2}}}))
    assert evidence.usage.input_tokens == 50
    assert evidence.usage.cost == 0.1


def test_output_recovery_costs_are_not_added_again_at_logical_completion(tmp_path):
    class Transport:
        calls = 0
        def frames(self, *_args):
            self.calls += 1
            payload = response_payload(self.calls)
            if self.calls == 1:
                payload["status"] = "incomplete"
                payload["incomplete_details"] = {"reason": "max_output_tokens"}
            yield _JSONFrame(0, payload)
    ep = endpoint()
    ep.capabilities_blob["max_output_recovery"] = {"enabled": True, "upper_limit": 4096, "max_continuations": 1}
    transport = Transport()
    llm = runtime(ep, transport, tmp_path)
    result = llm.generate(replace(_request(), metadata={"max_output_recovery_enabled": True}))
    assert transport.calls == 2
    assert len(result.response.attempt_ids) == 2
    assert llm.usage_ledger.snapshot()["provider_request_count"] == 2
    assert llm.usage_ledger.snapshot()["cost"] == pytest.approx(0.02)


def test_partial_usage_preserves_uncorrected_counter_anomalies():
    initial = usage_from_mapping({"input_tokens": 100,
        "input_tokens_details": {"cached_tokens": -20, "cache_write_tokens": 0}})
    partial = merge_usage(initial, usage_from_mapping({"output_tokens": 5}, final=True))
    assert "negative:input_tokens_details.cached_tokens" in partial.usage_anomaly
    assert dict(partial.raw_counters)["input_tokens_details.cached_tokens"] == -20
    corrected = merge_usage(partial, usage_from_mapping({
        "input_tokens_details": {"cached_tokens": 20}}, final=True))
    assert not corrected.usage_anomaly
    assert corrected.cached_input_tokens == 20
