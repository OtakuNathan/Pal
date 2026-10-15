"""Stream fragments must keep executable calls and native replay aligned."""
from copy import deepcopy

import pytest

from pal.llm.ir import GenerationPolicyIR, LLMRequestIR, WireShape
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext, ShapeDecodeError, _JSONFrame
from pal.shared.json_values import thaw_json


def decode(shape, events):
    context = ShapeContext(shape, "ep", "model")
    updates = list(codec_for_shape(shape).decode(
        [_JSONFrame(index, event) for index, event in enumerate(events)], context,
    ))
    return updates[-1].response


def completion_events(calls, *, together=False):
    batches = [calls] if together else [[call] for call in calls]
    return [
        *[{"choices": [{"delta": {"tool_calls": batch}}]} for batch in batches],
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]


@pytest.mark.parametrize("null_index", [False, True])
@pytest.mark.parametrize("together", [False, True])
def test_completion_indexless_calls_preserve_ids_arguments_and_native_signatures(null_index, together):
    calls = [
        {"id": "read-a", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'},
         "extra_content": {"google": {"thought_signature": "opaque-signature"}}},
        {"id": "read-b", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "b"}'}},
        {"id": "shell", "type": "function", "function": {"name": "run_shell", "arguments": '{"cmd": "pwd"}'}},
    ]
    original = deepcopy(calls)
    if null_index:
        calls = [{**call, "index": None} for call in calls]
    response = decode(WireShape.OPENAI_COMPLETION, completion_events(calls, together=together))
    assert response.finish_reason == "tool_calls"
    assert [(call.call_id, call.name, thaw_json(call.arguments)) for call in response.tool_calls] == [
        ("read-a", "read_file", {"path": "a"}),
        ("read-b", "read_file", {"path": "b"}),
        ("shell", "run_shell", {"cmd": "pwd"}),
    ]
    assert thaw_json(response.message.replay.payload)["message"]["tool_calls"] == original
    context = ShapeContext(WireShape.OPENAI_COMPLETION, "ep", "model")
    request = LLMRequestIR(messages=(response.message,), tools=(), policy=GenerationPolicyIR(max_output_tokens=100))
    encoded = codec_for_shape(context.wire_shape).encode(request, context)
    assert thaw_json(encoded.payload)["messages"][0]["tool_calls"] == original


def test_completion_interleaved_id_only_fragments_do_not_mix_calls():
    fragments = [
        {"id": "a", "function": {"name": "read_file", "arguments": '{"path":'}},
        {"id": "b", "function": {"name": "run_shell", "arguments": '{"cmd":'}},
        {"id": "a", "function": {"arguments": '"a"}'}},
        {"id": "b", "function": {"arguments": '"pwd"}'}},
    ]
    response = decode(WireShape.OPENAI_COMPLETION, completion_events(fragments))
    assert [(call.call_id, call.name, thaw_json(call.arguments)) for call in response.tool_calls] == [
        ("a", "read_file", {"path": "a"}), ("b", "run_shell", {"cmd": "pwd"}),
    ]
    native = thaw_json(response.message.replay.payload)["message"]["tool_calls"]
    assert [call["function"]["arguments"] for call in native] == ['{"path":"a"}', '{"cmd":"pwd"}']


def test_completion_standard_indices_can_be_followed_by_id_only_fragments():
    fragments = [
        {"index": 4, "id": "a", "function": {"name": "read_file", "arguments": '{"path":'}},
        {"id": "b", "function": {"name": "run_shell", "arguments": '{"cmd":"pwd"}'}},
        {"id": "a", "function": {"arguments": '"a"'}},
        {"index": 4, "function": {"arguments": '}'}},
    ]
    response = decode(WireShape.OPENAI_COMPLETION, completion_events(fragments))
    assert [(call.call_id, thaw_json(call.arguments)) for call in response.tool_calls] == [
        ("a", {"path": "a"}), ("b", {"cmd": "pwd"}),
    ]


