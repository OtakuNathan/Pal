from types import SimpleNamespace

import pytest

from pal.core import PalCore, TurnContinuation, register_with_core
from pal.foundation import EventEnvelope
from pal.llm.ir import ImagePartIR, LLMMessageIR, MessageRole, TextPartIR, WireShape
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.memory import MemoryService, MemoryCompactRequest, MemoryPackRequest, L2Entry
from pal.memory import register_with_core as register_memory
from pal.memory.continuity import SUMMARY_CONTEXT_KEY
from pal.memory.runtime_state import MemoryRuntimeStatePort
from pal.memory.turn_ir import L1TurnIR
from pal.shared import PromptAssemblyContext
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR


SEED = "UNIQUE_COMPACT_SEED"


def compact(service, text=SEED):
    return service.compact(MemoryCompactRequest(
        target_input_budget=2048, reserved_output_tokens=128,
        summary_entry=L2Entry(entry_id="summary", kind="summary", scope="system",
                              title="summary", summary=text, rendered=text)))


def setup():
    core, service = PalCore(), MemoryService()
    register_with_core(core)
    register_memory(core.context, service)
    compact(service)
    return core, service


def request(core, turn_id, **kwargs):
    continuation = TurnContinuation(turn_id=turn_id, program=iter(()), correlation_id=turn_id, **kwargs)
    return core.turn_executor.build_turn_prompt(
        continuation, PromptAssemblyContext(event=EventEnvelope(
            event_kind="user.message", source_kind="channel", payload={"text": "continue"})),
        max_output_tokens=128)


def assert_one_seed(prompt):
    assert sum(m.text.count(SEED) for m in prompt.messages) == 1
    assert not any(m.semantic_kind == "runtime_context_summary" for m in prompt.messages)
    return next(m for m in prompt.messages if SEED in m.text)


def test_fifty_rounds_and_multiple_turns_keep_one_frozen_prefix(monkeypatch):
    core, service = setup()
    first = service.begin_l1_turn("one", user_text="first request")
    initial = request(core, "one")
    anchored = assert_one_seed(initial)
    assert anchored.message_id == first.messages[0].message_id
    assert anchored.text.index(SEED) < anchored.text.index("first request")
    snapshot = service.l1_store.turns.continuity
    revision = service.active_l1_turn("one").revision

    def forbidden(*args, **kwargs):
        raise AssertionError("unchanged typed request rescanned or converted history")

    monkeypatch.setattr("pal.memory.service._transcript_from_turn", forbidden)
    monkeypatch.setattr(service.l1_store.turns, "_read_continuity", forbidden)
    for _ in range(50):
        current = request(core, "one")
        assert current.messages == initial.messages
        assert service.active_l1_turn("one").revision == revision
    for previous, tid in (("one", "two"), ("two", "three")):
        service.settle_l1_turn(previous)
        service.begin_l1_turn(tid, user_text=f"request {tid}")
        current = request(core, tid)
        seed = assert_one_seed(current)
        assert seed.message_id == anchored.message_id and seed.parts == anchored.parts
    assert service.l1_store.turns.continuity is snapshot
    assert service.l1_store.turns.get("one").messages[0] == first.messages[0]


@pytest.mark.parametrize("shape", list(WireShape))
def test_codecs_keep_seed_once_with_original_multimodal_user(shape):
    core, service = setup()
    original = LLMMessageIR(role=MessageRole.USER, parts=(
        ImagePartIR(source="https://example.invalid/picture.png"), TextPartIR("inspect image")))
    service.begin_l1_turn("one", user_message=original)
    prompt = request(core, "one")
    merged = assert_one_seed(prompt)
    assert merged.parts[1:] == original.parts
    encoded = codec_for_shape(shape).encode(prompt, ShapeContext(shape, "fixture", "fixture"))
    assert str(encoded.payload).count(SEED) == 1


def test_restore_recompact_and_reset_use_current_source_only():
    core, service = setup()
    service.begin_l1_turn("one", user_text="first")
    first = request(core, "one")
    port = MemoryRuntimeStatePort(service)
    saved = port.snapshot_state()
    port.install_prepared_state(port.prepare_restore_state(saved))
    assert request(core, "one").messages == first.messages
    compact(service)
    second = request(core, "one")
    assert_one_seed(second)
    assert second.metadata["continuity_id"] != first.metadata["continuity_id"]
    assert_one_seed(request(core, "one", preferred_llm_endpoint_id="other", preferred_llm_model_id="other"))
    service.l1_store.turns.clear()
    assert service.build_pack(MemoryPackRequest()).current_summary is None
    assert service.l1_store.turns.continuity is None


def test_compact_failure_restores_reference_and_anchor(monkeypatch):
    core, service = setup()
    service.begin_l1_turn("one", user_text="first")
    before = request(core, "one")
    turns = service.l1_store.turns.turns
    def fail(*args):
        raise RuntimeError("commit failed")
    monkeypatch.setattr(service, "remove_projected_entries", fail)
    with pytest.raises(RuntimeError, match="commit failed"):
        compact(service, "replacement")
    assert service.l1_store.turns.turns == turns
    assert request(core, "one").messages == before.messages


