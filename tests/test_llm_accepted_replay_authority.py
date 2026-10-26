"""Accepted L1 -> derived projection -> next wire must preserve one truth."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pal.core.turns import LLMRequestEffect
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.ir import MessageRole, WireShape
from pal.llm.replay_acceptance import native_candidate_from_accepted_message, validate_native_for_send
from pal.llm.response_blocks import ResponseBlock
from pal.llm.response_hooks import EndpointResponseHook, ProviderResponseHookError, ProviderResponseHookRegistry
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.llm.shapes.base import _JSONFrame
from pal.memory import MemoryService
from pal.shared import PromptAssemblyContext
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
from tests.test_llm_response_hook_layers import frames
from tests.test_v3_n1_root_lifecycle import user
from tests.test_v3_n3_vertical_trace import _endpoint, _executor, _request_for, _Settings
from tests.turn_fakes import continuation


OPAQUE = "opaque-original-signature"


def with_reasoning(shape, payloads, streaming):
    if shape == WireShape.OPENAI_COMPLETION:
        reasoning = {"reasoning_content": "original reasoning", "reasoning_details": [{"opaque": OPAQUE}]}
        if streaming:
            return [{"choices": [{"delta": reasoning}]}, *payloads]
        payloads[0]["choices"][0]["message"].update(reasoning)
    elif shape == WireShape.ANTHROPIC_MESSAGES:
        reasoning = {"type": "thinking", "thinking": "original reasoning", "signature": OPAQUE}
        if streaming:
            return [
                {"type": "content_block_start", "index": 0, "content_block": reasoning},
                {"type": "content_block_stop", "index": 0},
                *({**event, "index": event["index"] + 1} if "index" in event else event for event in payloads),
            ]
        payloads[0]["content"].insert(0, reasoning)
    else:
        reasoning = {"type": "reasoning", "id": "reason", "summary": [], "encrypted_content": OPAQUE}
        if streaming:
            return [
                {"type": "response.output_item.added", "output_index": 0, "item": reasoning},
                {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
                *({**event, "output_index": event["output_index"] + 1} if "output_index" in event else event
                  for event in payloads),
            ]
        payloads[0]["output"].insert(0, reasoning)
    return payloads


def wire_calls(shape, payload):
    if shape == WireShape.OPENAI_COMPLETION:
        messages = payload.get("messages", [payload.get("message", {})])
        return [(call["id"], call["function"]["name"], json.loads(call["function"]["arguments"]))
                for message in messages for call in message.get("tool_calls", ())]
    if shape == WireShape.OPENAI_RESPONSE:
        return [(item["call_id"], item["name"], json.loads(item["arguments"]))
                for item in payload.get("input", payload.get("output", ())) if item.get("type") == "function_call"]
    blocks = payload.get("content")
    if blocks is None:
        blocks = [block for message in payload.get("messages", ()) for block in message.get("content", ())
                  if isinstance(block, dict)]
    return [(block["id"], block["name"], block["input"]) for block in blocks if block.get("type") == "tool_use"]


def body_frames(shape, text, streaming):
    if shape == WireShape.OPENAI_COMPLETION:
        if streaming:
            return [*({"choices": [{"delta": {"content": char}}]} for char in text),
                    {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
        return [{"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}]
    if shape == WireShape.ANTHROPIC_MESSAGES:
        if streaming:
            return [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    *({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": char}}
                      for char in text),
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"}]
        return [{"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}]
    message = {"type": "message", "id": "answer", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    if streaming:
        return [{"type": "response.output_item.added", "output_index": 0, "item": {**message, "content": []}},
                *({"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": char} for char in text),
                {"type": "response.output_item.done", "output_index": 0, "item": message},
                {"type": "response.completed", "response": {"status": "completed"}}]
    return [{"output": [message]}]


@pytest.mark.parametrize("shape", list(WireShape))
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("syntax", ["json", "wrapped", "dsml", "body_dsml"])
def test_hook_repairs_are_the_same_in_l1_projection_and_next_request(tmp_path, shape, streaming, syntax):
    if syntax in {"dsml", "body_dsml"}:
        raw = '<｜DSML｜parameter name="target" string="false">0</｜DSML｜parameter>'
        if syntax == "body_dsml":
            raw = '<｜DSML｜tool_calls><｜DSML｜invoke name="read_file">' + raw + '</｜DSML｜invoke></｜DSML｜tool_calls>'
    else:
        raw = ('wrapped:' if syntax == "wrapped" else '') + '{"target":0}'

    def endpoint_hook(context, updates):
        for update in updates:
            def repair(part):
                if not isinstance(part, ResponseBlock):
                    return part
                arguments = part.arguments
                if isinstance(arguments, str):
                    arguments = json.loads(arguments.removeprefix("wrapped:"))
                assert dict(arguments) == {"target": 0}
                return replace(part, payload={**dict(part.payload), "name": "accepted_read", "arguments": {"target": 5}})
            yield replace(update, tool_block=repair(update.tool_block), response=replace(
                update.response, message=replace(update.response.message,
                    parts=tuple(repair(part) for part in update.response.message.parts))))

    class Transport:
        def __init__(self):
            self.requests = []

        def frames(self, endpoint, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                payloads = body_frames(shape, raw, streaming) if syntax == "body_dsml" else frames(shape, raw, streaming)
                payloads = with_reasoning(shape, payloads, streaming)
            elif shape == WireShape.OPENAI_COMPLETION:
                payloads = [{"choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]}]
            elif shape == WireShape.OPENAI_RESPONSE:
                payloads = [{"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}]}]
            else:
                payloads = [{"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}]
            for index, payload in enumerate(payloads):
                yield _JSONFrame(index, payload)

    ep = _endpoint()
    ep.provider = "deepseek" if syntax in {"dsml", "body_dsml"} else "vendor"
    ep.wire_shape = shape.value
    ep.supports_streaming = streaming
    defaults = ProviderResponseHookRegistry.builtin()
    hooks = ProviderResponseHookRegistry(defaults.hooks, {ep.endpoint_id: EndpointResponseHook(ep.endpoint_id, endpoint_hook)})
    transport = Transport()
    runtime = LLMRuntime(EndpointResolver(endpoints=(ep,)), _Settings(),
        endpoint_invoker=ShapeEndpointInvoker(transport=transport, response_hooks=hooks),
        config=SimpleNamespace(runtime_root=tmp_path, llm_endpoint_retry_attempts=1))
    memory = MemoryService()
    memory.begin_l1_turn("T", user_message=user("question", "q"))
    executor = _executor(memory, runtime)

    def drive():
        request = _request_for(memory, ())
        executor.build_turn_prompt = lambda *args, **kwargs: request
        return asyncio.run(executor._handle_llm_request(
            LLMRequestEffect(assembly_context=PromptAssemblyContext()), continuation(turn_id="T")))

    try:
        outcome = drive().payload
        assert outcome.tool_calls[0].args == {"target": 5}
        assert not hasattr(outcome.projection_receipt, "native")
        accepted = next(message for message in memory.active_l1_turn("T").messages if message.role == MessageRole.ASSISTANT)
        call_id = outcome.tool_calls[0].call_id
        expected = [(call_id, "accepted_read", {"target": 5})]
        assert accepted.tool_calls == outcome.tool_calls
        assert wire_calls(shape, thaw_json(accepted.replay.payload)) == expected
        assert accepted.replay.source_payload is not None
        original = thaw_json(accepted.replay.source_payload)
        assert raw in json.dumps(original, ensure_ascii=False).replace('\\"', '"')
        assert OPAQUE in json.dumps(original)
        session = runtime.endpoint_projection_session("pal:resident", rebind=False)
        assert len(session.native_by_attempt) == 1, executor.state.diagnostics
        frozen = json.loads(next(iter(session.native_by_attempt.values()))["payload_json"])
        assert wire_calls(shape, frozen) == expected
        assert OPAQUE in json.dumps(frozen)
        memory.append_l1_tool_result("T", ToolResultIR(call_id=call_id, name="accepted_read", content="result"))
        assert drive().payload.text == "done"
        sent = thaw_json(transport.requests[1].payload)
        assert wire_calls(shape, sent) == expected
        assert OPAQUE in json.dumps(sent)
        # Rebuild from canonical L1 after discarding every derived projection.
        from pal.memory.runtime_state import MemoryRuntimeStatePort

        restored = MemoryService()
        target = MemoryRuntimeStatePort(restored)
        target.install_prepared_state(target.prepare_restore_state(MemoryRuntimeStatePort(memory).snapshot_state()))
        runtime.retire_projection_sessions()
        rebuilt_executor = _executor(restored, runtime)
        pack = rebuilt_executor._prepare_turn_projection(runtime, _request_for(restored, ()))
        assert pack is not None
        rebuilt = thaw_json(pack[0].payload)
        assert wire_calls(shape, rebuilt) == expected
        assert OPAQUE in json.dumps(rebuilt)
        rebuilt_executor._abandon_turn_projection_round(pack)
    finally:
        runtime.close()


@pytest.mark.parametrize("shape", list(WireShape))
@pytest.mark.parametrize("field", ["name", "arguments"])
def test_same_ids_cannot_hide_disagreement_between_semantics_and_replay(shape, field):
    from tests.test_llm_response_hook_layers import Transport
    from tests.test_llm_response_hooks import _endpoint as hook_endpoint, _request

    response, _ = ShapeEndpointInvoker(transport=Transport(frames(shape, '{"target":0}', False))).invoke(
        hook_endpoint(provider="vendor", wire_shape=shape.value), _request())
    call = response.tool_calls[0]
    changed = ToolCallIR(call.call_id, "changed" if field == "name" else call.name,
                         {"target": 5} if field == "arguments" else call.arguments)
    message = replace(response.message, parts=(changed,))
    with pytest.raises(ProviderResponseHookError, match="payload differs"):
        native_candidate_from_accepted_message(message)
    with pytest.raises(ProviderResponseHookError, match="payload differs"):
        validate_native_for_send(message)
