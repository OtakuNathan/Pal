from dataclasses import replace

import pytest

from pal.llm.cache_handoff import Boundary, Bound, Handoff, Candidate, audit, clean_request
from pal.llm.ir import GenerationPolicyIR, LLMUsageIR, LLMMessageIR, LLMRequestIR, MessageRole, PromptRegionIR, TextPartIR, WireShape
from pal.llm.prompt_cache import PromptCacheCoordinator
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.shared.json_values import thaw_json


def usage(total, hit):
    return LLMUsageIR(input_tokens=total, cached_input_tokens=hit, reported=True, final=True,
        reported_fields=("input_tokens", "cached_input_tokens"), input_accounting="inclusive_cache")


def boundary(name, pos):
    return Boundary(name, ("input", pos, "content", 0), name, pos)


def state():
    s = Handoff(stable=boundary("s", 40000), stable_read=True)
    s.bounds["s"] = Bound(40000, "bootstrap", 1)
    s.propose(boundary("c", 60000), "frontier", minimum=0, read=.1, write=1.25, threshold=0)
    return s


def submit(s, name, p=70000, suffix=10000):
    s.submit(name, target=boundary(name, p), suffix=suffix, round_index=s.sequence,
             audited=True, wire_fingerprint="audit", now=1)


def test_two_sums_and_same_hit_ack():
    s = state()
    submit(s, "one")
    s.settle("one", usage(70000, 60000), success=True, now=2)
    submit(s, "two", 80000, 20000)
    s.settle("two", usage(80000, 60000), success=True, now=3)
    assert s.pending is None and s.baseline == 60000
    assert s.r == 30000 and s.ack_source == "estimated"
    s.settle("two", usage(80000, 60000), success=True, now=4)
    assert s.r == 30000


def test_abort_preserves_cost_and_third_response_can_ack():
    s = state()
    submit(s, "one")
    s.settle("one", usage(70000, 40000), success=True, now=2)
    submit(s, "two", 80000)
    s.settle("two", usage(80000, 40000), success=True, now=3)
    assert (s.pending.through, s.pending.after) == (40000, 30000)
    submit(s, "three", 80000)
    assert s.pending.attempts == 3
    s.settle("three", usage(80000, 60000), success=True, now=4)
    assert s.r == 50000 and s.promotions == 1
    s = state()
    submit(s, "one")
    s.settle("one", usage(70000, 40000), success=True, now=2)
    submit(s, "two", 80000)
    s.settle("two", usage(80000, 40000), success=True, now=3)
    s.end_candidate("abandon")
    assert s.r == 70000


def test_unknown_bound_and_inseparable_are_not_provider_failures():
    s = state()
    s.bounds.clear()
    submit(s, "one")
    s.settle("one", usage(70000, 60000), success=True, now=2)
    assert s.promotions == 0
    s = state()
    s.bounds["s"] = Bound(90000, "independent", 1)
    submit(s, "one", 92000, 10000)
    s.settle("one", usage(92000, 82000), success=True, now=2)
    assert s.decision == "candidate_inseparable_from_old_bounds"
    assert s.pending is None and s.cooldown is None


def test_late_attempt_uses_current_split_without_stale_promotion():
    s = state()
    submit(s, "late", 80000)
    submit(s, "first", 70000)
    s.settle("first", usage(70000, 40000), success=True, now=2)
    s.owner += 1  # Same content epoch, but this response has lost ACK authority.
    s.settle("late", usage(80000, 60000), success=True, now=3)
    assert s.promotions == 0
    assert (s.pending.through, s.pending.after) == (40000, 30000)
    s.settle("late", usage(80000, 60000), success=True, now=4)
    assert s.pending.after == 30000


class ExplicitProvider:
    """Independent toy tokenizer and cache, driven only by actual wire markers.

    Token positions count whitespace words, deliberately unlike char/4 planning.
    Tool definitions are included in the prefix identity by the codec fingerprint.
    """
    def __init__(self):
        self.cache = set()

    def respond(self, encoded):
        from pal.llm.cache_handoff import protocol_nodes, digest
        from pal.llm.shapes.base import _provider_prefix
        data = thaw_json(encoded.payload)
        count, hit = 0, 0
        writes = []
        for path, node in protocol_nodes(data):
            if len(path) != 4:
                continue
            count += len(str(node.get("text", "")).split())
            if "prompt_cache_breakpoint" in node:
                clean = clean_request(encoded)
                fingerprint = digest(_provider_prefix(thaw_json(clean.payload), path))
                if fingerprint in self.cache:
                    hit = max(hit, count)
                writes.append(fingerprint)
        self.cache.update(writes)
        return usage(count, hit)


