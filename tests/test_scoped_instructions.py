"""Scoped operator instructions are not ordinary mutable historical messages."""
from dataclasses import replace
import json

import pytest

from pal.llm.cache_capabilities import (
    ANTHROPIC_CLEAR_AT_BETA, supports_interaction_instructions, supports_response_instructions,
    supports_request_instructions, supports_suffix_instructions, supports_turn_scoped_instructions,
)
from pal.llm.ir import (
    GenerationPolicyIR, InstructionScopeIR, LLMMessageIR, LLMRequestIR, MessageRole,
    PromptRegionIR, TextPartIR, WireShape,
)
from pal.llm.context_budget import count_payload
from pal.llm.request_hooks import apply_provider_request_hooks
from pal.llm.runtime import _estimate_request_tokens
from pal.llm.serde import message_from_payload, message_to_payload
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext, ShapeDecodeError
from pal.llm.projection_contracts import EndpointBinding, LogicalSessionId, AttemptKey
from pal.llm.projection_session import EndpointProjectionSession, HistoryView
from pal.memory.context_view import projected_messages
from pal.memory.prompt_context import prepare_context, applicable_context
from pal.memory.turn_ir import L1TurnIR
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
from pal.shared.json_values import thaw_json


def message(role, text, region=PromptRegionIR.SETTLED_HISTORY, **kw):
    return LLMMessageIR(role, (TextPartIR(text),), prompt_region=region, **kw)


def scoped(text="temporary instruction"):
    return message(MessageRole.DEVELOPER, text, PromptRegionIR.ACTIVE_DYNAMIC,
                   instruction_scope=InstructionScopeIR.NEXT_USER_MESSAGE,
                   semantic_kind="pal_prompt_context", metadata={"pal_authored": True, "scope_turn_id": "old"})


def request(*messages, instructions=""):
    return LLMRequestIR(messages, (), GenerationPolicyIR(max_output_tokens=128, thinking_level="low"),
                        request_instructions=instructions)


def claude(mode="turn_scoped", provider="openrouter", model="anthropic/claude-opus-5.5", url="https://openrouter.ai/api"):
    return ShapeContext(WireShape.ANTHROPIC_MESSAGES, "claude", model, provider, url,
                        {"request_instruction_mode": mode, "prompt_cache": {"mode": "explicit"}})


def gemini(mode="interaction"):
    return ShapeContext(WireShape.GEMINI_INTERACTIONS, "gemini", "gemini-4-hydrogen", "google",
                        "https://generativelanguage.googleapis.com/v1beta", {"request_instruction_mode": mode})


def test_scoped_capabilities_are_bound_to_actual_routes():
    assert supports_turn_scoped_instructions(WireShape.ANTHROPIC_MESSAGES, "claude-opus-5-5", "https://api.anthropic.com", {})
    assert supports_turn_scoped_instructions(WireShape.ANTHROPIC_MESSAGES, "anthropic/claude-opus-5.5", "https://openrouter.ai/api", {"request_instruction_mode": "turn_scoped"})
    assert not supports_turn_scoped_instructions(WireShape.ANTHROPIC_MESSAGES, "anthropic/claude-opus-5.5", "https://openrouter.ai/api", {})
    assert not supports_turn_scoped_instructions(WireShape.ANTHROPIC_MESSAGES, "deepseek-flash", "https://api.deepseek.com/anthropic", {"request_instruction_mode": "turn_scoped"})
    assert not supports_turn_scoped_instructions(WireShape.ANTHROPIC_MESSAGES, "claude-sonnet-5", "https://api.anthropic.com", {})
    ctx = gemini()
    assert supports_interaction_instructions(ctx.wire_shape, ctx.model_id, ctx.base_url, ctx.capabilities)
    assert supports_interaction_instructions(ctx.wire_shape, ctx.model_id, ctx.base_url, {})
    assert not supports_interaction_instructions(ctx.wire_shape, ctx.model_id, ctx.base_url,
                                                 {"request_instruction_mode": "chronological"})