@pytest.mark.parametrize("fragment,reason", [
    ({"function": {"arguments": "{}"}}, "neither index nor call id"),
    ({"index": 0, "id": "b", "function": {"arguments": "{}"}}, "index changed call id"),
    ({"index": 1, "id": "a", "function": {"arguments": "{}"}}, "call id changed index"),
    ({"index": True, "id": "a"}, "non-negative integer index"),
    ({"index": -1, "id": "a"}, "non-negative integer index"),
    ({"index": "0", "id": "a"}, "non-negative integer index"),
])
def test_completion_rejects_ambiguous_or_conflicting_fragment_identity(fragment, reason):
    first = {"index": 0, "id": "a", "function": {"name": "read_file", "arguments": ""}}
    with pytest.raises(ShapeDecodeError, match=reason):
        decode(WireShape.OPENAI_COMPLETION, completion_events([first, fragment]))


@pytest.mark.parametrize("shape,event,index_field", [
    (WireShape.OPENAI_RESPONSE, {"type": "response.output_item.added", "item": {"type": "function_call"}}, "output_index"),
    (WireShape.OPENAI_RESPONSE, {"type": "response.function_call_arguments.delta", "delta": "{}"}, "output_index"),
    (WireShape.OPENAI_RESPONSE, {"type": "response.output_item.done", "item": {"type": "function_call"}}, "output_index"),
    (WireShape.ANTHROPIC_MESSAGES, {"type": "content_block_start", "content_block": {"type": "tool_use"}}, "index"),
    (WireShape.ANTHROPIC_MESSAGES, {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{}"}}, "index"),
    (WireShape.ANTHROPIC_MESSAGES, {"type": "content_block_stop"}, "index"),
])
@pytest.mark.parametrize("invalid_index", ["missing", None, "0", 0.0, True, -1])
def test_required_stream_index_is_not_silently_treated_as_zero(shape, event, index_field, invalid_index):
    payload = deepcopy(event)
    if invalid_index != "missing":
        payload[index_field] = invalid_index
    with pytest.raises(ShapeDecodeError, match=f"non-negative integer {index_field}"):
        decode(shape, [payload])


@pytest.mark.parametrize("shape", [WireShape.OPENAI_RESPONSE, WireShape.ANTHROPIC_MESSAGES])
def test_indexed_shapes_keep_interleaved_tool_fragments_separate(shape):
    if shape == WireShape.OPENAI_RESPONSE:
        items = [
            {"type": "function_call", "id": "item-a", "call_id": "a", "name": "read_file", "arguments": ""},
            {"type": "function_call", "id": "item-b", "call_id": "b", "name": "run_shell", "arguments": ""},
        ]
        events = [{"type": "response.output_item.added", "output_index": index, "item": item}
                  for index, item in enumerate(items)]
        events += [
            {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "item-a", "delta": '{"path":'},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "item_id": "item-b", "delta": '{"cmd":'},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "item-a", "delta": '"a"}'},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "item_id": "item-b", "delta": '"pwd"}'},
        ]
        for index, arguments in enumerate(['{"path":"a"}', '{"cmd":"pwd"}']):
            events.append({"type": "response.output_item.done", "output_index": index,
                           "item": {**items[index], "arguments": arguments}})
        events.append({"type": "response.completed", "response": {"status": "completed"}})
    else:
        events = [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "a", "name": "read_file", "input": {}}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "b", "name": "run_shell", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"cmd":'}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '"a"}'}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"pwd"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        ]
    response = decode(shape, events)
    assert [(call.call_id, call.name, thaw_json(call.arguments)) for call in response.tool_calls] == [
        ("a", "read_file", {"path": "a"}), ("b", "run_shell", {"cmd": "pwd"}),
    ]
    native = thaw_json(response.message.replay.payload)
    if shape == WireShape.OPENAI_RESPONSE:
        assert [item["call_id"] for item in native["output"]] == ["a", "b"]
    else:
        assert [item["id"] for item in native["content"]] == ["a", "b"]