@pytest.mark.parametrize("shape", [WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION])
@pytest.mark.parametrize("provider", ["openai", "openrouter"])
def test_ten_single_round_turns_always_send_current_anchor(shape, provider):
    context = ShapeContext(shape, "endpoint", "openai/gpt-6-astra" if provider == "openrouter" else "gpt-6-astra",
                           provider_id=provider, capabilities={"prompt_cache": {"minimum_tokens": 1, "net_threshold_tokens": 0}})
    coordinator, server = PromptCacheCoordinator(), ExplicitProvider()
    messages = [LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("abc " * 1000),), message_id="system",
                             prompt_region=PromptRegionIR.STABLE_SYSTEM)]
    for turn in range(10):
        messages = [replace(m, prompt_region=PromptRegionIR.SETTLED_HISTORY)
                    if m.role != MessageRole.SYSTEM else m for m in messages]
        messages.append(LLMMessageIR(MessageRole.USER, (TextPartIR("abc " * 1000),),
                        message_id=f"user{turn}", prompt_region=PromptRegionIR.ACTIVE_INPUT))
        request = LLMRequestIR(tuple(messages), tools=(), policy=GenerationPolicyIR(max_output_tokens=128), logical_scope_id="session", metadata={"turn_id": str(turn)})
        raw = codec_for_shape(shape).encode(request, context)
        plan = coordinator.plan(request, context, raw)
        encoded = coordinator.inject(raw, plan)
        coordinator.start_attempt(plan, request=request, context=context, encoded=encoded, raw_encoded=raw, request_id=str(turn))
        coordinator.record_success(plan, server.respond(encoded), request_id=str(turn))
        current = coordinator.snapshot()["handoff"]
        assert [b.message_id for b in plan.breakpoints] == ["system", f"user{turn}"]
        assert current["pending"] is None
        assert current["promotions"] == 0
        coordinator.end_turn(str(turn))
    assert current["active_attempts"] == 0


def test_exact_audit_and_content_identity_survive_old_marker_removal():
    from pal.llm.cache_handoff import inject_exact
    from pal.llm.prompt_cache import PromptCacheBreakpoint
    from pal.llm.shapes.base import EncodedRequest, EncodedMessageSpan
    raw = EncodedRequest({"input": [{"role": "user", "content": [
        {"type": "input_text", "text": "hello", "cache_control": {"type": "ephemeral"}},
        {"type": "input_text", "text": "world"}]}],
        "tools": [{"type": "function", "parameters": {"properties": {"cache_control": {"type": "string"}}}}]},
        (EncodedMessageSpan("u", (("input", 0, "content", 0),)),
         EncodedMessageSpan("c", (("input", 0, "content", 1),))))
    one = inject_exact(raw, (PromptCacheBreakpoint("c", "c"),), "key", True)
    both = inject_exact(raw, (PromptCacheBreakpoint("u", "u"), PromptCacheBreakpoint("c", "c")), "key", True)
    assert clean_request(one).message_spans == clean_request(both).message_spans
    assert audit(one, (("input", 0, "content", 1),))[0]
    assert "cache_control" in one.payload["tools"][0]["parameters"]["properties"]
    tampered = replace(one, extra_body={**one.extra_body, "cache_control": {"type": "ephemeral"}})
    assert not audit(tampered, (("input", 0, "content", 1),))[0]


def test_new_turn_discards_trial_and_sends_new_fixed_anchor():
    s = state()
    submit(s, "one")
    s.settle("one", usage(70000, 40000), success=True, now=2)
    old_candidate = s.pending.boundary
    s.end_turn("")
    u = boundary("new-user", 80000)
    points = {b.message_id: b for b in (s.stable, old_candidate, u)}
    s.refresh("new", points, s.stable, 3, anchor=u)
    assert s.pending is None and s.frontier is None
    assert s.markers() == (s.stable, u)
    assert not s.anchor_read and s.baseline == 40000


def test_prefix_change_invalidates_candidate_but_not_raw_billing():
    s = state()
    submit(s, "one")
    s.settle("one", usage(70000, 40000), success=True, now=2)
    s.end_turn("")
    s.refresh("next", {"s": s.stable}, s.stable, 3)
    assert s.pending is None and s.economic_epoch >= 1
    assert s.r == 0
    # Usage accounting lives in LLMUsageLedger; invalidation only drops estimates.


