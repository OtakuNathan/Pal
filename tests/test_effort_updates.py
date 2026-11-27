"""Thinking selection follows durable resident history, rather than live settings."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import pytest

from pal.core.capabilities import register_with_core
from pal.core.turns import LLMPreflightEffect, LLMRequestEffect, TurnContinuation
from pal.foundation.io import EventEnvelope
from pal.llm.cache_capabilities import ANTHROPIC_EFFORT_UPDATE_BETA, supports_effort_updates
from pal.llm.cache_wire import clean_request
from pal.llm.compaction import NATIVE_WINDOW_KEY
from pal.llm.effort_updates import EFFORT_PIN, EFFORT_UPDATE, binding
from pal.llm.ir import GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole, TextPartIR, WireShape
from pal.llm.request_hooks import apply_provider_request_hooks
from pal.llm.serde import message_from_payload, message_to_payload
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import EncodedRequest, ShapeContext, _JSONFrame
from pal.memory import MemoryService, register_with_core as register_memory
from pal.memory.effort_pin import plan_effort_pin, pinned_think_levels
from pal.memory.runtime_state import MemoryRuntimeStatePort
from pal.shared import PromptAssemblyContext
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolResultIR
from tests.core_factory import build_core
from tests.test_llm_endpoint_runtime_contract import _endpoint
from tests.test_prompt_cache_v2_runtime import runtime, response_payload
from tests.test_v3_n1_root_lifecycle import Candidate


def message(role, text, **kwargs):
    return LLMMessageIR(role, (TextPartIR(text),), **kwargs)


def context(shape, model, url):
    return ShapeContext(shape, "astra", model, base_url=url)


OPENAI = context(WireShape.OPENAI_RESPONSE, "openai/gpt-6.2-sol", "https://openrouter.ai/api/v1")
CLAUDE = context(WireShape.ANTHROPIC_MESSAGES, "anthropic/claude-opus-5.5", "https://openrouter.ai/api")
DEEPSEEK = context(WireShape.OPENAI_RESPONSE, "deepseek-flash", "https://api.deepseek.com")
GEMINI = context(WireShape.GEMINI_INTERACTIONS, "gemini-4-hydrogen", "https://generativelanguage.googleapis.com/v1beta")
GLM = context(WireShape.ANTHROPIC_MESSAGES, "glm-5.4", "https://open.bigmodel.cn/api/anthropic")


def facts(ctx):
    return {**binding({"endpoint_id": ctx.endpoint_id, "model_id": ctx.model_id, "wire_shape": ctx.wire_shape}),
            "thinking_levels": ["low", "high"],
            "supports_effort_updates": supports_effort_updates(ctx.wire_shape, ctx.model_id, ctx.base_url, ctx.capabilities)}


def admit(memory, ctx, turn_id, desired):
    pin, update = plan_effort_pin(memory, facts(ctx), desired)
    turn = memory.begin_l1_turn(turn_id, user_message=message(MessageRole.USER, turn_id,
        metadata={EFFORT_UPDATE: update} if update else {}), metadata={EFFORT_PIN: pin})
    return turn


def settle(memory, turn_id):
    memory.settle_l1_turn(turn_id)
    memory.history_root.promote()


def compact(memory, *, success=True):
    root = memory.history_root
    run_id = "compact-ok" if success else "compact-cancelled"
    root.begin_compact(run_id, reason="test")
    if success:
        root.mark_ready(run_id, Candidate())
        root.commit(run_id)
    else:
        root.cancel(run_id)


@pytest.mark.parametrize("ctx, supported", [
    (OPENAI, True), (CLAUDE, True), (DEEPSEEK, False), (GEMINI, False), (GLM, False),
    (context(WireShape.OPENAI_RESPONSE, "gpt-6.2-sol", "https://example.test/v1"), False),
    (context(WireShape.OPENAI_RESPONSE, "gpt-6.2-sol", "https://api.openai.com/v1"), True),
])
def test_effort_pin_survives_promotion_and_recovery_then_compact(ctx, supported):
    memory = MemoryService()
    first = admit(memory, ctx, "first", "low")
    settle(memory, first.turn_id)
    second = admit(memory, ctx, "second", "high")
    assert second.metadata[EFFORT_PIN]["base"] == "low"
    assert second.metadata[EFFORT_PIN]["effective"] == ("high" if supported else "low")
    assert bool(second.messages[0].metadata.get(EFFORT_UPDATE)) is supported
    settle(memory, second.turn_id)
    # Snapshot/restore preserves both the root generation and chronological event.
    saved = MemoryRuntimeStatePort(memory).snapshot_state()
    restored = MemoryService()
    port = MemoryRuntimeStatePort(restored)
    port.install_prepared_state(port.prepare_restore_state(saved))
    third = admit(restored, ctx, "third", "high")
    assert third.metadata[EFFORT_PIN]["base"] == "low"
    assert EFFORT_UPDATE not in third.messages[0].metadata
    settle(restored, third.turn_id)
    compact(restored, success=False)
    fourth = admit(restored, ctx, "fourth", "high")
    assert fourth.metadata[EFFORT_PIN]["base"] == "low"
    settle(restored, fourth.turn_id)
    # Only a successful compact allows a new root, not promotion, interruption
    # or a failed compaction.
    compact(restored)
    final = admit(restored, ctx, "final", "high")
    assert final.metadata[EFFORT_PIN]["base"] == "high"
    assert EFFORT_UPDATE not in final.messages[0].metadata


@pytest.mark.parametrize("ctx", [OPENAI, CLAUDE])
def test_codec_and_projection_preserve_event_position_and_root(ctx):
    from pal.llm.projection_contracts import EndpointBinding, LogicalSessionId
    from pal.llm.projection_session import EndpointProjectionSession, HistoryView
    from tests.test_projection_two_segment import _attempt

    req = LLMRequestIR((
        message(MessageRole.SYSTEM, "stable"),
        message(MessageRole.USER, "first"),
        message(MessageRole.ASSISTANT, "answer"),
        message(MessageRole.USER, "second", metadata={EFFORT_UPDATE: {**binding(facts(ctx)), "effort": "high"}}),
    ), (), GenerationPolicyIR(max_output_tokens=128, thinking_level="low"))
    codec = codec_for_shape(ctx.wire_shape)
    # Round-trip the precise L1 user record, not a provider-shaped history rewrite.
    req = replace(req, messages=(*req.messages[:-1], message_from_payload(message_to_payload(req.messages[-1]))))
    raw = codec.encode(req, ctx)
    container = "input" if ctx.wire_shape == WireShape.OPENAI_RESPONSE else "messages"
    event = ({"type": "configuration_update", "reasoning": {"effort": "high"}}
             if ctx.wire_shape == WireShape.OPENAI_RESPONSE else
             {"role": "system", "content": [], "output_config": {"effort": "high"}})
    assert raw.payload[container][-2] == event
    assert raw.payload[container][-1]["role"] == "user"
    assert raw.payload["reasoning" if container == "input" else "output_config"]["effort"] == "low"
    assert raw.message_spans[-1].wire_item_paths == ((container, len(raw.payload[container])-2), (container, len(raw.payload[container])-1))
    session = EndpointProjectionSession(LogicalSessionId("effort-test"))
    session.bind(EndpointBinding(ctx.endpoint_id, ctx.model_id, ctx.wire_shape, "spec", "policy", "cfg",
                                 base_url=ctx.base_url, provider_id=ctx.provider_id), capabilities=ctx.capabilities)
    session.begin_round(_attempt(session, "one"), requires_native=False)
    projected = session.prepare(HistoryView(session.frontier, req.messages[1:]), request_shell=req)
    assert json.loads(projected.payload_json) == thaw_json(raw.payload)
    if ctx == CLAUDE:
        encoded = apply_provider_request_hooks(raw, ctx)
        assert ANTHROPIC_EFFORT_UPDATE_BETA in encoded.payload["extra_headers"]["anthropic-beta"]


@pytest.mark.parametrize("ctx", [OPENAI, CLAUDE, DEEPSEEK])
def test_resident_executor_keeps_root_during_turn_and_updates_only_next_user_turn(ctx, tmp_path):
    async def run():
        class Transport:
            requests = []

            def frames(self, _endpoint, request):
                self.requests.append(request)
                n = len(self.requests)
                if ctx == CLAUDE:
                    blocks = ([{"type": "tool_use", "id": "call-1", "name": "fixture_probe", "input": {}}]
                              if n == 1 else [{"type": "text", "text": "done"}])
                    yield _JSONFrame(0, {"type": "message", "id": f"msg-{n}", "role": "assistant",
                        "model": ctx.model_id, "content": blocks,
                        "stop_reason": "tool_use" if n == 1 else "end_turn",
                        "usage": {"input_tokens": 500, "output_tokens": 5}})
                else:
                    yield _JSONFrame(0, {**response_payload(n, tool=n == 1, usage={"input_tokens": 500, "output_tokens": 5}), "model": ctx.model_id})

        ep = _endpoint("astra", model_id=ctx.model_id)
        ep.wire_shape, ep.base_url = ctx.wire_shape.value, ctx.base_url
        ep.provider = "openrouter" if ctx in (OPENAI, CLAUDE) else "deepseek"
        ep.thinking_levels_blob, ep.default_thinking_level = ["low", "high"], "low"
        ep.supports_streaming = False
        ep.context_window = 100_000
        transport = Transport()
        llm = runtime(ep, transport, tmp_path)
        core = build_core()
        register_with_core(core)
        memory = MemoryService()
        register_memory(core.context, memory)
        core.context.port_registry["llm:llm"] = llm
        executor = core.turn_executor
        tools = [{"type": "function", "function": {"name": "fixture_probe", "description": "Fixture",
                                                  "parameters": {"type": "object", "properties": {}}}}]

        def turn(num):
            event = EventEnvelope("user_message", "test", f"User turn {num}", event_id=f"turn-{num}")
            return (TurnContinuation(event.event_id, iter(()), "fixture",
                        turn_settings_snapshot=core.turn_manager._build_turn_settings_snapshot()),
                    PromptAssemblyContext(event=event))

        async def round(c, assembly):
            await executor.execute_turn_effect_async(c, LLMPreflightEffect(assembly_context=assembly,
                                                       max_output_tokens=128, tools_override=tools))
            result = await executor.execute_turn_effect_async(c, LLMRequestEffect(assembly_context=assembly,
                                                       max_output_tokens=128, tools_override=tools))
            return result.payload.response

        try:
            c, assembly = turn(1)
            response = await round(c, assembly)
            call = response.tool_calls[0]
            llm.set_think_level("high")
            from pal.llm.capabilities import LLMIntrospectionProvider
            from pal.memory.contracts import MEMORY
            inspector = LLMIntrospectionProvider(llm, memory=core.context.port_registry.observe(MEMORY))
            status = inspector._thinking_snapshot()
            assert status["requested"] == "high" and status["applied"] == "low" and status["pending"]
            memory.append_l1_tool_result(c.turn_id, ToolResultIR(call_id=call.call_id, name=call.name, content="success"))
            await round(c, assembly)
            settle(memory, c.turn_id)
            c, assembly = turn(2)
            await round(c, assembly)
            status = inspector._thinking_snapshot()
            assert status["applied"] == ("low" if ctx == DEEPSEEK else "high")
            assert status["root_effort"] == "low"
            settle(memory, c.turn_id)
            c, assembly = turn(3)
            await round(c, assembly)
            settle(memory, c.turn_id)
            # No previous wire bytes are rewritten by the effort changes.
            payloads = [thaw_json(clean_request(EncodedRequest(r.payload)).payload) for r in transport.requests]
            field = "output_config" if ctx == CLAUDE else "reasoning"
            container = "messages" if ctx == CLAUDE else "input"
            assert len(payloads) == 4
            assert all(p[field]["effort"] == "low" for p in payloads)
            def updates(p):
                return [item for item in p[container] if item.get("type") == "configuration_update"
                        or "output_config" in item]
            assert not updates(payloads[0]) and not updates(payloads[1])
            expected = 0 if ctx == DEEPSEEK else 1
            assert len(updates(payloads[2])) == len(updates(payloads[3])) == expected
            if expected:
                assert updates(payloads[2]) == updates(payloads[3])
            for before, after in zip(payloads, payloads[1:]):
                assert after[container][:len(before[container])] == before[container]
            if ctx == DEEPSEEK:
                compact(memory)
                c, assembly = turn(4)
                await round(c, assembly)
                assert transport.requests[-1].payload[field]["effort"] == "high"
            else:
                llm.set_think_level("low")
                c, assembly = turn(4)
                await round(c, assembly)
                final = thaw_json(clean_request(EncodedRequest(transport.requests[-1].payload)).payload)
                assert final[field]["effort"] == "low"
                assert len(updates(final)) == 2
                assert updates(final)[0] == updates(payloads[-1])[0]
                assert final[container][:len(payloads[-1][container])] == payloads[-1][container]
        finally:
            core.close()
            llm.close()

    asyncio.run(run())


def test_native_compact_strips_only_submitted_updates_and_restores_effort():
    from tests.test_model_compaction_strategies import runtime as native_runtime, request, user, assistant, native_output
    from pal.llm.shapes.base import ShapeContext

    rt, ep, transport = native_runtime(model="gpt-6.2-sol", capabilities_blob={"prompt_cache": {"mode": "explicit"}})
    ctx = ShapeContext(WireShape.OPENAI_RESPONSE, ep.endpoint_id, ep.model_id, base_url=ep.base_url)
    update = {"type": "configuration_update", "reasoning": {"effort": "high"}}
    req = request(user("first"), assistant(), user("next", metadata={
        EFFORT_UPDATE: {**binding(facts(ctx)), "effort": "high"}}), assistant())
    req = replace(req, policy=replace(req.policy, thinking_level="low"))
    replay, _, _ = asyncio.run(rt.acompact_native(req))
    compact_record, = [r for r in transport.requests if r.operation == "compact"]
    assert not any(item.get("type") == "configuration_update" for item in compact_record.payload["input"])
    assert thaw_json(replay.payload["output"]) == native_output()
    assert replay.payload[NATIVE_WINDOW_KEY][EFFORT_PIN]["effective"] == "high"
    seed = message(MessageRole.ASSISTANT, "", replay=replay)
    successor = replace(req, messages=(seed, user("continue")))
    wire = rt.endpoint_invoker.encode_request(ep, successor).payload
    assert thaw_json(wire["input"])[:-1] == [*native_output(), update]
    original = rt.endpoint_invoker.encode_request(ep, req).payload
    assert original["input"][-3] == update


@pytest.mark.parametrize("ctx", [OPENAI, DEEPSEEK])
def test_compact_during_active_turn_keeps_right_input_and_turn_effort(ctx):
    memory = MemoryService()
    first = admit(memory, ctx, "first", "low")
    settle(memory, first.turn_id)
    active = admit(memory, ctx, "active", "high")
    compact(memory)
    retained = memory.active_l1_turn(active.turn_id)
    assert retained.messages == active.messages
    assert pinned_think_levels(memory, facts(ctx), {"astra": "high"}, active_turn=retained)["astra"] == "low"
    settle(memory, active.turn_id)
    next_turn = admit(memory, ctx, "next", "high")
    assert next_turn.metadata[EFFORT_PIN]["base"] == ("low" if ctx == OPENAI else "high")
    assert EFFORT_UPDATE not in next_turn.messages[0].metadata


def test_mode_changes_are_deferred_even_on_update_capable_endpoint():
    memory = MemoryService()
    route = {**facts(OPENAI), "thinking_levels": ["off", "low", "high"]}
    first = admit(memory, OPENAI, "first", "low")
    settle(memory, first.turn_id)
    pending, update = plan_effort_pin(memory, route, "off")
    assert (pending["base"], pending["effective"], update) == ("low", "low", None)
    compact(memory)
    next_pin, update = plan_effort_pin(memory, route, "off")
    assert (next_pin["base"], next_pin["effective"], update) == ("off", "off", None)