def test_standalone_seed_does_not_move_when_first_input_arrives():
    _, service = setup()
    first = service.project_continuity([])
    port = MemoryRuntimeStatePort(service)
    port.install_prepared_state(port.prepare_restore_state(port.snapshot_state()))
    user = LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR("later input"),))
    later = service.project_continuity([user])
    assert later == [*first, user]
    assert service.project_continuity(later) == later


def test_old_source_tagged_copies_are_excluded_but_user_text_is_kept():
    core, service = setup()
    old = service.begin_l1_turn("old", user_text="first")
    duplicate = LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR(SEED),),
        semantic_kind="pal_prompt_context", metadata={"pal_authored": True,
                                                       "context_key": SUMMARY_CONTEXT_KEY})
    service.l1_store.turns.replace(old.append(duplicate))
    service.settle_l1_turn("old")
    service.begin_l1_turn("new", user_text="continue")
    assert_one_seed(request(core, "new"))
    ordinary = LLMMessageIR(role=MessageRole.ASSISTANT, parts=(TextPartIR(SEED),))
    assert ordinary in service.project_continuity([ordinary])


def test_compaction_does_not_split_pending_tool_protocol():
    _, service = setup()
    service.begin_l1_turn("one", user_text="work")
    assistant = LLMMessageIR(role=MessageRole.ASSISTANT,
        parts=(ToolCallIR(call_id="call", name="read", arguments={}),))
    service.upsert_l1_assistant("one", assistant)
    compact(service)
    service.append_l1_tool_result("one", ToolResultIR(call_id="call", name="read", content="done"))
    turn = service.active_l1_turn("one")
    projected = service.project_continuity(list(turn.messages))
    assert projected[1:] == list(turn.messages[1:])
    assert not L1TurnIR("protocol-check", tuple(projected)).pending_call_ids


def test_streaming_and_tool_rounds_only_append_after_continuity_prefix():
    core, service = setup()
    service.begin_l1_turn("one", user_text="work")
    previous = request(core, "one")
    for index in range(5):
        assistant = LLMMessageIR(role=MessageRole.ASSISTANT,
            parts=(ToolCallIR(call_id=f"call-{index}", name="read", arguments={}),))
        service.stream_l1_assistant("one", assistant)
        service.append_l1_tool_result("one", ToolResultIR(
            call_id=f"call-{index}", name="read", content=f"output-{index}"))
        current = request(core, "one")
        assert_one_seed(current)
        assert current.messages[:len(previous.messages)] == previous.messages
        previous = current


def test_legacy_pack_and_compiler_keep_one_summary_before_first_user():
    core, service = setup()
    service.begin_l1_turn("old", user_text="original request")
    service.settle_l1_turn("old")
    pack = service.build_pack(MemoryPackRequest())
    assert pack.l1_recent_context and pack.current_summary
    prompt = core.build_canonical_prompt(PromptAssemblyContext(metadata={"memory_pack": pack}))
    merged = assert_one_seed(prompt)
    assert merged.role == MessageRole.USER
    assert "original request" in merged.text
    assert not any("memory_current_summary" in c["key"] for c in prompt.metadata["context_candidates"])


def test_replay_from_previous_compaction_boundary_is_rejected():
    core, service = setup()
    service.begin_l1_turn("one", user_text="work")
    prompt = request(core, "one")
    service.settle_l1_turn("one")
    llm = SimpleNamespace(prompt_cache_confirmed_anchor_request=lambda **kwargs: {
        "request": prompt, "anchor_message_id": prompt.messages[-1].message_id})
    service.begin_l1_turn("two", user_text="next request")
    service.settle_l1_turn("two")
    replay = core.turn_executor._resident_compaction_replay_request(service, llm_runtime=llm,
        logical_scope_id="pal:resident", preferred_endpoint_id=None)
    assert_one_seed(replay[0])
    assert replay[0].messages[:len(prompt.messages)] == prompt.messages
    assert replay[0].messages[-1].text == "next request"
    compact(service)
    replay = core.turn_executor._resident_compaction_replay_request(service, llm_runtime=llm,
        logical_scope_id="pal:resident", preferred_endpoint_id=None)
    assert replay == (None, "", "")


def test_proactive_request_does_not_gain_unrequested_history():
    core, service = setup()
    service.begin_l1_turn("one", user_text="scheduled work")
    continuation = TurnContinuation(turn_id="one", program=iter(()), correlation_id="one")
    prompt = core.turn_executor.build_turn_prompt(continuation,
        PromptAssemblyContext(turn_kind="proactive_trigger", metadata={"proactive_input": "scheduled work"}),
        max_output_tokens=128)
    assert SEED not in "\n".join(m.text for m in prompt.messages)
    assert not service.l1_store.turns.continuity.anchor