def test_deadline_is_not_renewed_and_retry_rounds_do_not_advance_cooldown():
    s = state()
    for index in range(3):
        s.submit(str(index), target=boundary("p", 70000), suffix=10000, round_index=1,
                 audited=True, wire_fingerprint="hash", now=1)
        s.settle(str(index), usage(70000, 40000), success=True, now=2)
    assert s.pending is None and s.cooldown == set()
    for index, round_index in enumerate([1, 1, 2, 2, 3]):
        s.submit(f"cool{index}", target=None, suffix=None, round_index=round_index,
                 audited=True, wire_fingerprint="hash", now=3)
        s.settle(f"cool{index}", usage(70000, 40000), success=True, now=4)
    assert len(s.cooldown) == 2
    s.submit("finish", target=None, suffix=None, round_index=4, audited=True,
             wire_fingerprint="hash", now=5)
    s.settle("finish", usage(70000, 40000), success=True, now=6)
    assert s.cooldown is None
    s = state()
    submit(s, "one")
    s.settle("one", usage(70000, 40000), success=True, now=1802)
    assert s.pending is None and s.decision == "candidate_expired"


def test_submission_freezes_bounds_before_current_usage_can_tighten_them():
    s = state()
    submit(s, "one")
    s.settle("one", usage(70000, 40000), success=True, now=2)
    s.bounds["s"] = Bound(65000, "independent", 4)
    submit(s, "two")
    s.bounds["s"] = Bound(40000, "new-evidence", 5)
    s.settle("two", usage(70000, 60000), success=True, now=3)
    assert s.promotions == 0
    assert s.last_evidence["M"] == 65000


@pytest.mark.parametrize("field", ["usage_missing", "partial", "anomaly", "missing_hit", "write_only", "zero_input"])
def test_nonqualifying_evidence_never_calibrates(field):
    s = state()
    submit(s, "one")
    evidence = usage(70000, 60000)
    if field == "usage_missing":
        evidence = replace(evidence, reported=False)
    elif field == "partial":
        evidence = replace(evidence, final=False)
    elif field == "anomaly":
        evidence = replace(evidence, usage_anomaly="invalid_partition")
    elif field == "zero_input":
        evidence = usage(0, 0)
    elif field == "missing_hit":
        evidence = replace(evidence, reported_fields=("input_tokens",))
    else:
        evidence = replace(evidence, reported_fields=("cache_write_input_tokens",), cache_write_input_tokens=60000)
    s.settle("one", evidence, success=True, now=2)
    assert s.pending.calibration_sequence == 0 and s.promotions == 0


def test_exact_injection_does_not_fallback_when_last_path_is_invalid():
    from pal.llm.cache_handoff import inject_exact
    from pal.llm.prompt_cache import PromptCacheBreakpoint
    from pal.llm.shapes.base import EncodedRequest, EncodedMessageSpan
    raw = EncodedRequest({"input": [{"content": [{"type": "input_text", "text": "valid"}]}]},
        (EncodedMessageSpan("u", (("input", 0, "content", 0), ("input", 0, "content", 9))),))
    wire = inject_exact(raw, (PromptCacheBreakpoint("c", "u"),), "key", False)
    assert wire.applied_cache_breakpoint_message_ids == ()
    assert not audit(wire, (("input", 0, "content", 9),))[0]


def test_opaque_suffix_is_not_given_a_token_estimate():
    from pal.llm.cache_handoff import suffix_estimate
    from pal.llm.shapes.base import EncodedRequest
    encoded = EncodedRequest({"input": [{"content": [{"type": "input_text", "text": "user"}]},
        {"type": "reasoning", "encrypted_content": "opaque"}]})
    assert suffix_estimate(encoded, ("input", 0, "content", 0)) is None


