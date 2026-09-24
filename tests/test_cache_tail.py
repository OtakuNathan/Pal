from dataclasses import replace
import json

import httpx
from openai import OpenAI
import pytest

from pal.llm.cache_policy import CacheProfileError, available_modes, resolve_mode
from pal.llm.cache_wire import audit, clean_request, digest, protocol_nodes
from pal.llm.ir import LLMUsageIR, TextPartIR, WireShape
from pal.llm.prompt_cache import PromptCacheCoordinator
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext, _provider_prefix
from pal.shared.json_values import thaw_json
from tests.test_prompt_cache_policy import _request, _active_tool_request, _extend_active_tool_request


def context(mode="explicit", shape=WireShape.OPENAI_RESPONSE, provider="openai"):
    return ShapeContext(shape, "endpoint", "gpt-6-astra", provider_id=provider,
                        capabilities={"prompt_cache": {"mode": mode}})


def send(coordinator, request, ctx, identity):
    raw = codec_for_shape(ctx.wire_shape).encode(request, ctx)
    return coordinator.prepare_attempt(request, ctx, raw, identity)


@pytest.mark.parametrize("model", ["openai/gpt-6-luna", "openai/gpt-6-sol"])
def test_unverified_openrouter_models_do_not_enable_explicit_controls(model):
    ctx = replace(context("hybrid", provider="openrouter"), model_id=model)
    assert available_modes(ctx) == ("implicit",)
    with pytest.raises(CacheProfileError):
        resolve_mode(ctx)
    assert resolve_mode(replace(ctx, capabilities={"prompt_cache": {"mode": "implicit"}})) == "implicit"


@pytest.mark.parametrize("shape", [WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION])
@pytest.mark.parametrize("provider", ["openai", "openrouter"])
@pytest.mark.parametrize("mode,count", [("implicit", 0), ("hybrid", 2), ("explicit", 3)])
def test_modes_final_payload(shape, provider, mode, count):
    ctx, co = context(mode, shape, provider), PromptCacheCoordinator()
    request = _active_tool_request(_request())
    plan, encoded, _ = send(co, request, ctx, "first")
    found = [path for path, node in protocol_nodes(thaw_json(encoded.payload)) if "prompt_cache_breakpoint" in node]
    assert len(found) == count
    assert plan.mode == mode
    if mode == "implicit":
        assert "prompt_cache_options" not in encoded.extra_body
    else:
        assert audit(encoded, tuple(p.path for p in plan.breakpoints), mode=mode,
                     gateway=provider == "openrouter")[0]
        if provider == "openrouter" and mode == "hybrid":
            assert "prompt_cache_options" not in encoded.extra_body
        else:
            assert thaw_json(encoded.extra_body["prompt_cache_options"]) == {
                "mode": "explicit" if mode == "explicit" else "implicit", "ttl": "30m"}
    if mode == "explicit":
        assert plan.breakpoints[-1].label == "tail_current"
        assert plan.breakpoints[-1].message_id == request.messages[-2].message_id


@pytest.mark.parametrize("shape", [WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION])
def test_openrouter_hybrid_sdk_body_and_reject_incompatible_options(shape):
    # OR's PromptCacheOptions schema accepts only "explicit". Omitting the
    # object keeps automatic caching enabled alongside the block markers:
    # https://openrouter.ai/docs/guides/best-practices/prompt-caching
    co, ctx = PromptCacheCoordinator(), context("hybrid", shape, "openrouter")
    request = _active_tool_request(_request())
    plan, encoded, _ = send(co, request, ctx, "hybrid-wire")
    captured = []

    def capture(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "offline", "object": "response"})

    with OpenAI(api_key="offline-test", base_url="https://example.test/v1",
                http_client=httpx.Client(transport=httpx.MockTransport(capture))) as client:
        resource = client.responses if shape == WireShape.OPENAI_RESPONSE else client.chat.completions
        with resource.with_streaming_response.create(
            **thaw_json(encoded.payload), extra_body=thaw_json(encoded.extra_body), stream=False
        ):
            pass
    body, = captured
    assert "extra_body" not in body
    assert "prompt_cache_options" not in body
    assert body["session_id"] == body["prompt_cache_key"] == plan.cache_key
    paths = tuple(path for path, node in protocol_nodes(body) if "prompt_cache_breakpoint" in node)
    assert paths == tuple(point.path for point in plan.breakpoints)
    assert len(paths) == 2
    for options in ({"mode": "implicit", "ttl": "30m"}, {"mode": "explicit", "ttl": "30m"}, None):
        bad = replace(encoded, extra_body={**thaw_json(encoded.extra_body), "prompt_cache_options": options})
        assert not audit(bad, paths, mode="hybrid", gateway=True)[0]