@pytest.mark.parametrize("model,provider,url,suffix", [
    ("gpt-6-luna", "openai", "https://api.openai.com/v1", False),
    ("openai/gpt-6.1-terra", "openrouter", "https://openrouter.ai/api/v1", False),
    ("deepseek-flash", "deepseek", "https://api.deepseek.com", False),
    ("gpt-6.2-sol", "compatible", "https://compatible.example/v1", False),
    ("gpt-6.2-sol", "openai", "https://api.openai.com/v1", True),
    ("openai/gpt-6.2-astra", "openrouter", "https://openrouter.ai/api/v1", True),
])
def test_response_instruction_schema_is_independent_of_suffix_cache(model, provider, url, suffix):
    from pal.llm.cache_wire import clean_request, boundary_at
    from pal.llm.cache_diagnostics import describe_request, compare_requests
    ctx = ShapeContext(WireShape.OPENAI_RESPONSE, "node", model, provider, url)
    codec = codec_for_shape(ctx.wire_shape)
    sys = message(MessageRole.SYSTEM, "standing guidance", PromptRegionIR.STABLE_SYSTEM)
    user = message(MessageRole.USER, "task", PromptRegionIR.ACTIVE_INPUT)
    req = request(sys, user, instructions="temporary A")
    changed = replace(req, request_instructions="temporary B")
    a, b = codec.encode(req, ctx), codec.encode(changed, ctx)
    assert supports_response_instructions(ctx.wire_shape, ctx.capabilities)
    assert supports_suffix_instructions(ctx.wire_shape, model, url) is suffix
    assert a.payload["instructions"] == "temporary A"
    assert b.payload["instructions"] == "temporary B"
    assert a.payload["input"] == b.payload["input"]
    assert a.instructions_are_suffix is suffix
    assert (a.message_spans[-1].cache_prefix_fingerprint == b.message_spans[-1].cache_prefix_fingerprint) is suffix
    clean_a, clean_b = clean_request(a), clean_request(b)
    path = clean_a.message_spans[-1].cache_targets[-1]
    assert (boundary_at(clean_a, user.message_id, path).fingerprint ==
            boundary_at(clean_b, user.message_id, path).fingerprint) is suffix
    before = describe_request(req, a, a)
    after = describe_request(changed, b, b)
    assert compare_requests(before, after)["prefix_preserved"] is suffix
    assert "instructions" not in codec.encode(replace(req, request_instructions=""), ctx).payload
    disabled = ShapeContext(ctx.wire_shape, ctx.endpoint_id, model, provider, url,
                            {"request_instruction_mode": "chronological"})
    assert not supports_response_instructions(disabled.wire_shape, disabled.capabilities)
    with pytest.raises(ShapeDecodeError):
        codec.encode(req, disabled)


def test_other_wire_shapes_do_not_inherit_responses_fields():
    for shape in (WireShape.OPENAI_COMPLETION, WireShape.ANTHROPIC_MESSAGES, WireShape.GEMINI_INTERACTIONS):
        assert not supports_response_instructions(shape, {})


def test_leading_response_instruction_does_not_become_a_stripped_replay_anchor():
    from pal.llm.prompt_cache import PromptCacheCoordinator
    ctx = ShapeContext(WireShape.OPENAI_RESPONSE, "old", "openai/gpt-6-astra", "openrouter",
                       "https://openrouter.ai/api/v1", {"prompt_cache": {"mode": "explicit"}})
    coordinator = PromptCacheCoordinator()
    req = replace(request(
        message(MessageRole.SYSTEM, "stable " * 1000, PromptRegionIR.STABLE_SYSTEM),
        message(MessageRole.USER, "earlier " * 1000),
        message(MessageRole.USER, "current " * 1000, PromptRegionIR.ACTIVE_INPUT),
    ), logical_scope_id="pal:resident")
    coordinator.plan(req, ctx)
    assert coordinator.eligible_anchor_request(endpoint_id=ctx.endpoint_id)
    raw = codec_for_shape(ctx.wire_shape).encode(req, ctx)
    coordinator.plan(req, ctx, replace(raw, extra_body={"instructions": "Transport-only control"}))
    assert not coordinator.eligible_anchor_request(endpoint_id=ctx.endpoint_id)
    coordinator.plan(replace(req, request_instructions="Temporary control"), ctx)
    assert not coordinator.eligible_anchor_request(endpoint_id=ctx.endpoint_id)
    coordinator.plan(req, ctx)
    assert coordinator.eligible_anchor_request(endpoint_id=ctx.endpoint_id)