def test_content_fingerprint_includes_tool_schema_and_call_identity():
    from pal.llm.shapes.base import EncodedRequest, EncodedMessageSpan
    raw = EncodedRequest({"input": [{"type": "function_call", "call_id": "one", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "one", "output": [{"type": "input_text", "text": "ok"}]}],
        "tools": [{"type": "function", "name": "test", "parameters": {"type": "object"}}]},
        (EncodedMessageSpan("tool", (("input", 1, "output", 0),)),))
    original = clean_request(raw).message_spans[0].cache_prefix_fingerprint
    changed = thaw_json(raw.payload)
    changed["input"][0]["call_id"] = "two"
    assert clean_request(replace(raw, payload=changed)).message_spans[0].cache_prefix_fingerprint != original
    changed = thaw_json(raw.payload)
    changed["tools"][0]["parameters"]["additionalProperties"] = False
    assert clean_request(replace(raw, payload=changed)).message_spans[0].cache_prefix_fingerprint != original


def test_provider_binding_change_cannot_ack_and_partial_round_does_not_cool():
    s = state()
    submit(s, "one")
    s.settle("one", usage(70000, 40000), success=True, now=2, actual_provider="a", returned_model="one")
    submit(s, "two")
    s.settle("two", usage(70000, 60000), success=True, now=3, actual_provider="b", returned_model="two")
    assert s.promotions == 0 and s.pending is None and s.bounds == {}
    assert s.decision == "provider_binding_changed"
    s.end_candidate("cooldown", cooldown=True)
    s.submit("length", target=None, suffix=None, round_index=12, audited=True, wire_fingerprint="hash", now=4)
    s.settle("length", usage(70000, 0), success=True, normal_round=False, now=5)
    assert s.cooldown == set()


def test_final_payload_content_tampering_cannot_calibrate():
    from tests.test_prompt_cache_policy import _request, _openai_context, _send_evidence, _active_tool_request
    coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
    request, context = _active_tool_request(_request()), _openai_context()
    _send_evidence(coordinator, request, context)
    raw = codec_for_shape(context.wire_shape).encode(request, context)
    plan = coordinator.plan(request, context, raw)
    encoded = coordinator.inject(raw, plan)
    payload = thaw_json(encoded.payload)
    payload["input"][0]["content"][0]["text"] += " changed"
    coordinator.start_attempt(plan, request=request, context=context, raw_encoded=raw,
        encoded=replace(encoded, payload=payload), request_id="tampered")
    coordinator.record_success(plan, usage(70000, 60000), request_id="tampered")
    snapshot = coordinator.snapshot()["handoff"]
    assert snapshot["pending"]["calibration_sequence"] == 0
    assert not snapshot["evidence"]["audited"]


def test_partial_tool_batch_has_no_candidate_boundary():
    from tests.test_prompt_cache_policy import _request, _openai_context, _send_evidence
    from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
    coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
    request, context = _request(), _openai_context()
    for _ in range(3):
        _send_evidence(coordinator, request, context)
    pending = replace(request, messages=(*request.messages[:-1],
        LLMMessageIR(MessageRole.ASSISTANT, (ToolCallIR("a", "tool", {}), ToolCallIR("b", "tool", {})),
                     prompt_region=PromptRegionIR.ACTIVE_HISTORY),
        LLMMessageIR(MessageRole.TOOL, (ToolResultIR("a", "tool", "one " * 900),),
                     prompt_region=PromptRegionIR.ACTIVE_HISTORY), request.messages[-1]))
    coordinator.plan(pending, context)
    assert coordinator.snapshot()["handoff"]["pending"] is None