@pytest.mark.parametrize("shape", [WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION])
def test_eager_progresses_without_any_usage_and_retries_keep_previous(shape):
    co, ctx = PromptCacheCoordinator(rolling_net_threshold_tokens=10**12), context(shape=shape)
    request = _active_tool_request(_request())
    previous = None
    for round_ in range(12):
        plan, encoded, _ = send(co, request, ctx, str(round_))
        current = plan.breakpoints[-1]
        assert current.label == "tail_current"
        if previous:
            assert plan.breakpoints[-2].message_id == previous.message_id
            assert len(plan.breakpoints) == 4
        # Planning repeatedly and retrying the same boundary cannot consume P.
        snapshot = co.snapshot()["tail"]
        for _ in range(3):
            built = co.plan(request, ctx)
            assert co.snapshot()["tail"] == snapshot
        retry, _, _ = send(co, request, ctx, f"retry-{round_}")
        assert retry.breakpoints == plan.breakpoints
        co.record_attempt(plan, status="failed", request_id=str(round_))
        assert co.snapshot()["tail"]["tails"] == snapshot["tails"]
        previous = current
        request = _extend_active_tool_request(request, suffix=str(round_))


def test_usage_and_late_receipts_do_not_change_positions():
    co, ctx = PromptCacheCoordinator(), context()
    request = _active_tool_request(_request())
    old, _, _ = send(co, request, ctx, "old")
    newer = _extend_active_tool_request(request, suffix="new")
    new, _, _ = send(co, newer, ctx, "new")
    before = co.snapshot()["tail"]
    for plan, identity in ((new, "new"), (old, "old"), (old, "old")):
        co.record_success(plan, LLMUsageIR(input_tokens=50000, cached_input_tokens=45000, reported=True), request_id=identity)
    assert co.snapshot()["tail"] == before


def test_turn_prefix_and_mode_changes_discard_tail_history():
    co, ctx = PromptCacheCoordinator(), context()
    request = replace(_active_tool_request(_request()), metadata={"turn_id": "one"})
    first, _, _ = send(co, request, ctx, "first")
    co.end_turn("one")
    assert co.snapshot()["tail"]["tails"] == []
    next_turn = replace(request, metadata={"turn_id": "two"})
    plan = co.plan(next_turn, ctx)
    assert not any(p.label == "tail_previous" for p in plan.breakpoints)
    send(co, next_turn, ctx, "second")
    changed = replace(next_turn, messages=(replace(next_turn.messages[0], parts=(TextPartIR("different system"),)), *next_turn.messages[1:]))
    plan = co.plan(changed, ctx)
    assert co.snapshot()["tail"]["tails"] == []
    assert not any(p.label == "tail_previous" for p in plan.breakpoints)
    hybrid, encoded, _ = send(co, changed, context("hybrid"), "hybrid")
    assert len(hybrid.breakpoints) == 2
    co.record_success(first, LLMUsageIR(), request_id="first")
    assert co.snapshot()["tail"]["tails"] == []


def test_partial_batch_and_tampered_marker_are_rejected():
    co, ctx = PromptCacheCoordinator(), context()
    complete = _active_tool_request(_request())
    partial = replace(complete, messages=(*complete.messages[:-2], complete.messages[-1]))
    assert len(co.plan(partial, ctx).breakpoints) == 2
    raw = codec_for_shape(ctx.wire_shape).encode(complete, ctx)
    plan = co.plan(complete, ctx, raw)
    encoded = co.inject(raw, plan)
    tampered = replace(encoded, extra_body={**encoded.extra_body, "cache_control": {"type": "ephemeral"}})
    with pytest.raises(CacheProfileError):
        co.start_attempt(plan, request=complete, context=ctx, encoded=tampered, raw_encoded=raw, request_id="bad")
    assert co.snapshot()["tail"]["tails"] == []


