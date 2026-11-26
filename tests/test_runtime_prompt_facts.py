from types import SimpleNamespace

import pytest

from pal.checklist.capabilities import ChecklistIntrospectionProvider
from pal.checklist.prompt import ChecklistPromptFragmentProvider
from pal.checklist.service import ChecklistService
from pal.core.turns import TurnContinuation
from pal.execution.contracts import CapabilityCall
from pal.identity.prompt import IdentityPromptFragmentProvider
from pal.llm.cache_reminder import prefix_refresh_likely_cold
from pal.llm.contracts import LLMMetadata
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR, WireShape
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.memory.capabilities import register_with_core as register_memory
from pal.memory.context_view import projected_messages
from pal.memory.contracts import L2Entry
from pal.memory.prompt_context import prepare_context, prompt_date_pin, select_prompt_date_pin
from pal.memory.runtime_state import MemoryRuntimeStatePort
from pal.memory.service import MemoryService
from pal.memory.turn_ir import L1TurnIR
from pal.shared import PromptAssemblyContext
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
from tests.core_factory import build_core
from tests.llm_fakes import scripted_llm


def append(turn, **options):
    frozen, added, state = prepare_context(turn, [], [], **options)
    return turn.append_prompt_contexts(added, state), added


def commit_tool(turn, name, args, result):
    call = ToolCallIR(name, name, args)
    turn = turn.upsert_assistant(LLMMessageIR(MessageRole.ASSISTANT, (call,)))
    return turn.append_tool_result(ToolResultIR(name, name, result.llm_text,
                                               structured=result.structured))


def test_checklist_real_tool_results_and_missing_plan_recovery():
    service = ChecklistService()
    provider = ChecklistIntrospectionProvider(service)
    plan = [{"step": "first"}, {"step": "second"}]
    turn = L1TurnIR.begin("old", user_text="work")
    turn, _ = append(turn, checklist={"active": False, "remaining": []})
    turn = commit_tool(turn, "upsert_checklist", {"plan": plan},
                       provider.upsert(CapabilityCall("upsert_checklist", {"plan": plan})))
    state = {"active": True, "remaining": ["first", "second"]}
    turn, added = append(turn, checklist=state)
    assert not added
    turn = commit_tool(turn, "complete_checklist_step", {"step": "first"},
                       provider.check(CapabilityCall("complete_checklist_step", {"step": "first"})))
    state = {"active": True, "remaining": ["second"]}
    turn, added = append(turn, checklist=state)
    assert not added
    settled = turn.settle()
    new = L1TurnIR.begin("new", user_text="resume")
    new, added = append(new, checklist=state, settled_history=projected_messages(settled, settled=True))
    assert len(added) == 1 and "second" not in added[0].text
    for _ in range(50):
        new, added = append(new, checklist=state, settled_history=projected_messages(settled, settled=True))
        assert not added
    new, added = append(new, checklist=state, boundary="compacted")
    assert len(added) == 1 and "- second" in added[0].text and "first" not in added[0].text
    new, added = append(new, checklist=state, boundary="compacted")
    assert not added
    new, added = append(new, checklist={"active": False, "remaining": []}, boundary="compacted")
    assert len(added) == 1 and "closed" in added[0].text
    new, added = append(new, checklist={"active": False, "remaining": []}, boundary="compacted")
    assert not added


def test_date_pin_survives_turns_and_refreshes_only_on_allowed_boundary():
    memory = MemoryService()
    turn = memory.begin_l1_turn("a", user_text="work")
    pin = select_prompt_date_pin(memory, turn, "2026-11-26")
    _, turn = memory.prepare_l1_prompt_context("a", [], [], boundary="a", date_pin=pin, today="2026-11-26")
    assert prompt_date_pin(memory) == pin
    assert select_prompt_date_pin(memory, turn, "2026-11-27", cold=True) == pin
    _, turn = memory.prepare_l1_prompt_context("a", [], [], boundary="a", date_pin=pin, today="2026-11-27")
    fact = turn.messages[-1]
    assert fact.text.endswith("2026-11-27")
    memory.settle_l1_turn("a")
    turn = memory.begin_l1_turn("b", user_text="next")
    assert select_prompt_date_pin(memory, turn, "2026-11-27") == pin
    _, turn = memory.prepare_l1_prompt_context("b", [], [], boundary="b", date_pin=pin,
        today="2026-11-27", settled_turns=memory.history.turns[:1])
    assert len(turn.messages) == 1
    assert fact in projected_messages(memory.history.turns[0], settled=True)
    memory.settle_l1_turn("b")
    saved = MemoryRuntimeStatePort(memory).snapshot_state()
    memory = MemoryService()
    restore = MemoryRuntimeStatePort(memory)
    restore.install_prepared_state(restore.prepare_restore_state(saved))
    assert prompt_date_pin(memory) == pin
    turn = memory.begin_l1_turn("c", user_text="cold")
    refreshed = select_prompt_date_pin(memory, turn, "2026-11-27", cold=True)
    assert refreshed["date"] == "2026-11-27" and refreshed["epoch"] == pin["epoch"]


