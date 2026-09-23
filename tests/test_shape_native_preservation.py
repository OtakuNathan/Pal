"""Provider-owned continuation controls and opaque citation persistence."""
from dataclasses import replace

import pytest

from pal.llm.request_hooks import apply_provider_request_hooks
from pal.llm.endpoint_spec import LLMEndpointSpec, LLMEndpointSpecError
from pal.llm.ir import WireShape
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext, ShapeDecodeError, _JSONFrame
from pal.memory import MemoryService
from pal.memory.runtime_state import MemoryRuntimeStatePort
from pal.shared.json_values import thaw_json
from tests.test_llm_runtime_ir import _endpoint, _request


@pytest.mark.parametrize("shape,protocol,parameter", [
    (WireShape.OPENAI_COMPLETION, "glm", "thinking.clear_thinking"),
    (WireShape.ANTHROPIC_MESSAGES, "anthropic", "context_management"),
])
def test_preservation_is_opt_in_and_rejects_unsupported_parameter(shape, protocol, parameter):
    codec = codec_for_shape(shape)
    ctx = ShapeContext(shape, "ep", "model")
    assert not apply_provider_request_hooks(codec.encode(_request(), ctx), ctx).extra_body
    ctx = replace(ctx, capabilities={"preserved_thinking": protocol})
    encoded = apply_provider_request_hooks(codec.encode(_request(), ctx), ctx)
    if protocol == "glm":
        assert thaw_json(encoded.extra_body) == {"thinking": {"clear_thinking": False}}
    else:
        assert encoded.payload["extra_headers"]["anthropic-beta"] == "context-management-2025-06-27"
        assert thaw_json(encoded.extra_body) == {
            "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}}
    ctx = replace(ctx, capabilities={"preserved_thinking": protocol,
                                   "unsupported_request_parameters": [parameter]})
    with pytest.raises(ShapeDecodeError):
        apply_provider_request_hooks(codec.encode(_request(), ctx), ctx)


@pytest.mark.parametrize("shape,protocol,valid", [
    ("openai_completion", "glm", True),
    ("anthropic_messages", "anthropic", True),
    ("openai_response", "glm", False),
    ("openai_completion", "anthropic", False),
    ("anthropic_messages", True, False),
])
def test_preservation_protocol_binding(shape, protocol, valid):
    endpoint = _endpoint()
    endpoint.wire_shape = shape
    endpoint.capabilities_blob = {"preserved_thinking": protocol}
    if valid:
        assert LLMEndpointSpec.from_value(endpoint).capabilities_blob["preserved_thinking"] == protocol
    else:
        with pytest.raises(LLMEndpointSpecError):
            LLMEndpointSpec.from_value(endpoint)


def test_stream_citations_survive_settlement_restore_and_reencoding():
    shape = WireShape.ANTHROPIC_MESSAGES
    ctx = ShapeContext(shape, "ep", "model")
    codec = codec_for_shape(shape)
    citations = [
        {"type": "char_location", "document_index": 0, "start_char_index": 0,
         "end_char_index": 3, "cited_text": "abc", "document_title": None,
         "vendor_extension": {"opaque": ["unchanged", 1]}},
        {"type": "page_location", "document_index": 1, "start_page_number": 1,
         "end_page_number": 2, "cited_text": "xyz"},
    ]
    events = [
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": "", "citations": []}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "answer"}},
        *[{"type": "content_block_delta", "index": 0,
           "delta": {"type": "citations_delta", "citation": c}} for c in citations],
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]
    decoded = list(codec.decode(iter(_JSONFrame(i, e) for i, e in enumerate(events)), ctx))[-1].response
    source = MemoryService()
    source.begin_l1_turn("T", user_text="hello")
    source.upsert_l1_assistant("T", decoded.message)
    source.settle_l1_turn("T")
    restored = MemoryService()
    port = MemoryRuntimeStatePort(restored)
    port.install_prepared_state(port.prepare_restore_state(MemoryRuntimeStatePort(source).snapshot_state()))
    message = restored.l1_store.turns.turns[-1].messages[-1]
    encoded = codec.encode(replace(_request(), messages=(message,)), ctx)
    assert thaw_json(encoded.payload["messages"])[-1]["content"] == [
        {"type": "text", "text": "answer", "citations": citations}]
    assert message.text == "answer"


def test_malformed_citation_is_not_silently_discarded():
    ctx = ShapeContext(WireShape.ANTHROPIC_MESSAGES, "ep", "model")
    frames = [_JSONFrame(0, {"type": "content_block_delta", "index": 0,
                            "delta": {"type": "citations_delta", "citation": "invalid"}})]
    with pytest.raises(ShapeDecodeError, match="citation object"):
        list(codec_for_shape(ctx.wire_shape).decode(iter(frames), ctx))


@pytest.mark.parametrize("protocol", ["glm", "anthropic"])
def test_sdk_sends_native_controls_in_body_and_beta_in_header(protocol):
    import json
    import httpx
    from anthropic import Anthropic
    from openai import OpenAI

    captured = []

    def respond(request):
        captured.append(request)
        if protocol == "glm":
            body = {"id": "x", "object": "chat.completion", "created": 0,
                    "model": "model", "choices": []}
        else:
            body = {"id": "x", "type": "message", "role": "assistant", "model": "model",
                    "content": [], "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0}}
        return httpx.Response(200, json=body)

    shape = WireShape.OPENAI_COMPLETION if protocol == "glm" else WireShape.ANTHROPIC_MESSAGES
    ctx = ShapeContext(shape, "ep", "model", capabilities={"preserved_thinking": protocol})
    encoded = apply_provider_request_hooks(codec_for_shape(shape).encode(_request(), ctx), ctx)
    client_type = OpenAI if protocol == "glm" else Anthropic
    with client_type(api_key="offline", base_url="https://offline.invalid",
                     http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        resource = client.chat.completions if protocol == "glm" else client.messages
        resource.create(**thaw_json(encoded.payload), extra_body=thaw_json(encoded.extra_body))
    body = json.loads(captured[0].content)
    assert "extra_headers" not in body and "extra_body" not in body
    if protocol == "glm":
        assert body["thinking"]["clear_thinking"] is False
    else:
        assert body["context_management"]["edits"][0]["keep"] == "all"
        assert captured[0].headers["anthropic-beta"] == "context-management-2025-06-27"


@pytest.mark.parametrize("projected", [False, True])
def test_invoker_applies_provider_hook_to_both_encode_paths(projected):
    from pal.llm.endpoint import ShapeEndpointInvoker

    endpoint = _endpoint()
    endpoint.wire_shape = "anthropic_messages"
    endpoint.capabilities_blob = {"preserved_thinking": "anthropic"}
    captured = []

    class Transport:
        def frames(self, endpoint, request):
            captured.append(request)
            yield _JSONFrame(0, {"content": [{"type": "text", "text": "done"}],
                                 "stop_reason": "end_turn"})

    ctx = ShapeContext(WireShape.ANTHROPIC_MESSAGES, endpoint.endpoint_id, endpoint.model_id,
                       capabilities=endpoint.capabilities_blob)
    raw = codec_for_shape(ctx.wire_shape).encode(_request(), ctx)
    assert "extra_headers" not in raw.payload and not raw.extra_body
    invoker = ShapeEndpointInvoker(transport=Transport())
    invoker.invoke(endpoint, _request(), projection=raw if projected else None)
    assert captured[0].extra_body["context_management"]["edits"][0]["keep"] == "all"
    assert captured[0].payload["extra_headers"]["anthropic-beta"] == "context-management-2025-06-27"
    assert not raw.extra_body  # Applying the hook must not mutate the projection.


def test_provider_hook_preserves_existing_controls_and_is_idempotent():
    from pal.llm.shapes.base import EncodedRequest

    ctx = ShapeContext(WireShape.ANTHROPIC_MESSAGES, "ep", "model",
                       capabilities={"preserved_thinking": "anthropic"})
    encoded = EncodedRequest(
        {"extra_headers": {"Anthropic-Beta": "other-beta", "custom": "keep"}},
        extra_body={"context_management": {"edits": [
            {"type": "clear_thinking_20251015", "keep": "none"},
            {"type": "clear_tool_uses_20250919"}]}},
    )
    hooked = apply_provider_request_hooks(encoded, ctx)
    assert apply_provider_request_hooks(hooked, ctx) == hooked
    assert hooked.payload["extra_headers"]["Anthropic-Beta"] == "other-beta,context-management-2025-06-27"
    assert hooked.payload["extra_headers"]["custom"] == "keep"
    assert hooked.extra_body["context_management"]["edits"] == [
        {"type": "clear_thinking_20251015", "keep": "all"},
        {"type": "clear_tool_uses_20250919"}]


@pytest.mark.parametrize("reject", [False, True])
def test_provider_controls_are_finalized_before_cache_audit_and_submission(reject):
    from pal.llm.cache_diagnostics import describe_request
    from pal.llm.prompt_cache import PromptCacheCoordinator

    class Coordinator(PromptCacheCoordinator):
        def record_attempt(self, plan, **kwargs):
            records.append(kwargs)

    records = []
    capabilities = {"preserved_thinking": "glm"}
    if reject:
        capabilities["unsupported_request_parameters"] = ["thinking.clear_thinking"]
    ctx = ShapeContext(WireShape.OPENAI_COMPLETION, "ep", "model", capabilities=capabilities)
    raw = codec_for_shape(ctx.wire_shape).encode(_request(), ctx)
    coordinator = Coordinator()

    def prepare():
        return coordinator.prepare_attempt(_request(), ctx, raw, "request",
            finalize_request=lambda encoded: apply_provider_request_hooks(encoded, ctx))

    if reject:
        with pytest.raises(ShapeDecodeError):
            prepare()
        assert not records
    else:
        _, encoded, diagnostics = prepare()
        assert diagnostics["wire_hash"] == describe_request(_request(), raw, encoded)["wire_hash"]
        assert diagnostics["wire_hash"] != describe_request(_request(), raw, raw)["wire_hash"]
        assert records[0]["wire_hash"] == diagnostics["wire_hash"]


def test_glm_hook_moves_preexisting_thinking_to_sdk_extension():
    from pal.llm.shapes.base import EncodedRequest

    raw = EncodedRequest({"thinking": {"type": "enabled"}},
                         extra_body={"session_id": "keep"})
    ctx = ShapeContext(WireShape.OPENAI_COMPLETION, "ep", "model",
                       capabilities={"preserved_thinking": "glm"})
    encoded = apply_provider_request_hooks(raw, ctx)
    assert "thinking" not in encoded.payload
    assert encoded.extra_body == {"session_id": "keep",
                                  "thinking": {"type": "enabled", "clear_thinking": False}}
    assert raw.payload["thinking"] == {"type": "enabled"}
