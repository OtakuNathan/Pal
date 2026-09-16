from dataclasses import replace

import pytest

from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.memory.turn_ir import L1TurnStore, L1TurnProtocolError
from pal.shared.tool_protocol import new_tool_call, ToolResultIR


def result_turn(store, turn_id):
    turn = store.begin(turn_id, user_text='task')
    turn = turn.append(LLMMessageIR(MessageRole.ASSISTANT, (new_tool_call('same-call', 'read_file', {}),)))
    turn = turn.append_tool_result(ToolResultIR('same-call', 'read_file', turn_id))
    store.replace(turn)
    return turn


def test_tool_ids_are_scoped_and_old_view_is_immutable():
    store = L1TurnStore()
    first = result_turn(store, 'first')
    store.replace(first.settle())
    second = result_turn(store, 'second')
    view = store.context_view('second', (store.get('first'),))
    assert view.tool_result('first', 'same-call') == first.messages[-1].message_id
    assert view.tool_result('second', 'same-call') == second.messages[-1].message_id
    store.clear()
    assert view.contains('first', first.messages[-1].message_id)
    assert store.get('first') is None


def test_rollback_restores_all_indices_and_failed_bulk_restore_is_atomic():
    store = L1TurnStore()
    old = store.begin('turn', user_text='task')
    call = old.append(LLMMessageIR(MessageRole.ASSISTANT, (new_tool_call('call', 'read', {}),)))
    store.replace(call)
    result = call.append_tool_result(ToolResultIR('call', 'read', 'result'))
    store.replace(result)
    store.restore_turn(call, expected=result)
    assert store.context_view('turn').tool_result('turn', 'call') is None
    assert store.message('turn', result.messages[-1].message_id) is None
    with pytest.raises(L1TurnProtocolError):
        store.replace_all((old, old))
    assert store.get('turn') is call


def test_excluded_and_expired_messages_are_stored_but_not_visible():
    store = L1TurnStore()
    turn = store.begin('old', user_text='task')
    hidden = LLMMessageIR(MessageRole.USER, (TextPartIR('old state'),))
    directive = LLMMessageIR(MessageRole.DEVELOPER, (TextPartIR('finalize only'),),
        semantic_kind='pal_prompt_context', metadata={'pal_authored': True, 'scope_turn_id': 'old'})
    turn = turn.append(hidden).append(directive)
    turn = replace(turn, metadata={'prompt_context_state': {'excluded': [hidden.message_id]}})
    store.replace(turn)
    assert not store.context_view('old').contains('old', hidden.message_id)
    assert store.context_view('old').contains('old', directive.message_id)
    closed = turn.settle()
    store.replace(closed)
    store.begin('new', user_text='continue editing')
    view = store.context_view('new', (closed,))
    assert not view.contains('old', directive.message_id)
    assert store.message('old', directive.message_id) is not None


def test_coverage_requires_visible_proof_and_survives_restore():
    store = L1TurnStore()
    turn = store.begin('turn', user_text='task')
    message = LLMMessageIR(MessageRole.USER, (TextPartIR('current state'),))
    turn = turn.append_user_contexts((message,), coverage_namespace='extension', coverage={
        'states': {'session': {'revision': 7, 'message_id': message.message_id}}})
    store.replace(turn)
    restored = L1TurnStore(store.turns)
    assert restored.context_view('turn').state_proof('extension', 'session', 7)['turn_id'] == 'turn'
    hidden = replace(turn, revision=turn.revision+1, metadata={**dict(turn.metadata),
        'prompt_context_state': {'excluded': [message.message_id]}})
    restored.replace(hidden)
    assert not restored.context_view('turn').state_proof('extension', 'session', 7)


