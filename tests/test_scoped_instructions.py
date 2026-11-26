"""Scoped operator instructions are not ordinary mutable historical messages."""
from dataclasses import replace
import json

import pytest

from pal.llm.cache_capabilities import (
    ANTHROPIC_CLEAR_AT_BETA, supports_interaction_instructions, supports_turn_scoped_instructions,
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
    assert not supports_interaction_instructions(ctx.wire_shape, ctx.model_id, ctx.base_url, {})


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


def test_gemini_interaction_scope_is_optional_and_keeps_stable_instruction():
    ctx = gemini()
    codec = codec_for_shape(ctx.wire_shape)
    sys = message(MessageRole.SYSTEM, "stable", PromptRegionIR.STABLE_SYSTEM)
    user = message(MessageRole.USER, "task", PromptRegionIR.ACTIVE_INPUT)
    req = request(sys, user, instructions="temporary")
    full = codec.encode(req, ctx)
    assert full.payload["system_instruction"] == "stable\n\ntemporary"
    assert "temporary" not in str(full.payload["input"])
    with pytest.raises(ShapeDecodeError):
        codec.encode(req, gemini("auto"))
    changed = codec.encode(replace(req, request_instructions="changed"), ctx)
    assert changed.payload["input"] == full.payload["input"]
    assert changed.payload["system_instruction"] != full.payload["system_instruction"]


@pytest.mark.parametrize("ctx", [claude(), gemini(), ShapeContext(WireShape.OPENAI_RESPONSE, "sol", "gpt-6.2-sol", "openai", "https://api.openai.com/v1")])
def test_projection_uses_bound_route_and_matches_cold_scoped_encoding(ctx):
    from tests.test_projection_two_segment import _attempt
    sess = EndpointProjectionSession(LogicalSessionId("scoped-session"))
    binding = EndpointBinding(ctx.endpoint_id, ctx.model_id, ctx.wire_shape, "spec", "policy", "config", base_url=ctx.base_url, provider_id=ctx.provider_id)
    sess.bind(binding, capabilities=ctx.capabilities)
    sys = message(MessageRole.SYSTEM, "stable", PromptRegionIR.STABLE_SYSTEM)
    user = message(MessageRole.USER, "task", PromptRegionIR.ACTIVE_INPUT)
    tail = (user, scoped()) if ctx.wire_shape == WireShape.ANTHROPIC_MESSAGES else (user,)
    req = request(sys, *tail, instructions="" if ctx.wire_shape == WireShape.ANTHROPIC_MESSAGES else "temporary")
    attempt = _attempt(sess, "scoped")
    sess.begin_round(attempt, requires_native=False)
    prepared = sess.prepare(HistoryView(sess.frontier, tail), request_shell=req)
    assert json.loads(prepared.payload_json) == thaw_json(codec_for_shape(ctx.wire_shape).encode(req, ctx).payload)


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
