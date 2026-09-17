from dataclasses import replace
import pytest
from pal.llm.cache_wire import audit, clean_request
from pal.llm.ir import GenerationPolicyIR, LLMUsageIR, LLMMessageIR, LLMRequestIR, MessageRole, PromptRegionIR, TextPartIR, WireShape
from pal.llm.prompt_cache import PromptCacheCoordinator
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.shared.json_values import thaw_json
from tests.test_prompt_cache_policy import _usage as usage

def test_exact_audit_and_content_identity_survive_old_marker_removal():
    from pal.llm.cache_wire import inject_exact
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

def test_exact_injection_does_not_fallback_when_last_path_is_invalid():
    from pal.llm.cache_wire import inject_exact
    from pal.llm.prompt_cache import PromptCacheBreakpoint
    from pal.llm.shapes.base import EncodedRequest, EncodedMessageSpan
    raw = EncodedRequest({"input": [{"content": [{"type": "input_text", "text": "valid"}]}]},
        (EncodedMessageSpan("u", (("input", 0, "content", 0), ("input", 0, "content", 9))),))
    wire = inject_exact(raw, (PromptCacheBreakpoint("c", "u"),), "key", False)
    assert wire.applied_cache_breakpoint_message_ids == ()
    assert not audit(wire, (("input", 0, "content", 9),))[0]

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

def test_turn_notification_supports_invokers_without_cache_policy():
    from pal.llm.runtime import LLMRuntime
    runtime = object.__new__(LLMRuntime)
    runtime.endpoint_invoker = object()
    runtime.end_prompt_cache_turn("closed")

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

@pytest.mark.parametrize("shape", [WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION])
@pytest.mark.parametrize("provider", ["openai", "openrouter"])
def test_compact_anchor_is_exact_interior_block_then_advances_next_turn(shape, provider):
    from pal.memory.continuity import Continuity
    from pal.llm.cache_wire import boundary_at
    context = ShapeContext(shape, "endpoint", "openai/gpt-6-astra" if provider == "openrouter" else "gpt-6-astra", provider_id=provider, capabilities={"prompt_cache": {"mode": "explicit"}})
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
    from pal.llm.cache_wire import at
    assert at(coordinator.inject(codec.encode(request, context), plan).payload, plan.breakpoints[1].path)["text"] == "another input block"
    assert coordinator.snapshot()["tail"]["tails"] == []

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
    plan = PromptCacheCoordinator().plan(request, ShapeContext(WireShape.OPENAI_RESPONSE, "openai", "gpt-6-astra", provider_id="openai", capabilities={"prompt_cache": {"mode": "explicit"}}))
    assert next(b for b in plan.breakpoints if b.label == "anchor_fixed").message_id == "interjection"