def test_fourth_request_omits_pending_marker_without_destroying_third_ack():
    from tests.test_prompt_cache_policy import _request, _openai_context, _send_evidence, _active_tool_request
    coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
    request, context = _active_tool_request(_request()), _openai_context()
    _send_evidence(coordinator, _request(), context)
    _send_evidence(coordinator, request, context)
    raw = codec_for_shape(context.wire_shape).encode(request, context)
    for name in ("second", "third"):
        plan = coordinator.plan(request, context, raw)
        encoded = coordinator.inject(raw, plan)
        coordinator.start_attempt(plan, request=request, context=context, raw_encoded=raw, encoded=encoded, request_id=name)
    fourth = coordinator.plan(request, context, raw)
    assert fourth.handoff_candidate == ""
    assert not any(b.label.endswith("candidate") for b in fourth.breakpoints)
    state_ = coordinator._handoffs[fourth.scope_key]
    assert state_.pending.attempts == 3
    low, high = state_.pending.interval
    coordinator.record_success(plan, usage(high + 1, (low + high) // 2), request_id="third")
    assert coordinator.snapshot()["handoff"]["promotions"] == 1


def test_concurrent_preparation_cannot_reserve_four_carrying_attempts():
    from concurrent.futures import ThreadPoolExecutor
    from tests.test_prompt_cache_policy import _request, _openai_context, _send_evidence, _active_tool_request
    coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
    request, context = _active_tool_request(_request()), _openai_context()
    _send_evidence(coordinator, request, context)
    raw = codec_for_shape(context.wire_shape).encode(request, context)
    with ThreadPoolExecutor(max_workers=4) as pool:
        prepared = list(pool.map(lambda i: coordinator.prepare_attempt(request, context, raw, f"parallel{i}"), range(4)))
    assert sum(bool(plan.handoff_candidate) for plan, _, _ in prepared) == 3
    assert coordinator.snapshot()["handoff"]["pending"]["attempts"] == 3


def test_extending_stable_region_does_not_inherit_old_stable_ack():
    s = state()
    old = s.stable
    extended = boundary("extended-system", 50000)
    s.refresh("", {old.message_id: old, extended.message_id: extended,
                   s.pending.boundary.message_id: s.pending.boundary}, extended, 1)
    assert s.baseline == 0 and not s.stable_read
    assert s.pending is None and s.bounds == {}


def test_late_completed_round_from_before_cooldown_cannot_shorten_it():
    s = state()
    for i in range(3):
        s.submit(str(i), target=None, suffix=None, round_index=20, audited=True, wire_fingerprint="hash", now=1)
        s.settle(str(i), usage(70000, 0), success=True, now=2)
    assert s.cooldown == set()
    for i in range(10):
        s.submit(f"old{i}", target=None, suffix=None, round_index=i, audited=True, wire_fingerprint="hash", now=3)
        s.settle(f"old{i}", usage(70000, 0), success=True, now=4)
    assert s.cooldown == set()


def test_late_binding_report_does_not_invalidate_newer_state():
    s = state()
    submit(s, "old")
    submit(s, "new")
    s.settle("new", usage(70000, 40000), success=True, now=2, actual_provider="current")
    candidate = s.pending
    epoch = s.economic_epoch
    s.settle("old", usage(70000, 60000), success=True, now=3, actual_provider="old")
    assert s.pending is candidate and s.actual_provider == "current"
    assert s.economic_epoch == epoch and s.promotions == 0


def test_long_tool_turn_preserves_four_wire_positions_during_handoff():
    from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
    context = ShapeContext(WireShape.OPENAI_RESPONSE, "e", "gpt-6-astra", provider_id="openai",
        capabilities={"prompt_cache": {"minimum_tokens": 1, "net_threshold_tokens": 0}})
    coordinator, server = PromptCacheCoordinator(), ExplicitProvider()
    messages = [LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("abc " * 1000),), message_id="s",
                             prompt_region=PromptRegionIR.STABLE_SYSTEM)]
    max_markers = 0
    for n in range(15):
        turn = str(min(n, 2))
        if n < 3:
            messages = [replace(m, prompt_region=PromptRegionIR.SETTLED_HISTORY)
                        if m.role != MessageRole.SYSTEM else m for m in messages]
            messages.append(LLMMessageIR(MessageRole.USER, (TextPartIR("abc " * 1000),),
                            message_id=f"u{n}", prompt_region=PromptRegionIR.ACTIVE_INPUT))
        else:
            messages.extend((LLMMessageIR(MessageRole.ASSISTANT, (ToolCallIR(f"call{n}", "probe", {}),),
                message_id=f"a{n}", prompt_region=PromptRegionIR.ACTIVE_HISTORY),
                LLMMessageIR(MessageRole.TOOL, (ToolResultIR(f"call{n}", "probe", "abc " * 1000),),
                message_id=f"t{n}", prompt_region=PromptRegionIR.ACTIVE_HISTORY)))
        request = LLMRequestIR(tuple(messages), (), GenerationPolicyIR(max_output_tokens=128),
            logical_scope_id="long", metadata={"turn_id": turn, "llm_round_index": n})
        raw = codec_for_shape(context.wire_shape).encode(request, context)
        plan, wire, _ = coordinator.prepare_attempt(request, context, raw, f"req{n}")
        max_markers = max(max_markers, len(wire.applied_cache_breakpoint_message_ids))
        coordinator.record_success(plan, server.respond(wire), request_id=f"req{n}")
        if n < 2:
            coordinator.end_turn(turn)
    assert max_markers == 4
    assert coordinator.snapshot()["handoff"]["promotions"] >= 4