def test_final_transport_override_cannot_inherit_verified_suffix_diagnostics():
    from pal.llm.cache_wire import clean_request
    from pal.llm.cache_diagnostics import describe_request, compare_requests
    ctx = ShapeContext(WireShape.OPENAI_RESPONSE, "sol", "gpt-6.2-sol",
                       "openai", "https://api.openai.com/v1")
    req = request(message(MessageRole.USER, "task"), instructions="temporary A")
    raw = codec_for_shape(ctx.wire_shape).encode(req, ctx)
    overridden = replace(raw, extra_body={"model": "gpt-6-luna", "instructions": "temporary B"})
    wire = clean_request(overridden)
    assert not wire.instructions_are_suffix
    before = describe_request(req, raw, raw)
    after = describe_request(req, overridden, wire)
    assert not compare_requests(before, after)["prefix_preserved"]


def test_response_lineage_binds_actual_model_and_instruction_placement():
    from pal.llm.ir import ReplayEnvelope
    from pal.llm.response_lineage import ResponseLineage
    output = [{"type": "message", "role": "assistant",
               "content": [{"type": "output_text", "text": "answer"}]}]
    answer = message(MessageRole.ASSISTANT, "answer",
                     replay=ReplayEnvelope(WireShape.OPENAI_RESPONSE, "node", "gpt-6.2-sol", {"output": output}))
    payload = {"model": "gpt-6.2-sol", "input": [{"role": "user", "content": "first"}],
               "instructions": "temporary A"}
    lineage = ResponseLineage()
    lineage.remember("scope", answer.message_id, "response", payload, output, instructions_are_suffix=True)
    successor = {**payload, "input": [*payload["input"], *output, {"role": "user", "content": "second"}],
                 "instructions": "temporary B"}
    assert lineage.match("scope", (answer,), successor, instructions_are_suffix=True)
    assert not lineage.match("scope", (answer,), successor, instructions_are_suffix=False)
    assert not lineage.match("scope", (answer,), {**successor, "model": "gpt-6-luna"},
                             instructions_are_suffix=False)


def test_scoped_records_roundtrip_and_stay_present_after_settlement_and_rebuild():
    reminder = scoped()
    assert message_from_payload(message_to_payload(reminder)) == reminder
    ordinary = message(MessageRole.USER, "ordinary")
    assert "instruction_scope" not in message_to_payload(ordinary)
    turn = L1TurnIR.begin("old", user_text="question")
    turn = turn.append_prompt_contexts((reminder,), {})
    turn = turn.upsert_assistant(message(MessageRole.ASSISTANT, "answer"))
    _, additions, state = prepare_context(turn, [], [], boundary="old")
    turn = turn.append_prompt_contexts(additions, state)
    _, additions, state = prepare_context(turn, [], [], boundary="new")
    turn = turn.append_prompt_contexts(additions, state).settle()
    assert reminder in projected_messages(turn, settled=True)
    assert reminder in applicable_context(turn.messages, active_turn_id="", preserve_turn_scoped=True)
    assert reminder not in applicable_context(turn.messages, active_turn_id="")


def test_claude_scoped_wire_round_trip_preserves_records_and_cache_addresses():
    ctx = claude()
    codec = codec_for_shape(ctx.wire_shape)
    system = message(MessageRole.SYSTEM, "stable", PromptRegionIR.STABLE_SYSTEM)
    user = message(MessageRole.USER, "task", PromptRegionIR.ACTIVE_INPUT)
    old = scoped("old reminder")
    call = LLMMessageIR(MessageRole.ASSISTANT, (ToolCallIR("call", "probe", {}),), prompt_region=PromptRegionIR.ACTIVE_HISTORY)
    result = LLMMessageIR(MessageRole.TOOL, (ToolResultIR(call_id="call", name="probe", content="result"),), prompt_region=PromptRegionIR.ACTIVE_HISTORY)
    new = scoped("new reminder")
    first = codec.encode(request(system, user, old), ctx)
    later = codec.encode(request(system, user, replace(old, prompt_region=PromptRegionIR.ACTIVE_HISTORY), call, result, new), ctx)
    assert later.payload["messages"][:2] == first.payload["messages"]
    assert [m["role"] for m in later.payload["messages"]] == ["user", "system", "assistant", "user", "system"]
    assert later.payload["messages"][-1]["clear_at"] == "next_user_message"
    assert "thinking" not in later.payload and later.payload["output_config"]["effort"] == "low"
    spans = {s.message_id: s for s in later.message_spans}
    assert not spans[old.message_id].cache_targets and not spans[new.message_id].cache_targets
    assert spans[result.message_id].cache_targets
    hooked = apply_provider_request_hooks(later, ctx)
    assert ANTHROPIC_CLEAR_AT_BETA in hooked.payload["extra_headers"]["anthropic-beta"]
    counted = count_payload(ctx.wire_shape, hooked.payload)
    assert ANTHROPIC_CLEAR_AT_BETA in counted["extra_headers"]["anthropic-beta"]
    assert _estimate_request_tokens(request(system, user, old, call, result, new)) == _estimate_request_tokens(request(system, user, call, result, new))
    with pytest.raises(ShapeDecodeError):
        codec.encode(request(system, user, old), claude(mode="chronological"))
    with pytest.raises(ShapeDecodeError):
        codec.encode(request(system, old, user), ctx)


