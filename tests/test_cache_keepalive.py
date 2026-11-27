from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pal.cache_reminder.cache_warm_deadline import CacheWarmDeadlineManager
from pal.cache_reminder.runtime import _cache_policy_command
from pal.cache_reminder.settings import configure_cache_reminder_settings, read_cache_reminder_settings
from pal.control.contracts import ControlCommandInvocation
from pal.llm.cache_capabilities import KeepaliveProfile, keepalive_profile
from pal.llm.cache_keepalive import CacheKeepalive, action_budget
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.ir import WireShape
from pal.llm.response_evidence import WireResponseEvidence
from pal.llm.shapes.anthropic_messages import AnthropicMessagesCodec
from pal.llm.shapes.base import EncodedRequest, ShapeContext, _JSONFrame
from pal.llm.shapes.openai_response import OpenAIResponseCodec
from pal.llm.transport_contracts import LLMStreamControl
from pal.llm.usage import LLMUsageLedger
from pal.llm.usage_normalization import usage_from_mapping
from pal.shared.json_values import thaw_json
from tests.cache_reminder_factory import _Settings
from tests.test_cache_warm_deadline import _ControlledTimers, _capture, _route
from tests.test_prompt_cache_evidence import _request
from tests.test_prompt_cache_v2_runtime import endpoint, response_payload


@pytest.mark.parametrize("read,write,expected", [(.05, 1.25, 19), (.1, 1., 8), (.1, 1.25, 10), (.1, 2., 18)])
def test_budget(read, write, expected):
    assert action_budget(KeepaliveProfile(read, write, 5., 1800, 16, 2.)) == expected


def test_codec_only_changes_output_budget_and_suffix():
    ctx = ShapeContext(WireShape.OPENAI_RESPONSE, "ep", "gpt-6.2-sol", base_url="https://api.openai.com/v1")
    original = EncodedRequest({"input": [{"role": "user", "content": [{"type": "input_text", "text": "x", "prompt_cache_breakpoint": {"mode": "explicit"}}]}],
        "reasoning": {"effort": "max"}, "max_output_tokens": 999, "store": False}, extra_body={"prompt_cache_key": "a"}, instructions_are_suffix=True)
    ping = OpenAIResponseCodec().prepare_keepalive(original, ctx)
    assert ping is not None
    assert ping.payload["input"] == original.payload["input"]
    assert ping.payload["reasoning"] == original.payload["reasoning"]
    assert ping.extra_body == original.extra_body
    assert ping.payload["max_output_tokens"] == 16
    assert "PING" in ping.payload["instructions"]
    assert "instructions" not in original.payload
    older = OpenAIResponseCodec().prepare_keepalive(replace(original, instructions_are_suffix=False), ctx)
    assert older is not None and older.payload["input"][:-1] == original.payload["input"]


