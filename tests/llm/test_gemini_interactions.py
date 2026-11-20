from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from pal.llm.compaction_engine import CompactionClockKind, CompactionEngine
from pal.llm.resident_compaction import PalCompactionPolicy
from pal.memory import MemoryService
from pal.memory.turn_ir import L1TurnIR, L1TurnState
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.history_projection import HistoryProjection
from pal.llm.http_transport import _gemini_frames
from pal.llm.ir import GenerationPolicyIR, ImagePartIR, LLMMessageIR, LLMRequestIR, TextPartIR, ThinkingLevel, WireShape
from pal.llm.shapes.base import ShapeContext, ShapeDecodeError, _JSONFrame
from pal.llm.shapes.gemini import GeminiDecoder, GeminiInteractionsCodec
from pal.llm.transport import DirectEndpointTransport, LLMTransportError
from pal.shared.tool_protocol import ToolDefinitionIR, ToolResultIR
from tests.test_chatgpt_subscription import endpoint


def spec(mode="auto"):
    return LLMEndpointSpec.from_value({
        **vars(endpoint()), "endpoint_id": "google", "provider": "google",
        "wire_shape": "gemini_interactions", "transport": "http", "auth_kind": "api_key_ref",
        "base_url": "https://google.invalid/v1beta", "capabilities_blob": {"continuation_mode": mode},
    })


def message(text):
    return LLMMessageIR("user", (TextPartIR(text),))


def response(number, steps=None):
    return httpx.Response(200, json={
        "id": f"int-{number}", "status": "completed",
        "steps": steps or [{"type": "model_output", "content": [{"type": "text", "text": "ok"}]}],
        "usage": {"total_input_tokens": 150, "total_cached_tokens": 120, "total_output_tokens": 4},
    })


@pytest.fixture
def transport(monkeypatch):
    bodies = []
    replies = []

    def serve(request):
        assert request.url.path == "/v1beta/interactions"
        assert request.headers["x-goog-api-key"] == "test"
        assert request.headers["Api-Revision"] == "2026-05-20"
        bodies.append(json.loads(request.content))
        return replies.pop(0) if replies else response(len(bodies))

    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(serve), **kwargs))
    invoker = ShapeEndpointInvoker(transport=DirectEndpointTransport(lambda _: "test"))
    yield invoker, bodies, replies
    invoker.close()


def test_native_compact_uses_frozen_left_even_after_a_newer_response(transport):
    invoker, bodies, _ = transport
    ep = spec()
    req = LLMRequestIR((message("LEFT"),), (), GenerationPolicyIR(50), logical_scope_id="resident")
    first, _ = invoker.invoke(ep, req)
    assert first.usage.cached_input_tokens == 120
    left = (*req.messages, first.message)
    right = replace(req, messages=(*left, message("RIGHT")))
    invoker.invoke(ep, right)
    assert bodies[-1]["previous_interaction_id"] == "int-1"

    projection = HistoryProjection(None, [], lambda messages, **_: messages, lambda _: {})
    memory = MemoryService()
    memory.history_root.store.append(L1TurnIR("left", messages=left, state=L1TurnState.SETTLED))
    memory.history_root.promote()
    memory.history_root.store.append(L1TurnIR("right", messages=(right.messages[-1],)))
    snapshot = memory.begin_left_compaction("cut", reason="test")
    source = projection.prepare_compaction_source(
        memory, None, snapshot, logical_scope_id="resident", preferred_endpoint_id=ep.endpoint_id,
        preferred_model_id=ep.model_id, active_turn_id="", strategy="semantic_checkpoint", response_continuation=True,
        build_request=lambda *_: replace(req, messages=()),
    )
    captured = memory.capture_compaction(
        snapshot, target_input_budget=4000, reserved_output_tokens=512,
        clock_kind=CompactionClockKind.USER_TURN, clock_value=1,
        replay_request=source.replay_request, replay_dialect=source.dialect, replay_wire_shape=source.wire_shape,
    )
    compact = CompactionEngine(PalCompactionPolicy())._request(captured, "", attempt=1)
    invoker.invoke(ep, compact)
    assert bodies[-1]["previous_interaction_id"] == "int-1"
    assert len(bodies[-1]["input"]) == 1
    assert "RIGHT" not in json.dumps(bodies[-1])
    assert len(invoker.lineage.prefixes) == 2

    # Changes to the accepted prefix, binding, instructions or scope require full replay.
    for target, modified in [
        (ep, replace(right, logical_scope_id="other")),
        (ep, replace(right, messages=(message("EDIT"), *right.messages[1:]))),
        (ep, replace(right, messages=(LLMMessageIR("system", (TextPartIR("new"),)), *right.messages))),
        (replace(ep, model_id="other"), right),
    ]:
        invoker.invoke(target, modified)
        assert "previous_interaction_id" not in bodies[-1]


