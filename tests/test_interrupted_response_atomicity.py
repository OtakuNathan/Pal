"""An interrupted response draft is not an accepted native continuation."""
from dataclasses import replace

import pytest

from pal.llm.ir import LLMMessageIR, MessageRole, MessageState, ReasoningPartIR, ReplayEnvelope, TextPartIR, WireShape
from pal.llm.replay_acceptance import validate_native_for_send
from pal.llm.serde import message_to_payload
from pal.memory import MemoryService
from pal.memory.runtime_state import MemoryRuntimeStatePort
from pal.memory.turn_ir import L1TurnIR
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
from pal.shared.json_values import thaw_json


def draft(*, signature="", state=MessageState.IN_PROGRESS, committed=False):
    return LLMMessageIR(
        MessageRole.ASSISTANT, (ReasoningPartIR("unfinished reasoning"),),
        message_id="draft", state=state,
        semantic_kind="assistant_reply",
        replay=ReplayEnvelope(WireShape.ANTHROPIC_MESSAGES, "ep", "model", {
            "content": [{"type": "thinking", "thinking": "unfinished reasoning", "signature": signature}],
        }),
        metadata={"committed_items": [{"item_id": "reasoning", "item_kind": "reasoning"}] if committed else []},
    )


def accepted_round():
    call = ToolCallIR("read-1", "read", {"path": "a"})
    return LLMMessageIR(
        MessageRole.ASSISTANT, (ReasoningPartIR("accepted reasoning"), call), message_id="accepted",
        replay=ReplayEnvelope(WireShape.ANTHROPIC_MESSAGES, "ep", "model", {"content": [
            {"type": "thinking", "thinking": "accepted reasoning", "signature": "signed-by-provider"},
            {"type": "tool_use", "id": "read-1", "name": "read", "input": {"path": "a"}},
        ]}),
    )


@pytest.mark.parametrize("close", ["interrupt", "abort"])
@pytest.mark.parametrize("committed", [False, True])
def test_close_discards_only_current_response_and_keeps_accepted_round_verbatim(close, committed):
    accepted = accepted_round()
    current = draft(signature="signed-prefix" if committed else "", committed=committed)
    turn = L1TurnIR.begin("turn", user_text="work").append(accepted)
    turn = turn.append_tool_result(ToolResultIR("read-1", "read", "full result", structured={"original": 42}))
    turn = turn.upsert_assistant(current)

    closed = getattr(turn, close)(reason="user interrupt")

    assert all(m.message_id != "draft" for m in closed.messages)
    assert closed.messages[1] == accepted
    assert closed.messages[2] == turn.messages[2]
    assert thaw_json(closed.metadata["discarded_assistant_drafts"][0]) == message_to_payload(current)
    for message in closed.messages:
        validate_native_for_send(message)
    # The draft still exists in the captured pre-interruption snapshot.
    assert turn.messages[-1] == current


def test_plain_text_stream_is_also_an_uncommitted_response():
    current = LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR("partial"),),
                           message_id="draft", state=MessageState.IN_PROGRESS)
    closed = L1TurnIR.begin("turn", user_text="work").append(current).interrupt()
    assert all(m.message_id != "draft" for m in closed.messages)
    assert closed.metadata["discarded_assistant_drafts"][0]["parts"][0]["text"] == "partial"


@pytest.mark.parametrize("legacy_closed", [False, True])
@pytest.mark.parametrize("cut_includes_draft", [False, True])
def test_restore_recovers_draft_and_rebases_only_affected_left_cut(legacy_closed, cut_includes_draft):
    service = MemoryService()
    service.begin_l1_turn("turn", user_text="work")
    service.upsert_l1_assistant("turn", draft())
    snapshot = MemoryRuntimeStatePort(service).snapshot_state()
    # Model the incident: old interrupt changed only lifecycle state, then the
    # next request promoted the poisoned message into the frozen LEFT prefix.
    raw = snapshot["l1_turns"][0]
    if legacy_closed:
        raw["state"] = "interrupted"
        raw["messages"][-1]["state"] = "complete"
    snapshot["history_root"] = {
        "incarnation": "incident", "left_generation": 7, "abandoned_run": None,
        "cut": {"turn_count": 0, "intra_messages": 2 if cut_includes_draft else 1,
                "revision": 8, "cut_id": "old-cut"},
    }
    restored = MemoryService()
    port = MemoryRuntimeStatePort(restored)
    port.install_prepared_state(port.prepare_restore_state(snapshot))
    turn = restored.l1_store.turns.get("turn")
    assert all(m.message_id != "draft" for m in turn.messages)
    assert turn.metadata["discarded_assistant_drafts"][0]["message_id"] == "draft"
    owner = restored.history_root.owner_state()
    assert owner["cut"]["intra_messages"] == 1
    assert owner["left_generation"] == (8 if cut_includes_draft else 7)
    assert (owner["cut"]["cut_id"] != "old-cut") is cut_includes_draft
    for message in turn.messages:
        validate_native_for_send(message)
    # Recovery is idempotent: already-repaired evidence cannot re-enter replay
    # or repeatedly invalidate the restored LEFT generation.
    again = MemoryService()
    again_port = MemoryRuntimeStatePort(again)
    again_port.install_prepared_state(again_port.prepare_restore_state(port.snapshot_state()))
    assert again.history_root.owner_state() == owner
    assert again.l1_store.turns.get("turn") == turn


