"""Transport admission, rather than request preparation, owns cache-tail history."""
from dataclasses import replace

import pytest

from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.prompt_cache import PromptCacheCoordinator
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import _JSONFrame
from tests.test_cache_tail import context
from tests.test_prompt_cache_policy import _request, _active_tool_request, _extend_active_tool_request
from tests.test_v3_n3_vertical_trace import _endpoint


class Transport:
    def __init__(self, *, admit=False, frame=False, duplicate=False):
        self.admit, self.frame, self.duplicate = admit, frame, duplicate
        self.request = None

    def frames(self, endpoint, request):
        self.request = request
        if self.admit:
            request.on_submitted()
            if self.duplicate:
                request.on_submitted()
        if self.frame:
            yield _JSONFrame(0, {"choices": [{"message": {"role": "assistant", "content": "ok"},
                                            "finish_reason": "stop"}]})
        else:
            raise RuntimeError("offline transport failure")


def invoke(coordinator, request, transport):
    endpoint = _endpoint()
    endpoint.model_id = "gpt-6-astra"
    endpoint.capabilities_blob = {"prompt_cache": {"mode": "explicit"}}
    receipts = []
    invoker = ShapeEndpointInvoker(transport=transport, prompt_cache=coordinator)
    try:
        invoker.invoke(endpoint, request, submission_sink=receipts.append)
    except RuntimeError as exc:
        assert str(exc) == "offline transport failure"
    return receipts


def test_rejection_before_admission_does_not_consume_tail_sequence():
    co = PromptCacheCoordinator()
    receipts = invoke(co, _active_tool_request(_request()), Transport())
    assert receipts == []
    assert co.snapshot()["tail"]["submitted_sequence"] == 0
    assert co.snapshot()["tail"]["tails"] == []


def test_preparation_alone_does_not_submit():
    co, ctx = PromptCacheCoordinator(), context()
    request = _active_tool_request(_request())
    co.prepare_attempt(request, ctx, codec_for_shape(ctx.wire_shape).encode(request, ctx), "prepared")
    assert co.snapshot()["tail"]["submitted_sequence"] == 0
    assert co.snapshot()["tail"]["tails"] == []


@pytest.mark.parametrize("transport", [Transport(admit=True), Transport(frame=True),
                                       Transport(admit=True, frame=True, duplicate=True)])
def test_admission_commits_once_even_if_transport_later_fails(transport):
    co = PromptCacheCoordinator()
    assert len(invoke(co, _active_tool_request(_request()), transport)) == 1
    snapshot = co.snapshot()["tail"]
    assert snapshot["submitted_sequence"] == 1
    assert len(snapshot["tails"]) == 1
    transport.request.on_submitted()
    assert co.snapshot()["tail"] == snapshot


def test_rejected_later_tail_does_not_evict_admitted_history():
    co = PromptCacheCoordinator()
    request = _active_tool_request(_request())
    invoke(co, request, Transport(admit=True))
    before = co.snapshot()["tail"]
    for suffix in ("two", "three"):
        request = _extend_active_tool_request(request, suffix=suffix)
        invoke(co, request, Transport())
    assert co.snapshot()["tail"]["tails"] == before["tails"]
    assert co.snapshot()["tail"]["submitted_sequence"] == before["submitted_sequence"]


def test_late_admission_after_turn_close_cannot_revive_tail():
    co = PromptCacheCoordinator()
    request = replace(_active_tool_request(_request()), metadata={"turn_id": "closed"})
    transport = Transport()
    invoke(co, request, transport)
    co.end_turn("closed")
    before = co.snapshot()["tail"]
    transport.request.on_submitted()
    assert co.snapshot()["tail"] == before


def test_late_admission_cannot_commit_into_evicted_and_recreated_scope():
    co, ctx = PromptCacheCoordinator(max_scope_count=1), context()
    request = _active_tool_request(_request())

    def prepare(value, identity):
        return co.prepare_attempt(value, ctx, codec_for_shape(ctx.wire_shape).encode(value, ctx), identity)

    old, _, old_diagnostics = prepare(request, "old")
    prepare(replace(request, logical_scope_id="other-scope"), "other")
    current, _, diagnostics = prepare(request, "current")
    before = co.snapshot()["tail"]
    co.submit_attempt(old, request_id="old", diagnostics=old_diagnostics)
    assert co.snapshot()["tail"] == before
    co.submit_attempt(current, request_id="current", diagnostics=diagnostics)
    assert len(co.snapshot()["tail"]["tails"]) == 1


def test_diagnostics_settle_prepared_and_admitted_attempts():
    for admitted in (False, True):
        co = PromptCacheCoordinator()
        invoke(co, _active_tool_request(_request()), Transport(admit=admitted))
        record, = co.snapshot()["recent_attempts"]
        assert record["status"] == "failed"


@pytest.mark.parametrize("reverse", [False, True])
def test_overlapping_preparations_each_commit_once(reverse):
    co, ctx = PromptCacheCoordinator(), context()
    first = _active_tool_request(_request())
    second = _extend_active_tool_request(first, suffix="overlapping")
    attempts = [(identity, *co.prepare_attempt(request, ctx, codec_for_shape(ctx.wire_shape).encode(request, ctx), identity))
                for request, identity in ((first, "first"), (second, "second"))]
    newest_tail = attempts[1][1].tail_current
    if reverse:
        attempts.reverse()
    for identity, plan, _, diagnostics in attempts:
        co.submit_attempt(plan, request_id=identity, diagnostics=diagnostics)
    snapshot = co.snapshot()["tail"]
    assert snapshot["submitted_sequence"] == 2
    assert snapshot["tails"][-1]["message_id"] == newest_tail.message_id
    assert len(snapshot["tails"]) == (1 if reverse else 2)
    for identity, plan, _, diagnostics in attempts:
        co.submit_attempt(plan, request_id=identity, diagnostics=diagnostics)
    assert co.snapshot()["tail"] == snapshot
