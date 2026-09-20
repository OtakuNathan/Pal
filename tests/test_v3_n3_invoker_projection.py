"""N3 slice 1: the invoker accepts an owner-prepared projection verbatim.

NEXT_STEPS §4.1: the projection materializes at the raw-codec encode point.
This first slice proves the typed channel — an immutable EncodedRequest
prepared owner-side (here: from a real EndpointProjectionSession
prepare_normal) reaches the transport byte-identically, feeds the cache
coordinator like a codec encode would, and decodes normally.  The runtime
owner-side wiring and the accept->observe_commit closure land with the
full N3 vertical.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import replace

from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.ir import (
    GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole, TextPartIR,
    WireShape,
)
from pal.llm.models import LLMEndpointModel
from pal.llm.projection_contracts import (
    AppendReceipt, AttemptKey, EndpointBinding, HistoryCommitReceipt,
    HistoryCursor, LogicalSessionId, OwnerFence,
)
from pal.llm.projection_session import EndpointProjectionSession, HistoryView
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import EncodedRequest
from tests.test_llm_response_hooks import _JSONFrame
from tests.test_v3_n1_root_lifecycle import assistant, user


def endpoint() -> LLMEndpointModel:
    return LLMEndpointModel(
        endpoint_id="proj-endpoint", provider="openai", model_id="proj-model",
        display_name="Proj", wire_shape="openai_completion",
        base_url="https://example.test/v1", auth_kind="api_key_ref",
        credential_ref="key", context_window=100_000, max_output_tokens=4_096,
        thinking_levels_blob=["off"], default_thinking_level="off",
        supports_tools=True, supports_streaming=True, supports_vision=False,
        input_modalities_blob=["text"], output_modalities_blob=["text"],
        priority=0, enabled=True, capabilities_blob={},
    )


class CapturingTransport:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.captured = None

    def frames(self, _endpoint, transport_request):
        self.captured = transport_request
        for index, payload in enumerate(self.payloads):
            yield _JSONFrame(index, payload)

    def activate_endpoint(self, endpoint_id):
        pass

    def close(self):
        pass


def prepared_projection() -> tuple[EncodedRequest, EndpointProjectionSession]:
    """A real session-prepared normal request for a two-round lineage."""
    session = EndpointProjectionSession(LogicalSessionId('n3:resident'))
    session.bind(EndpointBinding(
        endpoint_id="proj-endpoint", model_id="proj-model",
        wire_shape=WireShape.OPENAI_COMPLETION, endpoint_spec_revision="r1",
        continuation_policy_version="p1", config_fingerprint="f1"))
    r1 = (user('first q', 'u1'), assistant('first a', 'a1'))
    attempt = AttemptKey(session.identity, OwnerFence(0), 'round-1')
    session.begin_round(attempt, requires_native=False)
    session.prepare_normal(HistoryView(cursor=session.frontier, messages=r1))
    session.observe_commit(HistoryCommitReceipt(
        attempt=attempt,
        append=AppendReceipt(
            before=session.frontier,
            after=HistoryCursor(0, 1, 'a' * 32), block_count=1),
        closed_call_ids=(), native_committed=False),
        accepted_messages=(), span_message_ids=[m.message_id for m in r1])
    tail = (user('second q', 'u2'),)
    attempt2 = AttemptKey(session.identity, OwnerFence(0), 'round-2')
    session.begin_round(attempt2, requires_native=False)
    prepared = session.prepare_normal(
        HistoryView(cursor=session.frontier, messages=tail))
    encoded = EncodedRequest(
        payload=json.loads(prepared.payload_json),
        message_spans=tuple(prepared.message_spans),
        extra_body=dict(prepared.extra_body),
        applied_cache_breakpoint_message_ids=tuple(
            prepared.applied_cache_breakpoint_message_ids),
    )
    return encoded, session


class InvokerProjectionChannelTests(unittest.TestCase):
    def test_owner_prepared_projection_reaches_transport_verbatim(self):
        projection, _session = prepared_projection()
        frames = [{
            "choices": [{"message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }]
        transport = CapturingTransport(frames)
        invoker = ShapeEndpointInvoker(transport=transport)
        request = LLMRequestIR(
            messages=(user('second q', 'u2'),),
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=256),
        )
        response, updates = invoker.invoke(
            endpoint(), request, projection=projection)
        self.assertEqual(transport.captured.payload, dict(projection.payload))
        self.assertIn('first a', json.dumps(dict(transport.captured.payload)))
        self.assertIn('ok', updates[-1].response.message.text)

    def test_without_projection_the_codec_path_is_unchanged(self):
        frames = [{
            "choices": [{"message": {"role": "assistant", "content": "plain"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }]
        transport = CapturingTransport(frames)
        invoker = ShapeEndpointInvoker(transport=transport)
        request = LLMRequestIR(
            messages=(user('q', 'q1'),),
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=256),
        )
        _response, updates = invoker.invoke(endpoint(), request)
        codec = codec_for_shape(WireShape.OPENAI_COMPLETION)
        expected = codec.encode(request, _context_for(endpoint()))
        self.assertEqual(transport.captured.payload, dict(expected.payload))
        self.assertIn('plain', updates[-1].response.message.text)


def _context_for(model: LLMEndpointModel):
    from pal.llm.shapes.base import ShapeContext

    return ShapeContext(
        wire_shape=WireShape(str(model.wire_shape)),
        endpoint_id=str(model.endpoint_id), model_id=str(model.model_id),
        provider_id=str(model.provider), base_url=str(model.base_url or ""),
        capabilities=dict(model.capabilities_blob or {}),
    )


if __name__ == '__main__':
    unittest.main()