def test_gemini_interaction_scope_defaults_to_native_and_keeps_stable_instruction():
    ctx = gemini()
    codec = codec_for_shape(ctx.wire_shape)
    sys = message(MessageRole.SYSTEM, "stable", PromptRegionIR.STABLE_SYSTEM)
    user = message(MessageRole.USER, "task", PromptRegionIR.ACTIVE_INPUT)
    req = request(sys, user, instructions="temporary")
    full = codec.encode(req, ctx)
    assert full.payload["system_instruction"] == "stable\n\ntemporary"
    assert "temporary" not in str(full.payload["input"])
    assert codec.encode(req, gemini("auto")).payload == full.payload
    with pytest.raises(ShapeDecodeError):
        codec.encode(req, gemini("chronological"))
    changed = codec.encode(replace(req, request_instructions="changed"), ctx)
    assert changed.payload["input"] == full.payload["input"]
    assert changed.payload["system_instruction"] != full.payload["system_instruction"]


@pytest.mark.parametrize("ctx", [claude(), gemini(),
    gemini("auto"),
    ShapeContext(WireShape.ANTHROPIC_MESSAGES, "deepseek", "deepseek-flash", "deepseek", "https://api.deepseek.com/anthropic"),
    ShapeContext(WireShape.OPENAI_COMPLETION, "glm", "glm-5.4", "zhipu", "https://api.z.ai/api/paas/v4"),
    ShapeContext(WireShape.OPENAI_RESPONSE, "sol", "gpt-6.2-sol", "openai", "https://api.openai.com/v1"),
    ShapeContext(WireShape.OPENAI_RESPONSE, "luna", "gpt-6-luna", "openai", "https://api.openai.com/v1"),
    ShapeContext(WireShape.OPENAI_RESPONSE, "compat", "gpt-6.2-sol", "proxy", "https://compatible.example/v1")])
def test_projection_uses_bound_route_and_matches_cold_scoped_encoding(ctx):
    from tests.test_projection_two_segment import _attempt
    sess = EndpointProjectionSession(LogicalSessionId("scoped-session"))
    binding = EndpointBinding(ctx.endpoint_id, ctx.model_id, ctx.wire_shape, "spec", "policy", "config", base_url=ctx.base_url, provider_id=ctx.provider_id)
    sess.bind(binding, capabilities=ctx.capabilities)
    sys = message(MessageRole.SYSTEM, "stable", PromptRegionIR.STABLE_SYSTEM)
    user = message(MessageRole.USER, "task", PromptRegionIR.ACTIVE_INPUT)
    scoped_supported = supports_turn_scoped_instructions(ctx.wire_shape, ctx.model_id, ctx.base_url, ctx.capabilities)
    tail = (user, scoped()) if scoped_supported else (user,)
    req = request(sys, *tail, instructions="" if scoped_supported else "temporary")
    attempt = _attempt(sess, "scoped")
    sess.begin_round(attempt, requires_native=False)
    prepared = sess.prepare(HistoryView(sess.frontier, tail), request_shell=req)
    assert json.loads(prepared.payload_json) == thaw_json(codec_for_shape(ctx.wire_shape).encode(req, ctx).payload)


