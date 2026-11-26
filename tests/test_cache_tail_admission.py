"""Transport admission records evidence without owning marker history."""
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


def test_rejection_before_admission_is_not_transport_admitted():
    co = PromptCacheCoordinator()
    assert invoke(co, _active_tool_request(_request()), Transport()) == []
    record, = co.snapshot()["recent_attempts"]
    assert not record["transport_admitted"]
    assert record["status"] == "failed"


def test_preparation_alone_does_not_submit():
    co, ctx = PromptCacheCoordinator(), context()
    request = _active_tool_request(_request())
    plan, _, _ = co.prepare_attempt(request, ctx, codec_for_shape(ctx.wire_shape).encode(request, ctx), "prepared")
    assert not plan.admission.observed
    assert not co.snapshot()["recent_attempts"][-1]["transport_admitted"]


@pytest.mark.parametrize("transport", [Transport(admit=True), Transport(frame=True),
                                        Transport(admit=True, frame=True, duplicate=True)])
def test_admission_commits_once_even_if_transport_later_fails(transport):
    co = PromptCacheCoordinator()
    assert len(invoke(co, _active_tool_request(_request()), transport)) == 1
    snapshot = co.snapshot()["recent_attempts"]
    assert len(snapshot) == 1 and snapshot[0]["transport_admitted"]
    transport.request.on_submitted()
    assert co.snapshot()["recent_attempts"] == snapshot


def test_rejected_later_attempt_does_not_change_pure_selection():
    co, ctx = PromptCacheCoordinator(), context()
    request = _active_tool_request(_request())
    invoke(co, request, Transport(admit=True))
    for suffix in ("two", "three"):
        request = _extend_active_tool_request(request, suffix=suffix)
        invoke(co, request, Transport())
    assert [r["transport_admitted"] for r in co.snapshot()["recent_attempts"]] == [True, False, False]
    assert co.plan(request, ctx).breakpoints == PromptCacheCoordinator().plan(request, ctx).breakpoints


def test_late_admission_after_turn_close_does_not_replace_terminal_record():
    co = PromptCacheCoordinator()
    request = replace(_active_tool_request(_request()), metadata={"turn_id": "closed"})
    transport = Transport()
    invoke(co, request, transport)
    co.end_turn("closed")
    before = co.snapshot()["recent_attempts"]
    transport.request.on_submitted()
    assert co.snapshot()["recent_attempts"] == before


def test_late_admission_cannot_displace_current_scope():
    co, ctx = PromptCacheCoordinator(max_scope_count=1), context()
    request = _active_tool_request(_request())
    def prepare(value, identity):
        return co.prepare_attempt(value, ctx, codec_for_shape(ctx.wire_shape).encode(value, ctx), identity)
    old, _, old_diagnostics = prepare(request, "old")
    prepare(replace(request, logical_scope_id="other-scope"), "other")
    current, _, diagnostics = prepare(request, "current")
    co.submit_attempt(old, request_id="old", diagnostics=old_diagnostics)
    assert co.snapshot()["last_scope_key"] == current.scope_key
    co.submit_attempt(current, request_id="current", diagnostics=diagnostics)
    assert co.snapshot()["scope_count"] == 1
    assert co.plan(request, ctx).breakpoints == current.breakpoints


@pytest.mark.parametrize("reverse", [False, True])
def test_overlapping_preparations_each_commit_once(reverse):
    co, ctx = PromptCacheCoordinator(), context()
    first = _active_tool_request(_request())
    second = _extend_active_tool_request(first, suffix="overlapping")
    attempts = [(identity, *co.prepare_attempt(request, ctx, codec_for_shape(ctx.wire_shape).encode(request, ctx), identity))
                for request, identity in ((first, "first"), (second, "second"))]
    newest = attempts[1][1]
    if reverse:
        attempts.reverse()
    for identity, plan, _, diagnostics in attempts:
        co.submit_attempt(plan, request_id=identity, diagnostics=diagnostics)
    assert all(plan.admission.observed for _, plan, _, _ in attempts)
    snapshot = co.snapshot()["recent_attempts"]
    assert len(snapshot) == 2 and all(r["transport_admitted"] for r in snapshot)
    for identity, plan, _, diagnostics in attempts:
        co.submit_attempt(plan, request_id=identity, diagnostics=diagnostics)
    assert co.snapshot()["recent_attempts"] == snapshot
    assert co.plan(second, ctx).breakpoints == newest.breakpoints