def test_turn_notification_supports_invokers_without_cache_policy():
    from pal.llm.runtime import LLMRuntime
    runtime = object.__new__(LLMRuntime)
    runtime.endpoint_invoker = object()
    runtime.end_prompt_cache_turn("closed")


def test_accounted_attempt_releases_inflight_snapshot_without_charging_again():
    from tests.test_prompt_cache_policy import _request, _openai_context, _send_evidence, _active_tool_request
    coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
    request, context = _active_tool_request(_request()), _openai_context()
    _send_evidence(coordinator, request, context)
    raw = codec_for_shape(context.wire_shape).encode(request, context)
    plan, _, _ = coordinator.prepare_attempt(request, context, raw, "duplicate")
    before = coordinator.snapshot()["handoff"]["pending"]["through_estimate"]
    coordinator.discard_accounted_attempt(plan, "duplicate")
    snapshot = coordinator.snapshot()["handoff"]
    assert snapshot["active_attempts"] == 0
    assert snapshot["pending"]["through_estimate"] == before


def test_provider_diagnostics_remain_serializable_auxiliary_data():
    import json
    from pal.llm.response_evidence import WireResponseEvidence
    from pal.llm.shapes.base import _JSONFrame
    evidence = WireResponseEvidence(WireShape.OPENAI_RESPONSE)
    evidence.observe(_JSONFrame(0, {"type": "response.completed", "response": {
        "prompt_cache_diagnostics": {"type": "cache_hit", "reason": {"code": "example"}},
    }}))
    assert json.loads(json.dumps(evidence.prompt_cache_diagnostics))["reason"]["code"] == "example"
    assert not evidence.usage.reported


def test_fixed_anchor_confirmation_preserves_pending_and_late_costs():
    s = state()
    s.anchor = boundary("u", 50000)
    s.bounds["u"] = Bound(50000, "independent", 2)
    submit(s, "one", 70000)
    s.settle("one", usage(70000, 40000), success=True, now=2)
    submit(s, "late", 80000, 20000)
    submit(s, "confirm-u", 70000)
    s.settle("confirm-u", usage(70000, 50000), success=True, now=3)
    assert s.anchor_read and s.baseline == 50000
    assert s.pending and (s.pending.through, s.pending.after) == (20000, 20000)
    # Already-sent response is charged against the current split, once.
    s.settle("late", usage(80000, 60000), success=True, now=4)
    assert s.frontier and s.baseline == 60000 and s.r == 40000
    s.settle("late", usage(80000, 60000), success=True, now=5)
    assert s.r == 40000


def test_fixed_anchor_uses_frozen_earlier_bounds_without_circular_evidence():
    s = Handoff(stable=boundary("s", 40000), anchor=boundary("u", 50000))
    submit(s, "unknown")
    s.settle("unknown", usage(70000, 50000), success=True, now=2)
    assert s.stable_read and not s.anchor_read and s.baseline == 40000
    # H may bound S after this decision; it cannot retrospectively confirm U.
    assert s.bounds["s"].upper == 50000
    submit(s, "freeze")
    s.bounds["s"] = Bound(40000, "later-independent", 10)
    s.settle("freeze", usage(70000, 50000), success=True, now=3)
    assert not s.anchor_read
    submit(s, "confirm")
    s.settle("confirm", usage(70000, 50000), success=True, now=4)
    assert s.anchor_read and s.baseline == 50000
    assert s.r == 60000  # Three requests, each with 20K beyond U.


@pytest.mark.parametrize("evidence", [LLMUsageIR(), replace(usage(70000, 0),
    cache_write_input_tokens=50000, reported_fields=("input_tokens", "cache_write_input_tokens"))])
def test_missing_read_or_write_only_never_confirms_fixed_anchors(evidence):
    s = Handoff(stable=boundary("s", 40000), anchor=boundary("u", 50000))
    submit(s, "one")
    s.settle("one", evidence, success=True, now=2)
    assert len(s.markers()) == 2 and s.baseline == 0
    assert not s.stable_read and not s.anchor_read


