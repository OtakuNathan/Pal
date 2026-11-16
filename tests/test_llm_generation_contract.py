"""The semantic stream is independent of the provider's HTTP response mode."""
from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest

from pal.core.runtime_config import RuntimeConfig
from pal.llm.contracts import collect_generation
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.ir import LLMResponseDeltaKind
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.llm.shapes.base import _JSONFrame
from tests.test_llm_runtime_ir import _endpoint, _request, _Settings


class Transport:
    def __init__(self, barrier=None):
        self.barrier = barrier
        self.requests = []

    def frames(self, endpoint, request):
        self.requests.append(request)
        request.on_submitted()
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        if request.stream:
            yield _JSONFrame(0, {"choices": [{"delta": {"content": endpoint.endpoint_id}, "finish_reason": None}]})
            yield _JSONFrame(1, {"choices": [{"delta": {}, "finish_reason": "stop"}]})
        else:
            yield _JSONFrame(0, {"choices": [{"message": {"role": "assistant", "content": endpoint.endpoint_id},
                                            "finish_reason": "stop"}]})


@pytest.mark.parametrize("streaming", [False, True])
def test_one_generation_interface_for_both_http_modes(tmp_path, streaming):
    endpoint = _endpoint()
    endpoint.supports_streaming = streaming
    transport = Transport()
    runtime = LLMRuntime(EndpointResolver(endpoints=(endpoint,)), _Settings(),
        endpoint_invoker=ShapeEndpointInvoker(transport=transport), config=RuntimeConfig(runtime_root=tmp_path))

    async def scenario():
        updates = [update async for update in runtime.generate(_request())]
        assert transport.requests[0].stream is streaming
        assert updates[-1].completion.text == endpoint.endpoint_id
        assert updates[-1].completion.preferred_endpoint_id == endpoint.endpoint_id
        assert updates[-1].completion.preferred_model_id == endpoint.model_id
        if streaming:
            assert any(update.delta_kind is LLMResponseDeltaKind.TEXT for update in updates)
        else:
            assert len(updates) == 1
            assert updates[0].delta_kind is LLMResponseDeltaKind.STATE
    asyncio.run(scenario())


def test_shared_runtime_keeps_concurrent_call_facts_separate(tmp_path):
    endpoints = (_endpoint("main", model_id="main-model"), _endpoint("side", model_id="side-model"))
    transport = Transport(threading.Barrier(2))
    runtime = LLMRuntime(EndpointResolver(endpoints=endpoints), _Settings(active="main"),
        endpoint_invoker=ShapeEndpointInvoker(transport=transport), config=RuntimeConfig(runtime_root=tmp_path))

    async def scenario():
        results = await asyncio.gather(*(collect_generation(runtime.generate(replace(_request(),
            logical_scope_id=endpoint.endpoint_id, metadata={"preferred_endpoint_id": endpoint.endpoint_id})))
            for endpoint in endpoints))
        for endpoint, result in zip(endpoints, results):
            assert (result.text, result.preferred_endpoint_id, result.preferred_model_id) == (
                endpoint.endpoint_id, endpoint.endpoint_id, endpoint.model_id)
            assert result.projection_receipt is None
    asyncio.run(scenario())


def test_budget_rejection_retains_request_local_compaction_advice(tmp_path):
    endpoint = _endpoint()
    transport = Transport()
    runtime = LLMRuntime(EndpointResolver(endpoints=(endpoint,)), _Settings(),
        endpoint_invoker=ShapeEndpointInvoker(transport=transport), config=RuntimeConfig(runtime_root=tmp_path))

    result = asyncio.run(collect_generation(runtime.generate(_request("large input " * 20_000))))
    assert result.finish_reason == "compact_required"
    assert result.target_input_budget > 0
    assert result.reserved_output_tokens == 100
    assert result.preferred_endpoint_id == endpoint.endpoint_id
    assert transport.requests == []