@pytest.mark.parametrize("status,detail,expected", [
    (404, "Requested entity was not found.", 3),
    (404, "Model not found", 2),
    (503, "service unavailable", 2),
])
def test_missing_parent_retries_once_with_signed_full_replay(transport, status, detail, expected):
    invoker, bodies, replies = transport
    ep = spec()
    req = LLMRequestIR((message("lookup"),), (), GenerationPolicyIR(50), logical_scope_id="resident")
    steps = [{"type": "thought", "signature": "signed"},
             {"type": "function_call", "id": "call", "name": "lookup", "arguments": {}}]
    replies.append(response(1, steps))
    first, _ = invoker.invoke(ep, req)
    result = LLMMessageIR("tool", (ToolResultIR("call", "lookup", "value"),))
    req = replace(req, messages=(*req.messages, first.message, result))
    replies.append(httpx.Response(status, json={"error": {"status": "NOT_FOUND", "message": detail}}))
    if expected == 3:
        invoker.invoke(ep, req)
        assert bodies[-1]["input"][1:3] == steps
        assert "previous_interaction_id" not in bodies[-1]
    else:
        with pytest.raises(LLMTransportError):
            invoker.invoke(ep, req)
    assert len(bodies) == expected
    assert bodies[1]["previous_interaction_id"] == "int-1"
    assert [s["type"] for s in bodies[1]["input"]] == ["function_result"]


def test_images_preserve_order_and_media_types(transport):
    invoker, bodies, _ = transport
    msg = LLMMessageIR("user", (TextPartIR("caption"),
        ImagePartIR("https://example.test/image.jpg?v=2"), ImagePartIR("data:image/png;base64,YWJj")))
    invoker.invoke(spec(), LLMRequestIR((msg,), (), GenerationPolicyIR(50)))
    assert bodies[0]["input"][0]["content"] == [
        {"type": "text", "text": "caption"},
        {"type": "image", "uri": "https://example.test/image.jpg?v=2", "mime_type": "image/jpeg"},
        {"type": "image", "data": "YWJj", "mime_type": "image/png"},
    ]


def test_full_mode_never_stores_or_uses_a_reference(transport):
    invoker, bodies, _ = transport
    ep = spec("full")
    req = LLMRequestIR((message("first"),), (), GenerationPolicyIR(50), logical_scope_id="resident")
    first, _ = invoker.invoke(ep, req)
    invoker.invoke(ep, replace(req, messages=(*req.messages, first.message, message("next"))))
    assert all(not body["store"] and "previous_interaction_id" not in body for body in bodies)
    assert len(bodies[-1]["input"]) == 3


def test_unfinished_stream_and_token_limit_are_distinct():
    context = ShapeContext(WireShape.GEMINI_INTERACTIONS, "google", "model")
    decoder = GeminiDecoder(context)
    decoder.feed(_JSONFrame(0, {"event_type": "step.start", "index": 0,
                                "step": {"type": "function_call", "id": "partial", "name": "lookup"}}))
    with pytest.raises(ShapeDecodeError, match="ended before terminal"):
        decoder.finish()
    terminal = decoder.feed(_JSONFrame(1, {"event_type": "interaction.completed",
                                           "interaction": {"status": "incomplete"}}))[-1]
    assert terminal.response.finish_reason.value == "length"
    assert not terminal.response.message.parts


def test_recovered_response_replays_both_segments_on_next_turn(transport):
    from pal.llm.output_recovery import merge_responses
    from pal.llm.replay_acceptance import validate_native_for_send

    invoker, bodies, replies = transport
    req = LLMRequestIR((message("first"),), (), GenerationPolicyIR(64))
    steps = [{"type": "thought", "signature": "sig"},
             {"type": "model_output", "content": [{"type": "text", "text": "partial"}]}]
    replies.append(httpx.Response(200, json={"id": "part", "status": "incomplete", "steps": steps}))
    truncated, _ = invoker.invoke(spec(), req)
    final, _ = invoker.invoke(spec(), replace(req, messages=(*req.messages, truncated.message, message("continue"))))
    recovered = merge_responses([truncated, final])
    validate_native_for_send(recovered.message)
    invoker.invoke(spec(), replace(req, messages=(*req.messages, recovered.message, message("next"))))
    assert bodies[-1]["input"][1:3] == steps
    assert bodies[-1]["input"][3]["content"][0]["text"] == final.text


def test_empty_token_limited_segment_can_be_followed_by_recovered_text(transport):
    from pal.llm.output_recovery import merge_responses
    from pal.llm.replay_acceptance import validate_native_for_send

    invoker, _, replies = transport
    req = LLMRequestIR((message("first"),), (), GenerationPolicyIR(64))
    replies.append(httpx.Response(200, json={"id": "part", "status": "incomplete", "steps": []}))
    truncated, _ = invoker.invoke(spec(), req)
    final, _ = invoker.invoke(spec(), req)
    recovered = merge_responses([truncated, final])
    assert recovered.text == final.text
    validate_native_for_send(recovered.message)