@pytest.mark.parametrize("shape", [WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION])
@pytest.mark.parametrize("provider", ["openai", "openrouter"])
def test_small_fixed_markers_ignore_economic_gate_and_failed_trials(shape, provider):
    context = ShapeContext(shape, "endpoint", "openai/gpt-6-astra" if provider == "openrouter" else "gpt-6-astra",
        provider_id=provider, capabilities={"prompt_cache": {"minimum_tokens": 100000, "net_threshold_tokens": 100000}})
    request = LLMRequestIR((
        LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("stable"),), message_id="s", prompt_region=PromptRegionIR.STABLE_SYSTEM),
        LLMMessageIR(MessageRole.USER, (TextPartIR("question"),), message_id="u", prompt_region=PromptRegionIR.ACTIVE_INPUT),
    ), tools=(), policy=GenerationPolicyIR(max_output_tokens=128), logical_scope_id="fixed", metadata={"turn_id": "one"})
    coordinator = PromptCacheCoordinator()
    raw = codec_for_shape(shape).encode(request, context)
    for i in range(5):
        plan = coordinator.plan(request, context, raw)
        assert [p.message_id for p in plan.breakpoints] == ["s", "u"]
        assert audit(coordinator.inject(raw, plan), tuple(p.path for p in plan.breakpoints))[0]
        s = coordinator._handoffs[plan.scope_key]
        s.cooldown = set()
        s.max_target = 999999
        s.deferred = ("u", s.revision)
    assert s.baseline == 0 and not s.pending


@pytest.mark.parametrize("shape", [WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION])
@pytest.mark.parametrize("provider", ["openai", "openrouter"])
def test_compact_anchor_is_exact_interior_block_then_advances_next_turn(shape, provider):
    from pal.memory.continuity import Continuity
    from pal.llm.cache_handoff import boundary_at
    context = ShapeContext(shape, "endpoint", "openai/gpt-6-astra" if provider == "openrouter" else "gpt-6-astra", provider_id=provider)
    system = LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("stable instructions"),), message_id="s", prompt_region=PromptRegionIR.STABLE_SYSTEM)
    user = LLMMessageIR(MessageRole.USER, (TextPartIR("current input"), TextPartIR("another input block")), message_id="u", prompt_region=PromptRegionIR.ACTIVE_INPUT)
    continuity = Continuity.from_message(LLMMessageIR(MessageRole.USER, (TextPartIR("summary"),), message_id="compact"), {"continuity_anchor": "u"})
    request = LLMRequestIR(tuple(continuity.project([system, user])), tools=(), policy=GenerationPolicyIR(max_output_tokens=128), logical_scope_id="compact", metadata={"turn_id": "one", "continuity_id": "compact"})
    coordinator = PromptCacheCoordinator()
    codec = codec_for_shape(shape)
    first = None
    for round_ in range(3):
        raw = codec.encode(request, context)
        plan = coordinator.plan(request, context, raw)
        u = plan.breakpoints[1]
        assert u.path[-2:] == ("content", 0)
        encoded = coordinator.inject(raw, plan)
        assert audit(encoded, tuple(p.path for p in plan.breakpoints))[0]
        if first is None:
            first = boundary_at(clean_request(encoded), u.message_id, u.path)
        assert boundary_at(clean_request(encoded), u.message_id, u.path) == first
        coordinator.start_attempt(plan, request=request, context=context, raw_encoded=raw, encoded=encoded, request_id=str(round_))
        coordinator.record_success(plan, usage(100, 0), request_id=str(round_))
        request = replace(request, messages=(*request.messages[:-1], replace(request.messages[-1], parts=(*request.messages[-1].parts[:1], TextPartIR("changed after compact " + str(round_))))))
    coordinator.end_turn("one")
    next_user = replace(user, message_id="next")
    request = replace(request, messages=(*[replace(m, prompt_region=PromptRegionIR.SETTLED_HISTORY) if m.role == MessageRole.USER else m for m in request.messages], next_user), metadata={**request.metadata, "turn_id": "two"})
    plan = coordinator.plan(request, context)
    assert plan.breakpoints[1].message_id == "next"
    from pal.llm.cache_handoff import at
    assert at(coordinator.inject(codec.encode(request, context), plan).payload, plan.breakpoints[1].path)["text"] == "another input block"
    assert coordinator.snapshot()["handoff"]["base_source"] == "cold"


