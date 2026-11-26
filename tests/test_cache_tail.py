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
    plan, encoded, diagnostics = coordinator.prepare_attempt(request, ctx, raw, identity)
    coordinator.submit_attempt(plan, request_id=identity, diagnostics=diagnostics)
    return plan, encoded, diagnostics


@pytest.mark.parametrize("model", [
    "openai/gpt-6.2-terra",
    "openai/gpt-6x2-astra", "openai/gpt-6.2-solar", "other/gpt-6.2-astra",
])
def test_unverified_openrouter_models_do_not_enable_explicit_controls(model):
    ctx = replace(context("hybrid", provider="openrouter"), model_id=model)
    assert available_modes(ctx) == ("implicit",)
    with pytest.raises(CacheProfileError):
        resolve_mode(ctx)
    assert resolve_mode(replace(ctx, capabilities={"prompt_cache": {"mode": "implicit"}})) == "implicit"


@pytest.mark.parametrize("shape", [WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION])
@pytest.mark.parametrize("provider", ["openai", "openrouter"])
@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.2-astra", "gpt-6.2-sol"])
@pytest.mark.parametrize("mode,count", [("implicit", 0), ("hybrid", 2), ("explicit", 4)])
def test_modes_final_payload(shape, provider, model, mode, count):
    model_id = f"openai/{model}" if provider == "openrouter" else model
    ctx = replace(context(mode, shape, provider), model_id=model_id)
    co = PromptCacheCoordinator()
    request = _active_tool_request(_request())
    plan, encoded, _ = send(co, request, ctx, "first")
    found = [path for path, node in protocol_nodes(thaw_json(encoded.payload)) if "prompt_cache_breakpoint" in node]
    assert len(found) == count
    assert plan.mode == mode
    if shape == WireShape.OPENAI_RESPONSE:
        assert "instructions" not in encoded.payload and "instructions" not in encoded.extra_body
        assert encoded.payload["input"][0]["content"][0]["text"] == request.messages[0].parts[0].text
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
def test_chase_tail_progresses_without_any_usage_and_retries_keep_previous(shape):
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
        for _ in range(3):
            built = co.plan(request, ctx)
            assert built.breakpoints == plan.breakpoints
        retry, _, _ = send(co, request, ctx, f"retry-{round_}")
        assert retry.breakpoints == plan.breakpoints
        co.record_attempt(plan, status="failed", request_id=str(round_))
        assert co.plan(request, ctx).breakpoints == plan.breakpoints
        previous = current
        request = _extend_active_tool_request(request, suffix=str(round_))


def test_usage_and_late_receipts_do_not_change_positions():
    co, ctx = PromptCacheCoordinator(), context()
    request = _active_tool_request(_request())
    old, _, _ = send(co, request, ctx, "old")
    newer = _extend_active_tool_request(request, suffix="new")
    new, _, _ = send(co, newer, ctx, "new")
    before = co.plan(newer, ctx).breakpoints
    for plan, identity in ((new, "new"), (old, "old"), (old, "old")):
        co.record_success(plan, LLMUsageIR(input_tokens=50000, cached_input_tokens=45000, reported=True), request_id=identity)
    assert co.plan(newer, ctx).breakpoints == before


def test_turn_changes_keep_session_anchors_and_rederive_recent_paths():
    co, ctx = PromptCacheCoordinator(), context()
    request = replace(_active_tool_request(_request()), metadata={"turn_id": "one"})
    first, _, _ = send(co, request, ctx, "first")
    co.end_turn("one")
    next_turn = replace(_extend_active_tool_request(request, suffix="two"), metadata={"turn_id": "two"})
    plan = co.plan(next_turn, ctx)
    assert plan.breakpoints[:2] == first.breakpoints[:2]
    assert plan.breakpoints[-2].path == first.breakpoints[-1].path
    assert plan.breakpoints[-1].path > first.breakpoints[-1].path
    assert PromptCacheCoordinator().plan(next_turn, ctx).breakpoints == plan.breakpoints
    changed = replace(next_turn, messages=(replace(next_turn.messages[0], parts=(TextPartIR("different system"),)), *next_turn.messages[1:]))
    from pal.llm.cache_wire import boundary_at
    changed_plan = co.plan(changed, ctx)
    assert boundary_at(changed_plan.prepared_encoded, changed_plan.breakpoints[0].message_id, changed_plan.breakpoints[0].path).fingerprint != boundary_at(plan.prepared_encoded, plan.breakpoints[0].message_id, plan.breakpoints[0].path).fingerprint
    hybrid, _, _ = send(co, changed, context("hybrid"), "hybrid")
    assert len(hybrid.breakpoints) == 2
    co.record_success(first, LLMUsageIR(), request_id="first")
    assert co.plan(next_turn, ctx).breakpoints == plan.breakpoints


