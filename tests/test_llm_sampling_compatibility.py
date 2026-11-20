from dataclasses import replace

import pytest

from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.endpoint_spec import LLMEndpointSpec, LLMEndpointSpecError
from pal.llm.ir import GenerationPolicyIR, LLMRequestIR, ThinkingLevel, WireShape
from pal.llm.request_hooks import apply_provider_request_hooks
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import EncodedRequest, ShapeContext
from pal.llm.transport import DirectSDKTransport
from tests.test_chatgpt_subscription import endpoint
from tests.test_llm_endpoint_composition import _Port


@pytest.mark.parametrize("shape", [WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION])
@pytest.mark.parametrize("prefix", ["", "openai/"])
@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna"])
@pytest.mark.parametrize("level", [None, ThinkingLevel.HIGH, ThinkingLevel.OFF])
def test_model_sampling_rules_apply_across_access_paths(shape, prefix, model, level):
    context = ShapeContext(shape, "test", prefix + model)
    request = LLMRequestIR(messages=(), tools=(), policy=GenerationPolicyIR(
        max_output_tokens=128, temperature=0.2, thinking_level=level,
    ))
    payload = codec_for_shape(shape).encode(request, context).payload
    without_reasoning = level == ThinkingLevel.OFF and model in {
        "gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna",
    }
    assert ("temperature" in payload) == without_reasoning
    if without_reasoning:
        effort = payload["reasoning"]["effort"] if shape == WireShape.OPENAI_RESPONSE else payload["reasoning_effort"]
        assert effort == "none"
        assert payload["temperature"] == 0.2
    assert request.policy.temperature == 0.2
    assert request.policy.thinking_level == level


@pytest.mark.parametrize("shape", list(WireShape))
@pytest.mark.parametrize("model", ["ordinary-model", "other/gpt-6.1-sol", "gpt-6-future"])
def test_unknown_models_keep_existing_sampling_behavior(shape, model):
    context = ShapeContext(shape, "test", model, provider_id="openai")
    request = LLMRequestIR(messages=(), tools=(), policy=GenerationPolicyIR(
        max_output_tokens=128, temperature=0.2,
    ))
    payload = codec_for_shape(shape).encode(request, context).payload
    assert payload.get("generation_config", payload)["temperature"] == 0.2


def test_explicit_capabilities_override_model_defaults_and_keep_parameter_restrictions():
    request = LLMRequestIR(messages=(), tools=(), policy=GenerationPolicyIR(
        max_output_tokens=128, temperature=0.2,
    ))
    context = ShapeContext(WireShape.OPENAI_RESPONSE, "test", "gpt-6.1-sol", capabilities={
        "sampling_policy": "supported",
    })
    codec = codec_for_shape(context.wire_shape)
    assert codec.encode(request, context).payload["temperature"] == 0.2
    restricted = replace(context, capabilities={"sampling_policy": "supported", "supports_temperature": False})
    assert "temperature" not in codec.encode(request, restricted).payload
    assert "temperature" not in codec.encode(request, replace(context, capabilities={
        "sampling_policy": "supported", "unsupported_request_parameters": ["temperature"],
    })).payload


@pytest.mark.parametrize("shape", [WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION])
def test_final_guard_filters_projected_payload_and_extra_body(shape):
    context = ShapeContext(shape, "test", "openai/gpt-6.1-sol")
    payload = {"temperature": 0.3, "top_p": 0.9, "top_logprobs": 2}
    if shape == WireShape.OPENAI_COMPLETION:
        payload["logprobs"] = True
    else:
        payload["include"] = ["reasoning.encrypted_content", "message.output_text.logprobs"]
    encoded = EncodedRequest(payload, (), extra_body=dict(payload))
    filtered = apply_provider_request_hooks(encoded, context)
    expected = {} if shape == WireShape.OPENAI_COMPLETION else {"include": ["reasoning.encrypted_content"]}
    assert filtered.payload == expected
    assert filtered.extra_body == expected
    assert "temperature" in encoded.payload


@pytest.mark.parametrize("shape", [WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION])
@pytest.mark.parametrize("effort", [None, "high", "none"])
def test_final_guard_respects_effective_wire_reasoning_in_extra_body(shape, effort):
    context = ShapeContext(shape, "test", "gpt-6-luna")
    payload = {"temperature": 0.2, "top_p": 0.9, "top_logprobs": 2}
    if shape == WireShape.OPENAI_RESPONSE:
        payload["reasoning"] = {"effort": "none"}
        payload["include"] = ["reasoning.encrypted_content", "message.output_text.logprobs"]
        extra = {"reasoning": {"effort": effort}}
    else:
        payload["reasoning_effort"] = "none"
        payload["logprobs"] = True
        extra = {"reasoning_effort": effort}
    encoded = EncodedRequest(payload, (), extra_body=extra)
    filtered = apply_provider_request_hooks(encoded, context)
    for parameter in ("temperature", "top_p", "top_logprobs"):
        assert (parameter in filtered.payload) == (effort == "none")
    if shape == WireShape.OPENAI_RESPONSE:
        assert ("message.output_text.logprobs" in filtered.payload["include"]) == (effort == "none")
    else:
        assert ("logprobs" in filtered.payload) == (effort == "none")