def test_compact_commit_retires_date_pin_but_cancel_keeps_it():
    memory = MemoryService()
    turn = memory.begin_l1_turn("old", user_text="long task")
    pin = select_prompt_date_pin(memory, turn, "2026-11-26")
    memory.prepare_l1_prompt_context("old", [], [], boundary="old", date_pin=pin, today="2026-11-26")
    memory.upsert_l1_assistant("old", LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR("done"),)))
    memory.settle_l1_turn("old")
    memory.history_root.promote()
    memory.begin_left_compaction("cancel", reason="manual")
    memory.history_root.cancel("cancel", reason="cancelled")
    assert prompt_date_pin(memory) == pin
    memory.begin_left_compaction("commit", reason="manual")
    assert prompt_date_pin(memory) == pin
    memory.compact_left("commit", L2Entry(entry_id="seed", kind="summary", scope="session",
                                          title="Summary", summary="done", rendered="done"))
    assert prompt_date_pin(memory) is None
    new = memory.begin_l1_turn("new", user_text="resume")
    new_pin = select_prompt_date_pin(memory, new, "2026-11-27")
    assert new_pin["date"] == "2026-11-27"
    assert new_pin["epoch"] != pin["epoch"]


@pytest.mark.parametrize("provider,model,source,expected", [
    ("openai", "gpt-6.2-sol", "request_cache_ttl", True),
    ("anthropic", "claude", "provider_policy", True),
    ("deepseek", "deepseek-v4", "provider_policy", False),
    ("openrouter", "deepseek/deepseek-v4", "provider_policy", False),
    ("google", "gemini", "fallback_policy", False),
])
def test_expiry_is_a_matching_local_estimate(provider, model, source, expected):
    snapshot = {"endpoint_id": "ep", "model_id": model, "provider_id": provider,
                "ttl_source": source, "reason": "estimated_lifetime_elapsed", "anchor_remaining_ttl_seconds": 0}
    assert prefix_refresh_likely_cold(snapshot, endpoint_id="ep", model_id=model) is expected
    assert not prefix_refresh_likely_cold(snapshot, endpoint_id="other", model_id=model)
    assert not prefix_refresh_likely_cold({}, endpoint_id="ep", model_id=model)


def test_expired_route_refreshes_the_next_turn_not_a_busy_turn(monkeypatch):
    core = build_core()
    memory = MemoryService()
    register_memory(core.context, memory)
    core.context.prompt_fragment_registry.register(IdentityPromptFragmentProvider(
        SimpleNamespace(get_persona=lambda: None, get_preferences=lambda: None)))
    facts = LLMMetadata(endpoint_id="sol", model_id="gpt-6.2-sol", supports_suffix_instructions=True)
    snapshot = {"endpoint_id": "sol", "model_id": "gpt-6.2-sol", "provider_id": "openai",
                "ttl_source": "request_cache_ttl", "reason": "estimated_lifetime_elapsed",
                "anchor_remaining_ttl_seconds": 0}
    core.context.port_registry["llm:llm"] = scripted_llm(
        metadata=lambda **_: facts, prompt_cache_reminder_snapshot=lambda: snapshot)
    today = ["2026-11-26"]
    monkeypatch.setattr("pal.core.turn_executor.today_for_timezone", lambda _: today[0])
    def build(turn_id):
        return core.turn_executor.build_turn_prompt(TurnContinuation(turn_id, iter(()), "request"),
            PromptAssemblyContext(), max_output_tokens=128)
    memory.begin_l1_turn("old", user_text="work")
    first = build("old")
    today[0] = "2026-11-27"
    busy = build("old")
    assert busy.messages[0].text == first.messages[0].text
    assert "UTC date update: 2026-11-27" in busy.messages[-1].text
    memory.settle_l1_turn("old")
    memory.history_root.promote()
    source_pin = prompt_date_pin(memory)
    branch = memory.borrow_context()
    branch_turn = branch.begin_l1_turn("side", user_text="side question")
    assert select_prompt_date_pin(branch, branch_turn, today[0], cold=True,
                                  borrowed_date=source_pin["date"])["date"] == "2026-11-26"
    assert prompt_date_pin(memory) == source_pin
    memory.begin_l1_turn("fresh", user_text="next")
    refreshed = build("fresh")
    assert "UTC context date: 2026-11-27" in refreshed.messages[0].text
    assert prompt_date_pin(memory)["date"] == "2026-11-27"