def test_capabilities_defaults_overrides_and_unknown_models():
    ctx = replace(context(), capabilities={})
    assert resolve_mode(ctx) == "implicit"
    assert available_modes(ctx) == ("implicit", "hybrid", "explicit")
    assert resolve_mode(replace(ctx, capabilities={"prompt_cache": {
        "mode": "hybrid", "cache_profile": "obsolete", "dialect": "obsolete"}})) == "hybrid"
    assert resolve_mode(replace(ctx, capabilities={"prompt_cache": {"mode": "invalid", "enabled": False}})) == "disabled"
    for unknown in (replace(context(), provider_id="compatible"), replace(context(), model_id="gpt-99")):
        with pytest.raises(CacheProfileError):
            resolve_mode(unknown)
    anthro = replace(context(), provider_id="anthropic", wire_shape=WireShape.ANTHROPIC_MESSAGES)
    assert resolve_mode(anthro) == "explicit"
    with pytest.raises(CacheProfileError):
        resolve_mode(replace(anthro, capabilities={"prompt_cache": {"mode": "hybrid"}}))


def test_mixed_scopes_are_bounded_and_old_receipt_does_not_evict_current_tail():
    co = PromptCacheCoordinator(max_scope_count=1)
    request = _active_tool_request(_request())
    old, _, _ = send(co, request, context(), "old")
    current = replace(request, logical_scope_id="another-session")
    send(co, current, context(), "new")
    before = co.snapshot()["tail"]
    co.record_success(old, LLMUsageIR(reported=True), request_id="old")
    assert co.snapshot()["tail"] == before
    assert co.snapshot()["scope_count"] == 1
    send(co, current, context("implicit"), "implicit")
    assert co.snapshot()["scope_count"] == 1


def test_content_tampering_and_stale_generation_cannot_be_submitted():
    co, ctx = PromptCacheCoordinator(), context()
    request = replace(_active_tool_request(_request()), metadata={"turn_id": "one"})
    raw = codec_for_shape(ctx.wire_shape).encode(request, ctx)
    plan = co.plan(request, ctx, raw)
    encoded = co.inject(raw, plan)
    payload = thaw_json(encoded.payload)
    payload["input"][0]["content"][0]["text"] = "changed instructions"
    with pytest.raises(CacheProfileError):
        co.start_attempt(plan, request=request, context=ctx, raw_encoded=raw,
                         encoded=replace(encoded, payload=payload), request_id="tampered")
    co.end_turn("one")
    with pytest.raises(CacheProfileError):
        co.start_attempt(plan, request=request, context=ctx, raw_encoded=raw,
                         encoded=encoded, request_id="stale")
    assert co.snapshot()["tail"]["tails"] == []


class Provider:
    """Independent explicit cache: word counts, exact prefix keys and optional failed writes."""
    def __init__(self):
        self.entries = {}

    def respond(self, encoded, fail_last=False):
        clean = thaw_json(clean_request(encoded).payload)
        points = []
        for path, node in protocol_nodes(thaw_json(encoded.payload)):
            if "prompt_cache_breakpoint" in node:
                prefix = _provider_prefix(clean, path)
                # Distinct from the client's serialization-length estimate.
                count = len(str(prefix).split())
                points.append((digest(prefix), count))
        hit = max((self.entries.get(key, 0) for key, _ in points), default=0)
        for key, count in (points[:-1] if fail_last else points):
            self.entries[key] = count
        return hit, max((count for _, count in points), default=0)


@pytest.mark.parametrize("fail", [False, True])
def test_independent_provider_reuse_and_documented_fallback(fail):
    co, ctx, server = PromptCacheCoordinator(), context(), Provider()
    request = _active_tool_request(_request())
    _, first, _ = send(co, request, ctx, "1")
    hit1, end1 = server.respond(first)
    assert hit1 == 0
    request = _extend_active_tool_request(request, suffix="2")
    _, second, _ = send(co, request, ctx, "2")
    hit2, end2 = server.respond(second, fail_last=fail)
    assert hit2 == end1
    request = _extend_active_tool_request(request, suffix="3")
    _, third, _ = send(co, request, ctx, "3")
    hit3, end3 = server.respond(third)
    assert end3 > end2 > end1
    assert hit3 < end1 if fail else hit3 == end2