@pytest.mark.parametrize("enabled", [False, True])
def test_handoff_attempt_log_is_opt_in_and_has_no_prompt_text(caplog, enabled):
    import json
    from tests.test_prompt_cache_policy import _request, _openai_context
    request = replace(_request(), metadata={"prompt_log_enabled": enabled})
    context, coordinator = _openai_context(), PromptCacheCoordinator()
    raw = codec_for_shape(context.wire_shape).encode(request, context)
    caplog.set_level("INFO", logger="pal.llm.prompt_cache")
    plan, encoded, _ = coordinator.prepare_attempt(request, context, raw, "logged")
    coordinator.record_success(plan, usage(10000, 1000), request_id="logged")
    rows = [json.loads(r.message.split("prompt_cache_handoff ", 1)[1]) for r in caplog.records if "prompt_cache_handoff " in r.message]
    assert bool(rows) == enabled
    if enabled:
        assert len(rows) == 2
        final = rows[-1]
        assert final["cache_write_input_tokens"] is None
        assert final["applied_marker_paths"]
        assert final["wire_audit_fingerprint"]
        assert final["cache_handoff"]["base_source"] == "S"
        assert "stable stable" not in json.dumps(rows)


def test_s_only_provider_never_confirms_u_or_f_but_trials_continue():
    from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
    context = ShapeContext(WireShape.OPENAI_RESPONSE, "endpoint", "gpt-6-astra",
        provider_id="openai", capabilities={"prompt_cache": {"minimum_tokens": 1, "net_threshold_tokens": 0}})
    coordinator = PromptCacheCoordinator()
    messages = [
        LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("abc " * 1000),), message_id="s", prompt_region=PromptRegionIR.STABLE_SYSTEM),
        LLMMessageIR(MessageRole.USER, (TextPartIR("abc " * 1000),), message_id="u", prompt_region=PromptRegionIR.ACTIVE_INPUT),
    ]
    trials = set()
    decisions = set()
    for round_ in range(10):
        request = LLMRequestIR(tuple(messages), tools=(), policy=GenerationPolicyIR(max_output_tokens=128), logical_scope_id="s-only", metadata={"turn_id": "one", "llm_round_index": round_})
        raw = codec_for_shape(context.wire_shape).encode(request, context)
        plan, encoded, _ = coordinator.prepare_attempt(request, context, raw, str(round_))
        assert [p.message_id for p in plan.breakpoints[:2]] == ["s", "u"]
        assert audit(encoded, tuple(p.path for p in plan.breakpoints))[0]
        if plan.handoff_candidate:
            trials.add(plan.handoff_candidate)
        # Independent provider counts words, stores/reads S only, ignores later markers.
        total = (round_ + 2) * 1000
        coordinator.record_success(plan, usage(total, 1000 if round_ else 0), request_id=str(round_))
        state_ = coordinator.snapshot()["handoff"]
        decisions.add(state_["decision"])
        assert not state_["anchor_read"] and state_["promotions"] == 0
        assert state_["base_source"] == ("cold" if round_ == 0 else "S")
        messages.extend([
            LLMMessageIR(MessageRole.ASSISTANT, (ToolCallIR(str(round_), "probe", {}),), message_id=f"call{round_}", prompt_region=PromptRegionIR.ACTIVE_HISTORY),
            LLMMessageIR(MessageRole.TOOL, (ToolResultIR(str(round_), "probe", "abc " * 1000),), message_id=f"result{round_}", prompt_region=PromptRegionIR.ACTIVE_HISTORY),
        ])
    assert len(trials) >= 2
    assert "candidate_budget_exhausted" in decisions


def test_compiler_anchors_last_real_user_interjection_not_runtime_user_context():
    from pal.core import PalCore, register_with_core
    from pal.memory import MemoryService, register_with_core as register_memory
    from tests.test_compact_continuity import request as build_request
    core, memory = PalCore(), MemoryService()
    register_with_core(core)
    register_memory(core.context, memory)
    memory.begin_l1_turn("one", user_text="original request")
    memory.append_l1_user("one", LLMMessageIR(MessageRole.USER, (TextPartIR("new requirement"),), message_id="interjection", semantic_kind="user_interjection"))
    memory.append_l1_user("one", LLMMessageIR(MessageRole.USER, (TextPartIR("runtime information"),), message_id="context", semantic_kind="pal_prompt_context"))
    request = build_request(core, "one")
    active = [m.message_id for m in request.messages if m.prompt_region == PromptRegionIR.ACTIVE_INPUT]
    assert active[-1] == "interjection" and "context" not in active
    plan = PromptCacheCoordinator().plan(request, ShapeContext(WireShape.OPENAI_RESPONSE, "openai", "gpt-6-astra", provider_id="openai"))
    assert next(b for b in plan.breakpoints if b.label == "anchor_fixed").message_id == "interjection"
