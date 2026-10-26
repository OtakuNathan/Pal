"""Runtime consumes semantic invocation contracts, never provider grammars."""
import pytest

from pal.core.runtime_config import RuntimeConfig
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.invocation_contracts import ManagedEndpointInvokerPort
from pal.llm.response_hooks import EndpointResponseHook, ProviderResponseHookRegistry
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.llm.shapes.base import _JSONFrame
from tests.test_llm_runtime_ir import _endpoint, _request, _Settings, _SequenceInvoker


class IndependentInvoker:
    """A composition implementing the contract without subclassing the codec invoker."""

    def __init__(self, delegate):
        self.delegate = delegate
        self.prepared = []
        self.subscriptions = []
        self.forks = 0

    @property
    def response_hooks(self):
        return self.delegate.response_hooks

    def configure_response_hooks(self, registry):
        self.delegate.configure_response_hooks(registry)

    def configure_attempt_sink(self, sink):
        self.delegate.configure_attempt_sink(sink)

    def prepare_endpoints(self, endpoints):
        self.prepared = tuple(endpoints)
        self.delegate.prepare_endpoints(self.prepared)

    def resume_subscription(self, endpoint):
        self.subscriptions.append(endpoint.endpoint_id)

    def fork_transient(self):
        self.forks += 1
        return IndependentInvoker(self.delegate.fork_transient())

    def invoke(self, endpoint, request, **kwargs):
        return self.delegate.invoke(endpoint, request, **kwargs)

    def invoke_updates(self, endpoint, request, **kwargs):
        yield from self.delegate.invoke_updates(endpoint, request, **kwargs)


@pytest.mark.parametrize("streaming", [False, True])
def test_independent_invoker_owns_adaptation_accounting_and_receipts(tmp_path, streaming):
    import asyncio

    admissions = []

    def hook(context, updates):
        admissions.append(context.endpoint_id)
        yield from updates

    class Transport:
        count = 0

        def frames(self, endpoint, request):
            self.count += 1
            request.on_submitted()
            yield _JSONFrame(0, {"choices": [{
                "message": {"role": "assistant", "content": "first " if self.count == 1 else "second"},
                "finish_reason": "length" if self.count == 1 else "stop",
            }]})

    ep = _endpoint()
    ep.context_window = 100_000
    ep.max_output_tokens = 100
    ep.capabilities_blob = {"max_output_recovery": {"enabled": True, "upper_limit": 100, "max_continuations": 1}}
    registry = ProviderResponseHookRegistry({}, {ep.endpoint_id: EndpointResponseHook(ep.endpoint_id, hook)})
    invoker = IndependentInvoker(ShapeEndpointInvoker(transport=Transport(), response_hooks=registry))
    assert isinstance(invoker, ManagedEndpointInvokerPort)
    runtime = LLMRuntime(EndpointResolver(endpoints=(ep,)), _Settings(), endpoint_invoker=invoker,
                         config=RuntimeConfig(runtime_root=tmp_path))
    assert invoker.prepared == (ep,)
    submissions = []
    if streaming:
        async def consume():
            return [update async for update in runtime.astream(_request(), on_submitted=submissions.append)][-1].response
        response = asyncio.run(consume())
    else:
        response = runtime.generate(_request(), on_submitted=submissions.append).response
    assert response.text == "first second"
    assert admissions == [ep.endpoint_id, ep.endpoint_id]
    assert runtime.usage_snapshot()["provider_request_count"] == 2
    assert len(submissions) == 1
    assert submissions[0].endpoint_id == ep.endpoint_id
    runtime.resume_subscription(ep.endpoint_id)
    assert invoker.subscriptions == [ep.endpoint_id]
    child = runtime.fork_transient()
    assert invoker.forks == 1
    assert child.endpoint_invoker is not invoker
    runtime.refresh_llm_endpoints()
    assert invoker.response_hooks is runtime.provider_response_hooks


def test_semantic_invoker_output_is_not_reinterpreted_after_recovery(tmp_path):
    from pal.llm.contracts import generation_result_from_values

    token = "｜DSML｜"
    example = (f'<{token}tool_calls><{token}invoke name="example">'
               f'</{token}invoke></{token}tool_calls>')
    responses = [generation_result_from_values(text=example[:30], finish_reason="length").response,
                 generation_result_from_values(text=example[30:]).response]
    invoker = _SequenceInvoker(responses)
    ep = _endpoint(provider="deepseek")
    ep.max_output_tokens = 100
    ep.capabilities_blob = {"max_output_recovery": {"enabled": True, "upper_limit": 100, "max_continuations": 1}}
    runtime = LLMRuntime(EndpointResolver(endpoints=(ep,)), _Settings(), endpoint_invoker=invoker,
                         config=RuntimeConfig(runtime_root=tmp_path))
    result = runtime.generate(_request())
    assert result.text == example
    assert result.tool_calls == ()
    assert invoker.attempts == 2