def test_multiline_sse_event_is_one_frame():
    response = httpx.Response(200, content=b'data: {"event_type":\ndata: "interaction.completed"}\n\n')
    frame, = _gemini_frames(response, stream=True)
    assert frame.payload["event_type"] == "interaction.completed"


def test_sse_flushes_trailing_frame_without_final_blank_line():
    response = httpx.Response(200, content=b'data: {"event_type": "interaction.completed", "interaction": {"status": "completed"}}')
    frame, = _gemini_frames(response, stream=True)
    assert frame.payload["event_type"] == "interaction.completed"


def test_streaming_sse_roundtrip_with_thought_signature_and_zero_arg_tool(transport):
    invoker, bodies, replies = transport
    ep = spec()
    events = [
        {"event_type": "interaction.created", "interaction": {"id": "int-stream-1", "status": "in_progress"}},
        {"event_type": "step.start", "index": 0, "step": {"type": "thought"}},
        {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_summary", "content": {"type": "text", "text": "checking state"}}},
        {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_signature", "signature": "sig-abc"}},
        {"event_type": "step.stop", "index": 0},
        {"event_type": "step.start", "index": 1, "step": {"type": "function_call", "id": "call-1", "name": "lookup"}},
        {"event_type": "step.delta", "index": 1, "delta": {"type": "arguments_delta", "arguments": '{"k":'}},
        {"event_type": "step.delta", "index": 1, "delta": {"type": "arguments_delta", "arguments": '1}'}},
        {"event_type": "step.stop", "index": 1},
        {"event_type": "step.start", "index": 2, "step": {"type": "function_call", "id": "call-2", "name": "inspect_llm"}},
        {"event_type": "step.stop", "index": 2},
        {"event_type": "interaction.completed", "interaction": {
            "id": "int-stream-1", "status": "requires_action",
            "usage": {"total_input_tokens": 80, "total_cached_tokens": 20, "total_output_tokens": 12, "total_thought_tokens": 7},
        }},
    ]
    sse = "".join(f"data: {json.dumps(ev)}\n\n" for ev in events).encode()
    replies.append(httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"}))
    tools = (
        ToolDefinitionIR("lookup", "lookup", {"type": "object", "properties": {"k": {"type": "integer"}}}),
        ToolDefinitionIR("inspect_llm", "inspect", {"type": "object", "properties": {}}),
    )
    req = LLMRequestIR((message("check"),), tools, GenerationPolicyIR(128, temperature=0.05, thinking_level=ThinkingLevel.MEDIUM),
                       logical_scope_id="resident")
    first, _ = invoker.invoke(ep, req, stream=True)
    assert bodies[0]["stream"] is True
    assert "temperature" not in bodies[0]["generation_config"]
    assert bodies[0]["generation_config"]["thinking_level"] == "medium"
    assert first.usage.reasoning_tokens == 7
    assert [(c.call_id, c.name, dict(c.arguments)) for c in first.message.tool_calls] == [
        ("call-1", "lookup", {"k": 1}),
        ("call-2", "inspect_llm", {}),
    ]
    assert first.message.replay.payload["steps"][0]["signature"] == "sig-abc"

    # Follow-up with identical tools chains via previous_interaction_id.
    tool_msg = LLMMessageIR("tool", (ToolResultIR("call-1", "lookup", "ok"), ToolResultIR("call-2", "inspect_llm", "ok")))
    followup = replace(req, messages=(*req.messages, first.message, tool_msg))
    invoker.invoke(ep, followup, stream=False)
    assert bodies[1]["previous_interaction_id"] == "int-stream-1"
    assert [s["type"] for s in bodies[1]["input"]] == ["function_result", "function_result"]

    # Changing tools invalidates the prefix and forces full replay.
    changed_tools = replace(followup, tools=(tools[0],))
    invoker.invoke(ep, changed_tools, stream=False)
    assert "previous_interaction_id" not in bodies[2]


def test_thinking_omits_temperature_unless_explicitly_enabled():
    codec = GeminiInteractionsCodec()
    req_thinking = LLMRequestIR((message("hi"),), (), GenerationPolicyIR(64, temperature=0.05, thinking_level=ThinkingLevel.LOW))
    req_off = LLMRequestIR((message("hi"),), (), GenerationPolicyIR(64, temperature=0.05, thinking_level=ThinkingLevel.OFF))
    base_ctx = ShapeContext(WireShape.GEMINI_INTERACTIONS, "google", "gemini-4-neon")
    assert "temperature" not in codec.encode(req_thinking, base_ctx).payload["generation_config"]
    assert codec.encode(req_off, base_ctx).payload["generation_config"]["temperature"] == 0.05
    opt_in_ctx = replace(base_ctx, capabilities={"supports_temperature": True})
    assert codec.encode(req_thinking, opt_in_ctx).payload["generation_config"]["temperature"] == 0.05
