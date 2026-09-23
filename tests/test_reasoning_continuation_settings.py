"""Offline coverage of portable reasoning and requested/observed modes."""
from dataclasses import replace

import pytest

from pal.llm.endpoint_spec import LLMEndpointSpec, LLMEndpointSpecError
from pal.llm.ir import GenerationPolicyIR, WireShape
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.llm.serde import response_from_payload, response_to_payload
from pal.llm.shapes.base import ShapeContext, ShapeDecodeError, _JSONFrame
from pal.llm.shapes.openai_response import OpenAIResponseCodec
from pal.memory import MemoryService
from pal.memory.runtime_state import MemoryRuntimeStatePort
from tests.test_llm_runtime_ir import _endpoint, _request, _Settings, _Invoker


def context(capabilities=None):
    return ShapeContext(WireShape.OPENAI_RESPONSE, "ep", "test-model",
                        capabilities=capabilities or {})


def test_responses_requests_portable_reasoning_without_forcing_context_mode():
    payload = OpenAIResponseCodec().encode(_request(), context()).payload
    assert payload["store"] is False
    assert list(payload["include"]) == ["reasoning.encrypted_content"]
    assert "context" not in payload.get("reasoning", {})


def test_compatibility_exclusions_are_respected():
    ctx = context({"unsupported_request_parameters": ["store", "include", "reasoning.context"]})
    payload = OpenAIResponseCodec().encode(_request(), ctx).payload
    assert "store" not in payload and "include" not in payload
    request = replace(_request(), policy=GenerationPolicyIR(100, reasoning_context="all_turns"))
    with pytest.raises(ShapeDecodeError, match="reasoning.context"):
        OpenAIResponseCodec().encode(request, ctx)


@pytest.mark.parametrize("mode", ["auto", "current_turn", "all_turns"])
def test_endpoint_mode_is_resolved_into_ir_and_encoded(mode):
    endpoint = _endpoint()
    endpoint.wire_shape = "openai_response"
    endpoint.capabilities_blob = {"reasoning_context": mode}
    runtime = LLMRuntime(EndpointResolver(endpoints=(endpoint,)), _Settings(), endpoint_invoker=_Invoker())
    try:
        prepared = runtime._compile_request(endpoint, _request())
        assert prepared.request.policy.reasoning_context == mode
        wire = OpenAIResponseCodec().encode(prepared.request, context()).payload
        assert wire["reasoning"]["context"] == mode
        # A captured, resolved request keeps its selection during warm reuse.
        endpoint.capabilities_blob = {"reasoning_context": "auto"}
        again = runtime._compile_request(endpoint, prepared.request)
        assert again.request.policy.reasoning_context == mode
    finally:
        runtime.close()


@pytest.mark.parametrize("shape,mode", [("openai_completion", "all_turns"), ("openai_response", "invented")])
def test_invalid_endpoint_modes_rejected(shape, mode):
    endpoint = _endpoint()
    endpoint.wire_shape = shape
    endpoint.capabilities_blob = {"reasoning_context": mode}
    with pytest.raises(LLMEndpointSpecError):
        LLMEndpointSpec.from_value(endpoint)


@pytest.mark.parametrize("stream", [False, True])
def test_effective_mode_survives_response_and_l1_restore_without_becoming_prompt_text(stream):
    output = [
        {"type": "reasoning", "id": "r", "encrypted_content": "opaque", "summary": []},
        {"type": "message", "id": "a", "role": "assistant", "phase": "final_answer",
         "content": [{"type": "output_text", "text": "done"}]},
    ]
    response = {"output": output, "reasoning": {"context": "current_turn"}, "status": "completed"}
    frames = ([_JSONFrame(0, {"type": "response.created", "response": {"reasoning": {"context": "all_turns"}}}),
               _JSONFrame(1, {"type": "response.completed", "response": response})]
              if stream else [_JSONFrame(0, response)])
    decoded = list(OpenAIResponseCodec().decode(iter(frames), context()))[-1].response
    restored = response_from_payload(response_to_payload(decoded))
    assert restored.reasoning_context == "current_turn"
    assert restored.message.metadata["reasoning_context"] == "current_turn"
    service = MemoryService()
    service.begin_l1_turn("T", user_text="hello")
    service.upsert_l1_assistant("T", restored.message)
    service.settle_l1_turn("T")
    target = MemoryService()
    port = MemoryRuntimeStatePort(target)
    port.install_prepared_state(port.prepare_restore_state(MemoryRuntimeStatePort(service).snapshot_state()))
    message = target.l1_store.turns.turns[-1].messages[-1]
    assert message.metadata["reasoning_context"] == "current_turn"
    wire = OpenAIResponseCodec().encode(replace(_request(), messages=(message,)), context()).payload
    assert list(wire["input"]) == output
    assert "current_turn" not in message.text


def test_missing_observation_remains_unknown_and_old_response_payload_is_readable():
    body = {"output": [{"type": "message", "role": "assistant",
                       "content": [{"type": "output_text", "text": "done"}]}], "status": "completed"}
    decoded = list(OpenAIResponseCodec().decode(iter([_JSONFrame(0, body)]), context()))[-1].response
    payload = response_to_payload(decoded)
    payload.pop("reasoning_context")
    restored = response_from_payload(payload)
    assert restored.reasoning_context == ""
    assert "reasoning_context" not in restored.message.metadata