def test_restore_keeps_complete_signed_response_verbatim():
    service = MemoryService()
    service.begin_l1_turn("turn", user_text="work")
    complete = replace(draft(signature="opaque-signature", state=MessageState.COMPLETE, committed=True),
                       parts=(ReasoningPartIR("unfinished reasoning"), TextPartIR("answer")))
    service.upsert_l1_assistant("turn", complete)
    service.interrupt_l1_turn("turn")
    snapshot = MemoryRuntimeStatePort(service).snapshot_state()
    restored = MemoryService()
    port = MemoryRuntimeStatePort(restored)
    port.install_prepared_state(port.prepare_restore_state(snapshot))
    assert restored.l1_store.turns.get("turn").messages[-1] == complete


def test_restore_does_not_silently_repair_an_invalid_accepted_continuation():
    service = MemoryService()
    service.begin_l1_turn("turn", user_text="work")
    accepted = draft(state=MessageState.COMPLETE, committed=True)
    service.upsert_l1_assistant("turn", accepted)
    service.interrupt_l1_turn("turn")
    restored = MemoryService()
    port = MemoryRuntimeStatePort(restored)
    port.install_prepared_state(port.prepare_restore_state(MemoryRuntimeStatePort(service).snapshot_state()))
    from pal.llm.response_hook_contracts import ProviderResponseHookError
    with pytest.raises(ProviderResponseHookError, match="no signature"):
        validate_native_for_send(restored.l1_store.turns.get("turn").messages[-1])


def test_unaccepted_response_cannot_hide_a_consumed_effect_at_close_or_restore():
    from pal.memory.turn_ir import L1TurnProtocolError
    current = replace(accepted_round(), state=MessageState.IN_PROGRESS)
    turn = L1TurnIR.begin("turn", user_text="work").append(current)
    turn = turn.append_tool_result(ToolResultIR("read-1", "read", "effect already observed"))
    with pytest.raises(L1TurnProtocolError, match="consumed tool calls"):
        turn.interrupt()
    assert turn.messages[1] == current
    service = MemoryService()
    service.l1_store.turns.append(turn)
    with pytest.raises(ValueError, match="consumed tool calls"):
        MemoryRuntimeStatePort(MemoryService()).prepare_restore_state(
            MemoryRuntimeStatePort(service).snapshot_state())


@pytest.mark.parametrize("signed_prefix", [False, True])
def test_real_anthropic_stream_interrupt_then_next_request_is_replayable(signed_prefix):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from pal.core import PalCore
    from pal.llm.ir import GenerationPolicyIR, LLMRequestIR
    from pal.llm.response_blocks import decode_response_blocks
    from pal.llm.shapes import codec_for_shape
    from pal.llm.shapes.anthropic_messages import AnthropicMessagesDecoder
    from pal.llm.shapes.base import ShapeContext, _JSONFrame
    from pal.memory import register_with_core

    async def scenario():
        context = ShapeContext(WireShape.ANTHROPIC_MESSAGES, "ep", "model")
        decoder = AnthropicMessagesDecoder(context)
        frames = [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "unfinished stream"}},
        ]
        if signed_prefix:
            frames += [
                {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "signed-prefix"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "unfinished body"}},
            ]
        core = PalCore()
        service = MemoryService()
        register_with_core(core.context, service)
        service.begin_l1_turn("turn", user_text="work")
        continuation = SimpleNamespace(turn_id="turn", interrupted=False)
        try:
            with patch.object(core.turn_executor, "execute_turn_effect_async", AsyncMock()):
                for index, payload in enumerate(frames):
                    for update in decode_response_blocks(decoder.feed(_JSONFrame(index, payload))):
                        await core.turn_executor._handle_ir_stream_update(continuation, update)
            closed = service.interrupt_l1_turn("turn", reason="user interrupt")
            assert closed.metadata["discarded_assistant_drafts"]
            for message in closed.messages:
                validate_native_for_send(message)
            request = LLMRequestIR(messages=(*closed.messages, LLMMessageIR(MessageRole.USER, (TextPartIR("continue"),))),
                                   tools=(), policy=GenerationPolicyIR(max_output_tokens=100))
            encoded = codec_for_shape(WireShape.ANTHROPIC_MESSAGES).encode(request, context)
            assert "unfinished" not in str(encoded.payload)
        finally:
            core.close()
    asyncio.run(scenario())