def test_streaming_only_visits_current_round_and_snapshots_are_immutable():
    from unittest.mock import patch
    from pal.memory.turn_ir import _protocol_ids
    store = L1TurnStore()
    turn = store.begin('long', user_text='task')
    for i in range(100):
        turn = turn.append(LLMMessageIR(MessageRole.ASSISTANT, (new_tool_call(f'c{i}', 'read', {}),)))
        turn = turn.append_tool_result(ToolResultIR(f'c{i}', 'read', 'done'))
    store.replace(turn)
    message = LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR('a'),), message_id='stream')
    store.stream_assistant('long', message)
    before = store.get('long')
    visited = []
    def tracked(messages):
        visited.append(len(messages))
        return _protocol_ids(messages)
    with patch('pal.memory.turn_ir._protocol_ids', side_effect=tracked):
        for i in range(1000):
            store.stream_assistant('long', replace(message, parts=(TextPartIR(str(i)),)))
    assert visited == [1] * 1000
    assert before.messages[-1].text == 'a'
    after = store.get('long')
    assert after.messages[-1].text == '999'
    assert after.revision == before.revision + 1000
    assert store.get('long') is after
    assert all(a is b for a, b in zip(before.messages[:-1], after.messages[:-1]))
    assert L1TurnStore(store.turns).get('long') == after


def test_invalid_stream_update_is_atomic_and_cannot_edit_frozen_round():
    store = L1TurnStore()
    old = result_turn(store, 'turn')
    message = LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR('next'),), message_id='next')
    store.stream_assistant('turn', message)
    before = store.get('turn')
    with pytest.raises(L1TurnProtocolError):
        store.stream_assistant('turn', replace(message, parts=(new_tool_call('same-call', 'read', {}),)))
    assert store.get('turn') is before
    with pytest.raises(L1TurnProtocolError):
        store.stream_assistant('turn', old.messages[1])
    assert store.get('turn') is before
    store.replace(before.settle())
    assert not store._rounds
    with pytest.raises(L1TurnProtocolError):
        store.stream_assistant('turn', message)


def test_stream_snapshot_rollback_and_request_view_reuse():
    store = L1TurnStore()
    store.begin('turn', user_text='work')
    message = LLMMessageIR(MessageRole.ASSISTANT, (new_tool_call('c', 'read', {}),), message_id='a')
    store.stream_assistant('turn', message)
    before = store.get('turn')
    view = store.context_view('turn')
    assert store.context_view('turn') is view
    result = before.append_tool_result(ToolResultIR('c', 'read', 'ok'))
    store.replace(result)
    store.restore_turn(before, expected=result)
    assert store.context_view('turn').tool_result('turn', 'c') is None
    store.replace(before.discard_assistant('a'))
    store.stream_assistant('turn', replace(message, parts=(TextPartIR('retry'),)))
    assert store.get('turn').messages[-1].text == 'retry'
    assert view.contains('turn', 'a')


def test_core_stream_delivery_avoids_snapshots_and_error_discards_only_live_response():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch
    from pal.core import PalCore
    from pal.memory import MemoryService, register_with_core
    from pal.llm.ir import LLMResponseIR, LLMResponseUpdate, LLMResponseDeltaKind, LLMFinishReason

    async def scenario():
        core = PalCore()
        memory = MemoryService()
        register_with_core(core.context, memory)
        memory.begin_l1_turn('turn', user_text='task')
        continuation = SimpleNamespace(turn_id='turn', interrupted=False)
        executor = core.turn_executor
        delivered = AsyncMock()
        message = LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR('a'),), message_id='stream')
        def update(message):
            return LLMResponseUpdate(LLMResponseIR(message, LLMFinishReason.STOP),
                                     LLMResponseDeltaKind.TEXT, text_delta='x')
        try:
            with patch.object(executor, 'execute_turn_effect_async', delivered):
                await executor._handle_ir_stream_update(continuation, update(message))
                with patch('pal.memory.turn_ir._ActiveRound.snapshot', side_effect=AssertionError('whole turn read')):
                    for i in range(50):
                        await executor._handle_ir_stream_update(continuation, update(
                            replace(message, parts=(TextPartIR(str(i)),))))
                assert delivered.await_count == 51
                before = memory.active_l1_turn('turn')
                assert before.messages[-1].text == '49'
                error = LLMResponseUpdate(LLMResponseIR(message, LLMFinishReason.ERROR),
                                          LLMResponseDeltaKind.STATE)
                await executor._handle_ir_stream_update(continuation, error)
                assert len(memory.active_l1_turn('turn').messages) == 1
                assert before.messages[-1].text == '49'
        finally:
            core.close()
    asyncio.run(scenario())