def test_absent_sampling_override_keeps_defaults_and_other_capabilities():
    capabilities = {"sampling_policy": None, "prompt_cache": {"mode": "implicit"}}
    context = ShapeContext(WireShape.OPENAI_RESPONSE, "test", "gpt-6.1-sol", capabilities=capabilities)
    assert context.capabilities["sampling_policy"] == "unsupported"
    assert context.capabilities["prompt_cache"] == capabilities["prompt_cache"]
    assert capabilities["sampling_policy"] is None


@pytest.mark.parametrize("reasoning", [None, {}, {"summary": "auto"}])
def test_extra_body_replacing_reasoning_does_not_keep_old_disabled_effort(reasoning):
    context = ShapeContext(WireShape.OPENAI_RESPONSE, "test", "gpt-6-luna")
    encoded = EncodedRequest(
        {"reasoning": {"effort": "none"}, "temperature": 0.2}, (),
        extra_body={"reasoning": reasoning, "top_p": 0.8},
    )
    filtered = apply_provider_request_hooks(encoded, context)
    assert "temperature" not in filtered.payload
    assert "top_p" not in filtered.extra_body


@pytest.mark.parametrize("shape", [WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION])
def test_sampling_guard_reads_only_the_shapes_reasoning_field(shape):
    context = ShapeContext(shape, "test", "gpt-6-luna")
    wrong_field = {"reasoning_effort": "none"} if shape == WireShape.OPENAI_RESPONSE else {"reasoning": {"effort": "none"}}
    filtered = apply_provider_request_hooks(EncodedRequest({"temperature": 0.2, **wrong_field}, ()), context)
    assert "temperature" not in filtered.payload


@pytest.mark.parametrize("subscription", [False, True])
def test_subscription_keeps_its_stricter_parameter_filter(subscription):
    capabilities = {"access_profile": "openai_chatgpt"} if subscription else {}
    context = ShapeContext(WireShape.OPENAI_RESPONSE, "test", "gpt-6-luna", capabilities=capabilities)
    request = LLMRequestIR(messages=(), tools=(), policy=GenerationPolicyIR(
        max_output_tokens=128, temperature=0.2, thinking_level=ThinkingLevel.OFF,
    ))
    encoded = codec_for_shape(context.wire_shape).encode(request, context)
    filtered = apply_provider_request_hooks(encoded, context)
    assert ("temperature" in filtered.payload) == (not subscription)
    assert ("max_output_tokens" in filtered.payload) == (not subscription)


@pytest.mark.parametrize("projection", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("model", ["gpt-6.1-sol", "openai/gpt-6.1-sol"])
def test_transport_receives_filtered_requests_for_existing_endpoint_rows(model, stream, projection):
    ep = endpoint()
    ep.model_id = model
    ep.auth_kind, ep.capabilities_blob, ep.transport = "api_key_ref", {}, "http"
    ep.provider = "openrouter" if model.startswith("openai/") else "openai"
    port = _Port()
    transport = DirectSDKTransport(lambda spec: "test-credential", port)
    invoker = ShapeEndpointInvoker(transport=transport)
    request = LLMRequestIR(messages=(), tools=(), policy=GenerationPolicyIR(max_output_tokens=128, temperature=0.2))
    encoded = None
    if projection:
        context = ShapeContext(WireShape.OPENAI_RESPONSE, ep.endpoint_id, model)
        encoded = codec_for_shape(context.wire_shape).encode(request, context)
        encoded = replace(encoded, payload={**encoded.payload, "temperature": 0.2}, extra_body={"top_p": 0.9})
    try:
        response, _ = invoker.invoke(ep, request, stream=stream, projection=encoded)
        assert response.text == "OK"
        sent = port.requests[0]
        assert "temperature" not in sent.payload
        assert "top_p" not in sent.extra_body
        assert sent.stream == stream
        assert ep.capabilities_blob == {}
    finally:
        invoker.close()


@pytest.mark.parametrize("policy,shape", [
    ("bad", "openai_response"), ([], "openai_response"),
    ("requires_no_reasoning", "anthropic_messages"),
])
def test_invalid_sampling_configuration_fails_before_transport(policy, shape):
    ep = endpoint()
    ep.auth_kind, ep.transport, ep.wire_shape = "api_key_ref", "http", shape
    ep.capabilities_blob = {"sampling_policy": policy}
    with pytest.raises(LLMEndpointSpecError, match="sampling_policy"):
        LLMEndpointSpec.from_value(ep)