def test_claude_gateway_one_token_and_direct_zero():
    original = EncodedRequest({"messages": [{"role": "user", "content": "x"}], "max_tokens": 100,
        "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}})
    direct = ShapeContext(WireShape.ANTHROPIC_MESSAGES, "a", "claude-opus-5-5", base_url="https://api.anthropic.com")
    gateway = replace(direct, base_url="https://openrouter.ai/api", model_id="anthropic/claude-opus-5.5")
    for ctx, limit in ((direct, 0), (gateway, 1)):
        ping = AnthropicMessagesCodec().prepare_keepalive(original, ctx)
        assert ping is not None and ping.payload["max_tokens"] == limit
        assert ping.payload["thinking"] == original.payload["thinking"]
        assert ping.payload["messages"][:1] == original.payload["messages"]
    assert AnthropicMessagesCodec().prepare_keepalive(replace(original, payload={**original.payload, "thinking": {"type": "enabled"}}), direct) is None
    evidence = WireResponseEvidence(WireShape.ANTHROPIC_MESSAGES)
    evidence.observe(_JSONFrame(1, {"type": "message", "content": [], "usage": {"input_tokens": 1, "output_tokens": 0, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0}}))
    assert evidence.usage.final


def usage(read, write=0, output=5):
    return usage_from_mapping({"input_tokens": 40_010, "output_tokens": output,
        "input_tokens_details": {"cached_tokens": read, "cache_write_tokens": write}}, final=True)


class Transport:
    def __init__(self):
        self.requests = []
        self.usage = [usage(0, 40_000), usage(40_000), usage(10, 39_990)]

    def frames(self, _endpoint, request):
        self.requests.append(request)
        request.on_submitted and request.on_submitted()
        u = self.usage.pop(0)
        payload = response_payload(len(self.requests), usage={"input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens, "input_tokens_details": {"cached_tokens": u.cached_input_tokens,
            "cache_write_tokens": u.cache_write_input_tokens}})
        yield _JSONFrame(1, payload)


def test_invoker_keeps_exact_request_and_separates_usage():
    ep = endpoint()
    ep.model_id = "openai/gpt-6.2-sol"
    ep.capabilities_blob = {"prompt_cache": {"mode": "explicit"}}
    transport, ledger = Transport(), LLMUsageLedger()
    invoker = ShapeEndpointInvoker(transport=transport, attempt_sink=ledger.record_attempt)
    request = replace(_request(), metadata={"turn_id": "turn-a"})
    invoker.invoke(ep, request)
    before = ledger.snapshot()
    state = invoker.keepalive_snapshot(ep)
    assert state["eligible"] and state["prefix_tokens"] == 40_000
    epoch = state["anchor_epoch"]
    assert invoker.invoke_keepalive(ep, epoch, LLMStreamControl())
    after = ledger.snapshot()
    assert before["input_tokens"] == after["input_tokens"]
    assert after["keepalive"]["cached_input_tokens"] == 40_000
    first, ping = transport.requests
    assert thaw_json(first.payload)["input"] == thaw_json(ping.payload)["input"]
    assert thaw_json(first.extra_body) == thaw_json(ping.extra_body)
    assert not invoker.invoke_keepalive(ep, epoch, LLMStreamControl())
    assert not invoker.keepalive_snapshot(ep)["eligible"]
    assert not invoker.invoke_keepalive(ep, epoch, LLMStreamControl())
    assert len(transport.requests) == 3


def test_provider_and_mode_binding():
    spec = SimpleNamespace(auth_kind="api_key_ref", transport="http", model_id="gpt-6.2-sol", base_url="https://api.openai.com/v1", wire_shape="openai_response")
    assert keepalive_profile(spec, ("30m",)) is not None
    claude = SimpleNamespace(auth_kind="api_key_ref", transport="http", model_id="anthropic/claude-opus-5.5", base_url="https://openrouter.ai/api", wire_shape="anthropic_messages")
    profile = keepalive_profile(claude, ("5m", "1h"))
    assert profile is not None and profile.read_multiplier == .05 and profile.input_price == 4. and profile.output_limit == 1
    spec.auth_kind = "oauth"
    assert keepalive_profile(spec, ("30m",)) is None
    spec.auth_kind = "api_key_ref"
    spec.base_url = "https://unknown.test/v1"
    assert keepalive_profile(spec, ("30m",)) is None


def test_slash_mode_persists_and_invalid_choice_rejected():
    settings = _Settings()
    assert read_cache_reminder_settings(settings).mode == "reminder"
    assert configure_cache_reminder_settings(settings, mode="keep-alive").mode == "keep-alive"
    assert read_cache_reminder_settings(settings).mode == "keep-alive"
    assert _cache_policy_command(ControlCommandInvocation("cache_policy", ("keep-alive",))).args == {"mode": "keep-alive"}
    assert _cache_policy_command(ControlCommandInvocation("cache_policy", ("other",))).action_kind == "invalid_command"


def test_timer_rearms_on_renewal_but_user_activity_cancels():
    async def run():
        settings, timers, notices, steps = _Settings(), _ControlledTimers(), [], []
        configure_cache_reminder_settings(settings, mode="keep-alive", lead_seconds=300)
        state = {"eligible": True, "estimated": True, "anchor_epoch": "epoch", "turn_id": "turn",
                 "anchor_ttl_seconds": 300, "anchor_remaining_ttl_seconds": 300,
                 "prefix_tokens": 40_000, "decision": "keepalive"}
        async def step(notice, decision):
            steps.append(decision)
            return True
        manager = CacheWarmDeadlineManager(lambda: state, lambda: settings, lambda: False,
            lambda n: _capture(notices, n), lambda n: _capture([], n), timers,
            keepalive_snapshot=lambda: state, keepalive_step=step)
        assert manager.schedule_after_turn_commit(route=_route(), turn_id="turn")
        assert timers.calls == [240, 300]
        await timers.release(0)
        await asyncio.sleep(0)
        assert steps == ["keepalive"] and not notices
        assert timers.calls == [240, 300, 240, 300]
        await manager.clear_for_user_activity()
        await timers.release(2)
        assert steps == ["keepalive"]
    asyncio.run(run())


def test_compact_permit_is_single_use_and_checks_effort_and_marker():
    import time
    anchor = CacheKeepalive()
    encoded = EncodedRequest({"model": "gpt-6.2-sol", "input": [{"role": "user", "content": [
        {"type": "input_text", "text": "frozen", "prompt_cache_breakpoint": {"mode": "explicit"}}]}],
        "reasoning": {"effort": "high"}, "max_output_tokens": 100},
        extra_body={"prompt_cache_key": "a", "prompt_cache_options": {"mode": "explicit", "ttl": "30m"}}, instructions_are_suffix=True)
    anchor.capture(epoch="e", endpoint_id="a", binding="b", scope_key="s", turn_id="t", encoded=encoded,
        profile=KeepaliveProfile(.05, 1.25, 5., 1800, 16, 2.), usage=usage(0, 40000),
        started_at=time.monotonic(), provider="")
    for _ in range(18):
        a = anchor.begin("e", "b")
        assert a is not None
        assert anchor.settle(a, usage(40000), started_at=time.monotonic(), accepted=True, provider="")
    assert anchor.snapshot("b")["decision"] == "compact"
    assert anchor.claim_compact("e", "b") and not anchor.claim_compact("e", "b")
    compact = replace(encoded, payload={**encoded.payload, "max_output_tokens": 16000,
        "input": [*encoded.payload["input"], {"role": "user", "content": "compact"}]})
    assert anchor.prove_compact("e", "b", compact)
    assert not anchor.prove_compact("e", "b", replace(compact, payload={**compact.payload, "reasoning": {"effort": "low"}}))
    assert not anchor.prove_compact("e", "other", compact)
    anchor.clear()
    assert not anchor.prove_compact("e", "b", compact)


def test_final_compaction_uses_real_memory_and_generation_path(tmp_path):
    from pal.llm.contracts import collect_generation
    from pal.llm.ir import GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole, PromptRegionIR, TextPartIR
    from pal.memory import MemoryService
    from tests.core_factory import build_core
    from tests.test_prompt_cache_v2_runtime import runtime, summary_response_payload

    class CompactTransport:
        def __init__(self):
            self.requests = []

        def frames(self, _endpoint, request):
            self.requests.append(request)
            request.on_submitted and request.on_submitted()
            index = len(self.requests)
            counters = {"input_tokens": 40_010, "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 40_000 if index > 1 else 0,
                    "cache_write_tokens": 40_000 if index == 1 else 0}}
            if "pal.compaction.continuity.v1" in str(request.payload):
                yield _JSONFrame(1, summary_response_payload(index, usage=counters))
            else:
                yield _JSONFrame(1, response_payload(index, usage=counters))

    async def run():
        ep = endpoint()
        ep.model_id = "openai/gpt-6.2-sol"
        ep.capabilities_blob = {"prompt_cache": {"mode": "explicit"}}
        transport = CompactTransport()
        llm = runtime(ep, transport, tmp_path)
        core = build_core()
        memory = MemoryService()
        core.context.port_registry["llm:llm"] = llm
        try:
            memory.begin_l1_turn("warm", user_text="original user input " * 900)
            user = memory.active_l1_turn("warm").messages[0]
            request = LLMRequestIR(messages=(
                LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("stable " * 900),), prompt_region=PromptRegionIR.STABLE_SYSTEM),
                replace(user, prompt_region=PromptRegionIR.ACTIVE_INPUT)), tools=(),
                policy=GenerationPolicyIR(max_output_tokens=1024), logical_scope_id="pal:resident",
                metadata={"turn_id": "warm"})
            outcome = await collect_generation(llm.generate(request))
            memory.upsert_l1_assistant("warm", outcome.response.message)
            memory.settle_l1_turn("warm")
            epoch = llm.prompt_cache_keepalive_snapshot()["anchor_epoch"]
            for _ in range(18):
                assert await llm.keepalive_once(epoch)
            assert llm.claim_keepalive_compact(epoch)
            result = await core.turn_executor.compaction.run(memory, target_input_budget=16384,
                reserved_output_tokens=1024, preferred_endpoint_id=ep.endpoint_id,
                max_attempts=1, force_semantic=True, keepalive_compact_epoch=epoch,
                replay_guard=lambda: llm.keepalive_compact_valid(epoch))
            assert result.success, (result.status, result.failures)
            assert len(transport.requests) == 20
        finally:
            core.close()
            llm.close()
    asyncio.run(run())