def test_partial_batch_and_tampered_marker_are_rejected():
    co, ctx = PromptCacheCoordinator(), context()
    complete = _active_tool_request(_request())
    partial = replace(complete, messages=(*complete.messages[:-2], complete.messages[-1]))
    assert {p.message_id for p in co.plan(partial, ctx).breakpoints} == {m.message_id for m in complete.messages[:3]}
    raw = codec_for_shape(ctx.wire_shape).encode(complete, ctx)
    plan = co.plan(complete, ctx, raw)
    encoded = co.inject(raw, plan)
    tampered = replace(encoded, extra_body={**encoded.extra_body, "cache_control": {"type": "ephemeral"}})
    with pytest.raises(CacheProfileError):
        co.start_attempt(plan, request=complete, context=ctx, encoded=tampered, raw_encoded=raw, request_id="bad")
    assert co.snapshot()["tail"] is None


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
    before = co.snapshot()["last_scope_key"]
    co.record_success(old, LLMUsageIR(reported=True), request_id="old")
    assert co.snapshot()["last_scope_key"] == before
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
    assert co.snapshot()["tail"] is None


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


@pytest.mark.parametrize("provider,url", [("openai", "https://api.openai.com/v1"), ("openrouter", "https://openrouter.ai/api/v1")])
def test_52_lookup_markers_keep_first_two_and_latest_fifty_across_turns(provider, url):
    from pal.llm.ir import LLMMessageIR, MessageRole, PromptRegionIR
    ctx = replace(context(provider=provider), base_url=url)
    request = _request()
    history = tuple(LLMMessageIR(MessageRole.USER, (TextPartIR(f"Entry {i}"),), message_id=f"entry-{i}", prompt_region=PromptRegionIR.SETTLED_HISTORY) for i in range(100))
    request = replace(request, messages=(request.messages[0], *history), metadata={"turn_id": "first"})
    co = PromptCacheCoordinator()
    first, encoded, _ = send(co, request, ctx, "first")
    assert len(first.breakpoints) == 52
    assert [p.message_id for p in first.breakpoints] == [request.messages[0].message_id, "entry-0", *[f"entry-{i}" for i in range(50,100)]]
    co.end_turn("first")
    next_request = replace(request, messages=(*request.messages, replace(history[-1], message_id="next", parts=(TextPartIR("Next"),))), metadata={"turn_id": "next"})
    second, _, _ = send(co, next_request, ctx, "second")
    assert second.scope_key == first.scope_key
    assert second.cache_key == first.cache_key
    assert co.snapshot()["recent_attempts"][-1]["prefix_preserved"]
    assert second.breakpoints[:2] == first.breakpoints[:2]
    assert [p.path for p in second.breakpoints[2:-1]] == [p.path for p in first.breakpoints[3:]]
    assert second.breakpoints[-1].message_id == "next"
    assert PromptCacheCoordinator().plan(next_request, ctx).breakpoints == second.breakpoints
    assert audit(encoded, tuple(p.path for p in first.breakpoints), lookup_limit=52)[0]


def test_explicit_side_branch_shares_routing_but_not_session_diagnostics():
    co, ctx = PromptCacheCoordinator(), context()
    request = replace(_active_tool_request(_request()), metadata={"turn_id": "main"})
    main, _, _ = send(co, request, ctx, "main")
    anchor = dict(co._anchor_evidence)
    branch = replace(request, metadata={"turn_id": "side", "transient_history": True})
    side, _, _ = send(co, branch, ctx, "side")
    assert main.cache_key == side.cache_key
    assert main.scope_key != side.scope_key
    assert main.breakpoints == side.breakpoints
    assert co.snapshot()["last_scope_key"] == main.scope_key
    assert co._anchor_evidence == anchor


def test_warm_semantic_compact_uses_own_admission_scope_not_closed_turn():
    from pal.llm.compaction_engine import CompactionEngine, CompactionSnapshot
    from pal.llm.resident_compaction import PalCompactionPolicy
    policy = PalCompactionPolicy()
    ctx = context()
    co = PromptCacheCoordinator()
    original = replace(_active_tool_request(_request()), metadata={"turn_id": "closed"})
    main, _, _ = send(co, original, ctx, "main")
    anchor = co.eligible_anchor_request()["request"]
    co.end_turn("closed")
    snapshot = CompactionSnapshot(target_input_budget=8192, reserved_output_tokens=512,
        clock_kind=policy.clock_kind, clock_value=1, replay_request=anchor, replay_wire_shape="openai_response",
        metadata={"compaction_op_id": "test-run"})
    compact = CompactionEngine(policy)._request(snapshot, "fixture", attempt=1)
    plan, _, diagnostics = send(co, compact, ctx, "compact")
    assert plan.turn_id == "compact:test-run"
    assert plan.scope_key != main.scope_key
    assert plan.cache_key == main.cache_key
    assert diagnostics["operation"] == "semantic_compact"
    assert co.snapshot()["last_scope_key"] == main.scope_key
