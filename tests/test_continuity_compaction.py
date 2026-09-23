"""Both hosts preserve accepted checkpoint content and replay the same prefix."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pal.bunshin.compact import BunshinCompactionPolicy
from pal.bunshin.runner import _BunshinLLMRuntimeAdapter
from pal.core.compaction import CompactionClockKind, CompactionEngine, CompactionSnapshot
from pal.core.continuity_compaction import COMPACTION_SCHEMA_CONTINUITY_V1, CONTINUITY_FIELDS
from pal.core.pal_compaction import PalCompactionPolicy
from pal.llm.ir import GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole, TextPartIR
from pal.llm.prompt_cache import PromptCacheCoordinator
from pal.llm.shapes.openai_response import OpenAIResponseCodec
from pal.memory.contracts import L1MessageKind, L1TranscriptMessage
from pal.llm.ir import ThinkingLevel
from tests.test_prompt_cache_evidence import _astra_context, _request
from tests.test_runtime_compaction import _ScriptedLLM, _memory_with_turns, _valid_pal_payload


def payload(kind="pal"):
    return {"schema": COMPACTION_SCHEMA_CONTINUITY_V1, "kind": kind,
            "summary": {"summary": "Continue the current collaboration."},
            "continuity": {key: [] for key in CONTINUITY_FIELDS}}


@pytest.mark.parametrize("policy", [PalCompactionPolicy(), BunshinCompactionPolicy()])
def test_all_accepted_items_and_exact_evidence_reach_context(policy):
    value = payload(policy.kind)
    evidence = "  /workspace/a.py:42\n  exit=7; outcome unknown  "
    value["continuity"]["state"] = [f"unique-marker-{i:03d}" for i in range(24)] + [evidence]
    entry = policy.validate_checkpoint(json.dumps(value), None)
    assert all(item in entry.rendered for item in value["continuity"]["state"])
    assert entry.search_text == entry.summary
    assert "search_text" not in entry.payload["summary"]
    assert "open_items" not in entry.payload["continuity"]
    assert "current checklist" in entry.rendered


@pytest.mark.parametrize("policy", [PalCompactionPolicy(), BunshinCompactionPolicy()])
@pytest.mark.parametrize("bad", ["unknown", "missing", "nested", "empty_string", "summary_extra"])
def test_invalid_body_is_rejected_instead_of_silently_dropped(policy, bad):
    value = payload(policy.kind)
    if bad == "unknown": value["continuity"]["open_items"] = ["critical task"]
    elif bad == "missing": del value["continuity"]["state"]
    elif bad == "nested": value["continuity"]["state"] = [{"text": "critical fact"}]
    elif bad == "empty_string": value["continuity"]["state"] = [" "]
    else: value["summary"]["search_text"] = "old schema"
    with pytest.raises(ValueError):
        policy.validate_checkpoint(json.dumps(value), None)


def test_bunshin_rejects_memory_candidates_even_when_empty():
    value = payload("bunshin")
    value["memory_candidates"] = []
    with pytest.raises(ValueError):
        BunshinCompactionPolicy().validate_checkpoint(json.dumps(value), None)


@pytest.mark.parametrize("kind,legacy_schema", [("pal", "pal.compaction.pal.v2"), ("bunshin", "pal.compaction.bunshin.v3")])
def test_previous_schema_is_source_data_not_revalidated_or_rewritten(kind, legacy_schema):
    seed = {"schema": legacy_schema, "kind": kind, "summary": {"summary": "KEEP OLD SEED", "search_text": "old identifiers"},
            "continuity": {"legacy_field": ["old evidence"]}}
    transcript = L1TranscriptMessage(role="assistant", content="OLD RENDERED BODY", kind=L1MessageKind.RUNTIME_CONTEXT_SUMMARY, payload=seed)
    snapshot = CompactionSnapshot(target_input_budget=8192, reserved_output_tokens=1024,
                                  clock_kind=CompactionClockKind.USER_TURN, clock_value=1, memory_items=((transcript,),))
    policy = PalCompactionPolicy() if kind == "pal" else BunshinCompactionPolicy()
    source = policy.build_source(snapshot, [])
    assert legacy_schema in source and "old evidence" in source
    assert snapshot.previous_summary.rendered == "OLD RENDERED BODY"
    assert transcript.payload == seed
    with pytest.raises(ValueError):
        policy.validate_checkpoint(json.dumps(seed), snapshot)


@pytest.mark.parametrize("shape", ["openai_response", "openai_completion", "anthropic_messages"])
@pytest.mark.parametrize("policy", [PalCompactionPolicy(), BunshinCompactionPolicy()])
def test_replay_keeps_prefix_tools_and_resolved_generation_settings(shape, policy):
    from pal.llm.ir import WireShape
    from pal.shared.tool_protocol import ToolDefinitionIR
    from pal.llm.shapes import codec_for_shape
    from pal.llm.shapes.base import ShapeContext
    budget = 3000 if shape == "anthropic_messages" else None
    original = LLMRequestIR(
        messages=(LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("original instructions"),)),
                  LLMMessageIR(MessageRole.USER, (TextPartIR("original input"),))),
        tools=(ToolDefinitionIR(name="read_file", description="original description", input_schema={"type": "object", "properties": {}}),),
        policy=GenerationPolicyIR(max_output_tokens=16000, temperature=0.7, thinking_level=ThinkingLevel.HIGH,
                                  thinking_budget_tokens=budget,
                                  reasoning_context="all_turns" if shape == "openai_response" else None),
        logical_scope_id="bunshin:isolated" if policy.kind == "bunshin" else "pal:resident",
        metadata={"retained_marker": "retain-me"},
    )
    snapshot = CompactionSnapshot(target_input_budget=8192, reserved_output_tokens=1024,
                                  clock_kind=policy.clock_kind, clock_value=1, replay_request=original, replay_wire_shape=shape)
    request = CompactionEngine(policy)._request(snapshot, "", attempt=1)
    assert request.messages[:-1] == original.messages
    assert request.messages[-1].role == MessageRole.USER
    assert request.tools == original.tools
    assert request.policy.thinking_level == ThinkingLevel.HIGH
    assert request.policy.thinking_budget_tokens == budget
    assert request.policy.reasoning_context == original.policy.reasoning_context
    assert request.policy.thinking_selection == "configured"
    assert request.policy.temperature == 0.7
    assert request.policy.tool_choice == ("none" if shape.startswith("openai_") else "auto")
    assert request.logical_scope_id == original.logical_scope_id
    assert request.metadata["model_hooks_already_applied"] is True
    assert request.metadata["retained_marker"] == "retain-me"
    context = ShapeContext(wire_shape=WireShape(shape), endpoint_id="fixture", model_id="fixture-model")
    codec = codec_for_shape(context.wire_shape)
    before = codec.encode(original, context).payload
    after = codec.encode(request, context).payload
    history_key = "input" if shape == "openai_response" else "messages"
    old_history = before[history_key]
    new_history = after[history_key]
    if shape == "openai_response":
        assert new_history[:len(old_history)] == old_history
    else:
        # These codecs merge adjacent user messages, appending blocks while
        # retaining the original content prefix inside the last container.
        assert new_history[:len(old_history) - 1] == old_history[:-1]
        old_tail = old_history[-1]
        new_tail = new_history[len(old_history) - 1]
        assert {k: v for k, v in new_tail.items() if k != "content"} == {
            k: v for k, v in old_tail.items() if k != "content"
        }
        assert new_tail["content"][:len(old_tail["content"])] == old_tail["content"]
    assert after["tools"] == before["tools"]
    for key in ("system", "reasoning", "reasoning_effort", "thinking", "output_config", "temperature"):
        assert after.get(key) == before.get(key)


def test_bunshin_anchor_retention_and_adapter_are_scope_isolated_and_bounded():
    coordinator = PromptCacheCoordinator(max_scope_count=2)
    context = _astra_context()
    requests = {}
    for scope in ("bunshin:a", "bunshin:b"):
        request = replace(_request(), logical_scope_id=scope)
        requests[scope] = request
        coordinator.plan(request, context, OpenAIResponseCodec().encode(request, context))
    base = SimpleNamespace(prompt_cache_eligible_anchor_request=coordinator.eligible_anchor_request,
                           prompt_cache_confirmed_anchor_request=coordinator.confirmed_anchor_request)
    adapter = _BunshinLLMRuntimeAdapter(None, base, None)
    for scope, request in requests.items():
        anchor = adapter.prompt_cache_eligible_anchor_request(logical_scope_id=scope)
        assert anchor["request"] == replace(request, messages=request.messages[:-1])
        assert anchor["anchor_message_id"] == request.messages[-2].message_id
        assert anchor["read_confirmed"] is False
    assert adapter.prompt_cache_eligible_anchor_request(logical_scope_id="pal:resident") == {}
    request = replace(_request(), logical_scope_id="bunshin:c")
    coordinator.plan(request, context, OpenAIResponseCodec().encode(request, context))
    assert len(coordinator._anchor_evidence) <= 2


def test_eager_plan_material_reaches_real_compact_without_cache_ack():
    from pal.core.runtime import PalCore
    from pal.llm import generation_result_from_values
    from pal.llm.ir import PromptRegionIR
    from pal.memory import MemoryService

    core = PalCore()
    service = MemoryService()
    service.begin_l1_turn("warm", user_text="original user input " * 900)
    user = service.active_l1_turn("warm").messages[0]
    service.upsert_l1_assistant("warm", LLMMessageIR(
        role=MessageRole.ASSISTANT, parts=(TextPartIR("accepted final result"),)))
    service.settle_l1_turn("warm")
    original = LLMRequestIR(
        messages=(
            LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("stable " * 900),),
                         prompt_region=PromptRegionIR.STABLE_SYSTEM),
            replace(user, prompt_region=PromptRegionIR.ACTIVE_INPUT),
        ),
        tools=(), policy=GenerationPolicyIR(max_output_tokens=1024),
        logical_scope_id="pal:resident",
    )
    coordinator = PromptCacheCoordinator()
    context = _astra_context()
    coordinator.plan(original, context, OpenAIResponseCodec().encode(original, context))
    llm = _ScriptedLLM([generation_result_from_values(text=_valid_pal_payload())])
    llm.prompt_cache_eligible_anchor_request = coordinator.eligible_anchor_request
    core.context.port_registry["llm:llm"] = llm
    result = asyncio.run(core.turn_executor.compact_memory_async(
        service, target_input_budget=16384, reserved_output_tokens=1024))
    assert result.success, result.failures
    sent = llm.generate_requests[0]
    assert sent.messages[:2] == original.messages
    assert sent.messages[2].text == "accepted final result"
    assert sent.messages[-1].role == MessageRole.USER
    assert sent.policy.tool_choice == "none"


@pytest.mark.parametrize("hot", [False, True])
def test_incompatible_manual_thinking_budget_falls_back_or_stops(hot):
    from pal.llm import generation_result_from_values
    from tests.test_runtime_compaction import _attach_hot_cache
    from pal.core.runtime import PalCore
    core = PalCore()
    service = _memory_with_turns(2)
    llm = _ScriptedLLM([generation_result_from_values(text=_valid_pal_payload())])
    _attach_hot_cache(llm, service)
    old_reader = llm.prompt_cache_confirmed_anchor_request
    anchor = old_reader()
    anchor["request"] = replace(anchor["request"], policy=GenerationPolicyIR(max_output_tokens=16000,
        thinking_level=ThinkingLevel.HIGH, thinking_budget_tokens=8000))
    llm.prompt_cache_eligible_anchor_request = lambda **kwargs: anchor
    core.context.port_registry["llm:llm"] = llm
    before = list(service.l1_store.items)
    result = asyncio.run(core.turn_executor.compact_memory_async(service, target_input_budget=8192,
        reserved_output_tokens=2048, cache_epoch="epoch-a" if hot else ""))
    if hot:
        assert result.status == "hot_cache_unavailable"
        assert not llm.generate_requests
        assert list(service.l1_store.items) == before
    else:
        assert result.success
        assert llm.generate_requests[0].policy.thinking_selection == "lowest_supported"