@pytest.mark.parametrize("shape", [WireShape.OPENAI_COMPLETION, WireShape.ANTHROPIC_MESSAGES, WireShape.GEMINI_INTERACTIONS])
def test_compatible_route_temporary_control_does_not_enter_l1(shape):
    core = build_core()
    memory = MemoryService()
    register_memory(core.context, memory)
    facts = LLMMetadata(endpoint_id="node", model_id="compatible",
                        wire_shape=shape.value, supports_request_instructions=True)
    core.context.port_registry["llm:llm"] = scripted_llm(metadata=lambda **_: facts)
    memory.begin_l1_turn("turn", user_text="task")
    cont = TurnContinuation("turn", iter(()), "request")
    cont.finalization_only = True
    cont.finalization_reason = "TEMPORARY CONTROL"
    prompt = core.turn_executor.build_turn_prompt(cont, PromptAssemblyContext(), max_output_tokens=128)
    assert "TEMPORARY CONTROL" in prompt.request_instructions
    assert all("TEMPORARY CONTROL" not in m.text for m in memory.active_l1_turn("turn").messages)
    assert not any(k.startswith("runtime:") for k in memory.active_l1_turn("turn").metadata["prompt_context_state"]["sources"])
    ctx = ShapeContext(shape, "node", "compatible", "proxy", "https://example.test")
    assert "TEMPORARY CONTROL" in str(codec_for_shape(shape).encode(prompt, ctx).payload)
    cont.finalization_only = False
    successor = core.turn_executor.build_turn_prompt(cont, PromptAssemblyContext(), max_output_tokens=128)
    assert not successor.request_instructions
    assert "TEMPORARY CONTROL" not in str(codec_for_shape(shape).encode(successor, ctx).payload)


@pytest.mark.parametrize("shape", ["openai", "anthropic", "gemini", "legacy"])
def test_compiler_resident_50_rounds_and_midnight(monkeypatch, shape):
    core = build_core()
    memory = MemoryService()
    register_memory(core.context, memory)
    identity = IdentityPromptFragmentProvider(SimpleNamespace(get_persona=lambda: None, get_preferences=lambda: None))
    core.context.prompt_fragment_registry.register(identity)
    checklist = ChecklistService()
    core.context.prompt_fragment_registry.register(ChecklistPromptFragmentProvider(checklist))
    from tests.test_scoped_instructions import claude, gemini
    ctx = {"openai": ShapeContext(WireShape.OPENAI_RESPONSE, "sol", "gpt-6.2-sol", "openai", "https://api.openai.com/v1"),
           "anthropic": claude(), "gemini": gemini(),
           "legacy": ShapeContext(WireShape.OPENAI_RESPONSE, "old", "gpt-6-sol", "openai", "https://api.openai.com/v1")}[shape]
    facts = LLMMetadata(endpoint_id=ctx.endpoint_id, model_id=ctx.model_id,
        supports_suffix_instructions=shape == "openai", supports_turn_scoped_instructions=shape == "anthropic",
        supports_interaction_instructions=shape == "gemini")
    core.context.port_registry["llm:llm"] = scripted_llm(metadata=lambda **_: facts)
    today = ["2026-11-26"]
    monkeypatch.setattr("pal.core.turn_executor.today_for_timezone", lambda _: today[0])
    cont = TurnContinuation("turn", iter(()), "request")
    memory.begin_l1_turn("turn", user_text="hi")
    codec = codec_for_shape(ctx.wire_shape)
    def build():
        request = core.turn_executor.build_turn_prompt(cont, PromptAssemblyContext(), max_output_tokens=128)
        assert request.request_instructions == ""
        payload = codec.encode(request, ctx).payload
        assert "instructions" not in payload
        assert "clear_at" not in str(payload)
        return request.messages, payload
    first = build()
    assert "UTC context date: 2026-11-26" in str(first[1])
    for _ in range(50):
        assert build()[1] == first[1]
    today[0] = "2026-11-27"
    second = build()
    assert [(m.role, m.text) for m in second[0][:len(first[0])]] == [(m.role, m.text) for m in first[0]]
    assert "UTC date update: 2026-11-27" in second[0][-1].text
    assert build()[1] == second[1]
    checklist.upsert([{"step": "unfinished task"}])
    third = build()
    assert [(m.role, m.text) for m in third[0][:len(second[0])]] == [(m.role, m.text) for m in second[0]]
    assert "unfinished task" in third[0][-1].text
    assert build()[1] == third[1]