def test_renewal_keeps_date_pin_warm_beyond_original_expiry(monkeypatch):
    from pal.llm import cache_reminder
    clock = [100.]
    monkeypatch.setattr(cache_reminder, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    tracker = cache_reminder.CacheReminderTracker()
    basis = cache_reminder.CacheReminderBasis("pal:resident", "binding", "ep", "gpt-6.2-sol", "openai",
        1800, "request_cache_ttl", 40_000, clock[0], "epoch")
    tracker.record_success(basis, usage(40_000), maximum=1)
    clock[0] = 1600.
    tracker.renew("epoch", started_at=clock[0])
    clock[0] = 1901.
    snap = tracker.snapshot(logical_scope_id="pal:resident", binding="binding")
    assert snap["anchor_epoch"] == "epoch" and snap["eligible"]
    assert not cache_reminder.prefix_refresh_likely_cold(snap, endpoint_id="ep", model_id="gpt-6.2-sol")
    clock[0] = 3401.
    snap = tracker.snapshot(logical_scope_id="pal:resident", binding="binding")
    assert cache_reminder.prefix_refresh_likely_cold(snap, endpoint_id="ep", model_id="gpt-6.2-sol")


def test_omitted_write_counter_does_not_prove_zero_writes():
    import time
    state = CacheKeepalive()
    wire = EncodedRequest({"input": ["unchanged"]})
    state.capture(epoch="e", endpoint_id="a", binding="b", scope_key="s", turn_id="t", encoded=wire,
        profile=KeepaliveProfile(.05, 1.25, 5., 1800, 16, 2.), usage=usage(0, 40000),
        started_at=time.monotonic(), provider="")
    anchor = state.begin("e", "b")
    assert anchor is not None
    missing_write = usage_from_mapping({"input_tokens": 40_010, "output_tokens": 5,
        "input_tokens_details": {"cached_tokens": 40_000}}, final=True)
    assert not state.settle(anchor, missing_write, started_at=time.monotonic(), accepted=True, provider="")
    assert state.snapshot("b")["reason"] == "renewal_not_confirmed"


def test_inflight_heartbeat_cancelled_by_user_activity():
    async def run():
        settings, timers, notices = _Settings(), _ControlledTimers(), []
        configure_cache_reminder_settings(settings, mode="keep-alive")
        state = {"eligible": True, "estimated": True, "anchor_epoch": "epoch", "turn_id": "turn",
                 "anchor_ttl_seconds": 300, "anchor_remaining_ttl_seconds": 300,
                 "prefix_tokens": 40_000, "decision": "keepalive"}
        cancelled = asyncio.Event()
        async def step(notice, decision):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        manager = CacheWarmDeadlineManager(lambda: state, lambda: settings, lambda: False,
            lambda n: _capture(notices, n), lambda n: _capture([], n), timers,
            keepalive_snapshot=lambda: state, keepalive_step=step)
        assert manager.schedule_after_turn_commit(route=_route(), turn_id="turn")
        await timers.release(0)
        await asyncio.sleep(0)
        await manager.clear_for_user_activity()
        await asyncio.sleep(0)
        assert cancelled.is_set() and not manager.inspect()["timer_scheduled"] and not notices
    asyncio.run(run())


def test_keepalive_fallback_notice_expires_if_activity_arrives_during_delivery():
    async def run():
        settings, timers, expired = _Settings(), _ControlledTimers(), []
        configure_cache_reminder_settings(settings, mode="keep-alive")
        state = {"eligible": True, "estimated": True, "anchor_epoch": "epoch", "turn_id": "turn",
                 "anchor_ttl_seconds": 300, "anchor_remaining_ttl_seconds": 300,
                 "prefix_tokens": 40_000, "decision": "keepalive"}
        delivering, release = asyncio.Event(), asyncio.Event()
        async def step(notice, decision):
            return False
        async def deliver(notice):
            delivering.set()
            await release.wait()
            return True
        manager = CacheWarmDeadlineManager(lambda: state, lambda: settings, lambda: False,
            deliver, lambda n: _capture(expired, n), timers, keepalive_snapshot=lambda: state, keepalive_step=step)
        assert manager.schedule_after_turn_commit(route=_route(), turn_id="turn")
        await timers.release(0)
        await delivering.wait()
        await manager.clear_for_user_activity()
        release.set()
        await asyncio.sleep(0)
        assert len(expired) == 1 and manager._active_notice is None
    asyncio.run(run())