@pytest.mark.parametrize("shape", [WireShape.OPENAI_COMPLETION, WireShape.ANTHROPIC_MESSAGES, WireShape.GEMINI_INTERACTIONS])
@pytest.mark.parametrize("with_preamble", [False, True])
def test_root_request_instructions_are_ephemeral_and_cold_projection_agree(shape, with_preamble):
    from tests.test_projection_two_segment import _attempt
    from pal.llm.cache_diagnostics import describe_request, compare_requests
    ctx = ShapeContext(shape, "node", "compatible", "proxy", "https://example.test")
    assert supports_request_instructions(shape, ctx.model_id, ctx.base_url, ctx.capabilities)
    codec = codec_for_shape(shape)
    user = message(MessageRole.USER, "task", PromptRegionIR.ACTIVE_INPUT)
    preamble = (message(MessageRole.SYSTEM, "stable", PromptRegionIR.STABLE_SYSTEM),) if with_preamble else ()
    req = request(*preamble, user, instructions="temporary instruction")
    original = replace(req, request_instructions="")
    cold = codec.encode(req, ctx)
    normal = codec.encode(original, ctx)
    assert "temporary instruction" in str(cold.payload)
    assert "temporary instruction" not in str(normal.payload)
    assert "instructions" not in cold.payload
    assert not compare_requests(describe_request(original, normal, normal),
                                describe_request(req, cold, cold))["prefix_preserved"]
    session = EndpointProjectionSession(LogicalSessionId("root-session"))
    session.bind(EndpointBinding(ctx.endpoint_id, ctx.model_id, shape, "spec", "policy", "config",
                                 base_url=ctx.base_url, provider_id=ctx.provider_id),
                 capabilities=ctx.capabilities)
    session.begin_round(_attempt(session, "round"), requires_native=False)
    projected = session.prepare(HistoryView(session.frontier, (user,)), request_shell=req)
    assert json.loads(projected.payload_json) == thaw_json(cold.payload)
    assert tuple(projected.message_spans) == cold.message_spans or shape == WireShape.GEMINI_INTERACTIONS
    disabled = replace(ctx, capabilities={"request_instruction_mode": "chronological"})
    with pytest.raises(ShapeDecodeError):
        codec.encode(req, disabled)


@pytest.mark.parametrize("shape", [WireShape.OPENAI_COMPLETION, WireShape.ANTHROPIC_MESSAGES,
                                  WireShape.GEMINI_INTERACTIONS, WireShape.OPENAI_RESPONSE])
@pytest.mark.parametrize("with_preamble", [False, True])
def test_temporary_instruction_never_freezes_into_a_committed_projection(shape, with_preamble):
    from tests.test_projection_two_segment import _attempt, _cursor, _receipt
    ctx = ShapeContext(shape, "node", "compatible", "proxy", "https://example.test")
    codec = codec_for_shape(shape)
    session = EndpointProjectionSession(LogicalSessionId("scope"))
    session.bind(EndpointBinding(ctx.endpoint_id, ctx.model_id, shape, "spec", "policy", "config",
                base_url=ctx.base_url, provider_id=ctx.provider_id), capabilities=ctx.capabilities)
    preamble = (message(MessageRole.SYSTEM, "stable", PromptRegionIR.STABLE_SYSTEM),) if with_preamble else ()
    history = ()
    for i, instructions in enumerate(("temporary first", "temporary second", ""), 1):
        user = message(MessageRole.USER, f"question {i}", PromptRegionIR.ACTIVE_INPUT)
        answer = message(MessageRole.ASSISTANT, f"answer {i}")
        req = request(*preamble, *history, user, instructions=instructions)
        attempt = _attempt(session, f"round-{i}")
        before = session.frontier
        session.begin_round(attempt, requires_native=False)
        projected = session.prepare(HistoryView(before, (user,)), request_shell=req)
        payload = json.loads(projected.payload_json)
        assert payload == thaw_json(codec.encode(req, ctx).payload)
        assert ("temporary first" in str(payload)) is (i == 1)
        assert ("temporary second" in str(payload)) is (i == 2)
        after = _cursor(i, f"d{i}" * 32)
        session.observe_commit(_receipt(attempt, before, after), accepted_messages=(answer,),
                               span_message_ids=(user.message_id, answer.message_id))
        history += (user, answer)


def test_left_bootstrap_does_not_reinterpret_cleared_claude_instruction_on_other_routes():
    from pal.memory.service import MemoryService
    from pal.llm.history_projection import HistoryProjection
    memory = MemoryService()
    turn = memory.begin_l1_turn("old", user_text="question")
    reminder = scoped("CLEARED_CLAUDE_REMINDER")
    memory.append_l1_prompt_contexts("old", (reminder,), {}, expected_revision=turn.revision)
    memory.upsert_l1_assistant("old", message(MessageRole.ASSISTANT, "answer"))
    memory.settle_l1_turn("old")
    memory.history_root.promote()
    projection = HistoryProjection(None, [], None, lambda _: {})
    ordinary, coverage = projection.left_view(memory, memory.history_root)
    native, native_coverage = projection.left_view(memory, memory.history_root, preserve_turn_scoped=True)
    assert reminder.message_id not in coverage
    assert reminder.message_id in native_coverage
    assert reminder in native and reminder not in ordinary
