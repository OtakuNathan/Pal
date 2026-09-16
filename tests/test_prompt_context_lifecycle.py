from dataclasses import replace
import pytest

from pal.core.prompt_context import prepare_context, applicable_context, STATE_KEY
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR, WireShape
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.llm.ir import LLMRequestIR, GenerationPolicyIR
from pal.memory.turn_ir import L1TurnIR, L1TurnProtocolError
from pal.memory.service import MemoryService
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolCallIR


def candidate(value='A', **extra):
    return {'key': 'route', 'role': 'developer', 'content': value, **extra}


def advance(turn, candidates):
    stable, additions, state = prepare_context(turn, [], candidates)
    return turn.append_prompt_contexts(additions, state), additions


def test_fifty_unchanged_rounds_are_quiet():
    turn, first = advance(L1TurnIR.begin('t', user_text='work'), [candidate()])
    original = turn
    for _ in range(50):
        turn, additions = advance(turn, [candidate()])
        assert not additions
        assert turn is original
    assert len(first) == 1
    assert turn.metadata[STATE_KEY]['sources']['route']['revision'] == 1


def test_changes_withdrawal_and_recovery_keep_source_revision():
    turn = L1TurnIR.begin('t', user_text='work')
    records = []
    for value in ['A', 'B', 'A']:
        turn, added = advance(turn, [candidate(value)])
        records.extend(added)
    assert [m.metadata['source_revision'] for m in records] == [1, 2, 3]
    assert 'replaces="2"' in records[-1].text
    lost = replace(turn, messages=turn.messages[:1])
    restored, added = advance(lost, [candidate()])
    assert len(added) == 1 and added[0].metadata['source_revision'] == 3
    assert added[0].message_id != records[-1].message_id
    assert 'action="restore"' in added[0].text
    withdrawn, added = advance(restored, [])
    assert added[0].metadata['withdrawn']
    assert 'action="withdraw"' in added[0].text
    assert advance(withdrawn, [])[1] == ()


def test_events_are_not_recreated_after_context_loss():
    item = candidate('completed', kind='event', event_id='call-1:complete')
    turn, _ = advance(L1TurnIR.begin('t'), [item])
    lost = replace(turn, messages=())
    assert advance(lost, [item])[1] == ()
    with pytest.raises(ValueError, match='identity reused'):
        advance(turn, [{**item, 'content': 'different'}])


def test_atomic_store_failure_and_stale_snapshot(monkeypatch):
    service = MemoryService()
    turn = service.begin_l1_turn('t', user_text='work')
    _, added, state = prepare_context(turn, [], [candidate()])
    with monkeypatch.context() as patch:
        def fail(_):
            raise RuntimeError('write failed')
        patch.setattr(service.l1_store, 'replace', fail)
        with pytest.raises(RuntimeError):
            service.append_l1_prompt_contexts('t', added, state, expected_revision=turn.revision)
    assert service.active_l1_turn('t') is turn
    committed = service.append_l1_prompt_contexts('t', added, state, expected_revision=turn.revision)
    with pytest.raises(L1TurnProtocolError, match='snapshot changed'):
        service.append_l1_prompt_contexts('t', added, state, expected_revision=turn.revision)
    assert service.active_l1_turn('t') is committed


def test_context_cannot_split_pending_tool_batch():
    turn = L1TurnIR.begin('t').append(LLMMessageIR(role=MessageRole.ASSISTANT,
        parts=(ToolCallIR(call_id='call', name='probe', arguments={}),)))
    _, messages, state = prepare_context(turn, [], [candidate()])
    with pytest.raises(L1TurnProtocolError, match='closed tool batch'):
        turn.append_prompt_contexts(messages, state)
    assert STATE_KEY not in turn.metadata


@pytest.mark.parametrize('shape', list(WireShape))
def test_expired_finalization_never_replays_as_instruction(shape):
    turn, _ = advance(L1TurnIR.begin('A', user_text='old task'),
                      [candidate('Only produce final text; tools are terminated.')])
    messages = applicable_context(turn.messages, active_turn_id='B')
    messages.append(LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR('Continue editing the file.'),)))
    request = LLMRequestIR(tools=(), messages=tuple(messages), policy=GenerationPolicyIR(max_output_tokens=20))
    payload = codec_for_shape(shape).encode(request, ShapeContext(shape, 'fixture', 'fixture')).payload
    assert 'tools are terminated' not in str(payload)
    assert 'Continue editing' in str(payload)
    assert 'tools are terminated' in turn.messages[-1].text


@pytest.mark.parametrize('shape', list(WireShape))
def test_runtime_guidance_stays_after_original_user_block(shape):
    turn, _ = advance(L1TurnIR.begin('t', user_text='original request'), [candidate()])
    request = LLMRequestIR(tools=(), messages=turn.messages, policy=GenerationPolicyIR(max_output_tokens=20))
    encoded = codec_for_shape(shape).encode(request, ShapeContext(shape, 'fixture', 'fixture'))
    assert len(encoded.message_spans) == 2
    assert encoded.message_spans[0].cache_targets != encoded.message_spans[1].cache_targets
    assert 'scope="turn"' in str(encoded.payload)


def test_default_snapshot_does_not_freeze_changed_guidance():
    stable = [LLMMessageIR(role=MessageRole.DEVELOPER, parts=(TextPartIR('default A'),))]
    turn = L1TurnIR.begin('t')
    frozen, added, state = prepare_context(turn, stable, [candidate('A', instruction=True)])
    assert not added
    turn = turn.append_prompt_contexts(added, state)
    frozen2, added2, _ = prepare_context(turn, [], [candidate('B', instruction=True)])
    assert frozen2 == frozen
    assert 'B' in added2[0].text and 'Unless the current user request' in added2[0].text


def test_compaction_keeps_only_current_state_without_making_new_revision():
    from pal.core.prompt_context import projected_context
    turn = L1TurnIR.begin('t')
    event = candidate('temporary advice', key='advice', kind='event', event_id='advice-1')
    for value in ['A', 'B', 'A']:
        turn, _ = advance(turn, [candidate(value), event])
    _, additions, state = prepare_context(turn, [], [candidate('A'), event], boundary='compact-1')
    turn = turn.append_prompt_contexts(additions, state)
    assert not additions
    visible = projected_context(turn)
    assert len(visible) == 1
    assert visible[0].metadata['source_revision'] == 3
    assert len(turn.messages) == 4
    _, additions, again = prepare_context(turn, [], [candidate('A'), event], boundary='compact-1')
    assert not additions and again == thaw_json(turn.metadata[STATE_KEY])


def test_completed_sidecars_survive_partial_batch_interruption():
    from types import SimpleNamespace
    from pal.core.prompt_context import completed_tool_contexts
    from pal.shared.tool_protocol import ToolResultIR, ToolContextMessageIR
    service = MemoryService()
    turn = service.begin_l1_turn('t')
    calls = [ToolCallIR(call_id='done', name='probe', arguments={}),
             ToolCallIR(call_id='pending', name='probe', arguments={})]
    service.upsert_l1_assistant('t', LLMMessageIR(role=MessageRole.ASSISTANT, parts=tuple(calls)))
    turn = service.append_l1_tool_result('t', ToolResultIR(call_id='done', name='probe', content='completed'))
    continuation = SimpleNamespace(pending_tool_call_batch=calls, pending_tool_results=[
        SimpleNamespace(context_messages=(ToolContextMessageIR(content='reference manual', semantic_kind='skill_context'),))])
    contexts = completed_tool_contexts(continuation, turn)
    closed = service.interrupt_l1_turn('t', reason='cancelled', context_messages=contexts)
    assert not closed.pending_call_ids
    assert len([m for m in closed.messages if m.message_id.startswith('tool-context:')]) == 1
    assert 'reference manual' in closed.messages[-1].text
    assert all(call.call_id != 'pending' for m in closed.messages for call in m.tool_calls)


def test_checklist_echo_covers_state_without_a_second_sync_message():
    from pal.shared.tool_protocol import ToolResultIR
    import json
    turn = L1TurnIR.begin('t')
    call = ToolCallIR(call_id='check', name='checklist_check', arguments={})
    turn = turn.upsert_assistant(LLMMessageIR(role=MessageRole.ASSISTANT, parts=(call,)))
    turn = turn.append_tool_result(ToolResultIR(call_id='check', name='checklist_check', content=json.dumps(
        {'echo': {'tag': 'checklist', 'markdown': 'Stage done', 'payload': {'active': True}}})))
    turn, added = advance(turn, [candidate('Stage done', coverage_kind='checklist')])
    assert not added
    assert turn.metadata[STATE_KEY]['sources']['route']['message_id'] == turn.messages[-1].message_id


def test_reference_material_remains_historical_user_data():
    turn, _ = advance(L1TurnIR.begin('A'), [candidate('source evidence', role='user')])
    retained = applicable_context(turn.messages, active_turn_id='B')
    assert retained == list(turn.messages)
    assert retained[0].role == MessageRole.USER


def test_explicit_source_revision_survives_rendering_and_recovery():
    turn, _ = advance(L1TurnIR.begin('t'), [candidate(source_revision=7)])
    for _ in range(50):
        turn, added = advance(turn, [candidate(source_revision=7)])
        assert not added
    turn, added = advance(turn, [candidate('B', source_revision=8)])
    assert added[0].metadata['source_revision'] == 8
    with pytest.raises(ValueError, match='does not advance'):
        advance(turn, [candidate('C', source_revision=8)])


def test_distinct_events_from_same_source_and_new_event_at_rebuild():
    from pal.core.prompt_context import projected_context
    first = candidate('first', kind='event', event_id='one')
    second = candidate('second', kind='event', event_id='two')
    turn, added = advance(L1TurnIR.begin('t'), [first, second])
    assert len(added) == 2
    third = candidate('third', kind='event', event_id='three')
    _, additions, state = prepare_context(turn, [], [first, second, third], boundary='rebuild')
    turn = turn.append_prompt_contexts(additions, state)
    assert len(additions) == 1
    assert projected_context(turn) == list(additions)
    assert 'event_id="three"' in additions[0].text


def test_compaction_drops_expired_control_and_does_not_reuse_its_anchor():
    from pal.core.compaction import CompactionEngine, CompactionSnapshot, CompactionClockKind, _scope_safe_snapshot
    from pal.core.pal_compaction import PalCompactionPolicy
    from pal.memory.contracts import L1TranscriptMessage
    turn, _ = advance(L1TurnIR.begin('A'), [candidate('expired finalization')])
    replay = LLMRequestIR(messages=turn.messages, tools=(), policy=GenerationPolicyIR(max_output_tokens=20))
    snapshot = CompactionSnapshot(
        target_input_budget=10000, reserved_output_tokens=1000,
        clock_kind=CompactionClockKind.USER_TURN, clock_value=1,
        memory_items=((L1TranscriptMessage(role='developer', content='expired finalization',
            kind='pal_prompt_context', payload={'pal_authored': True}),),),
        replay_request=replay, replay_wire_shape='openai_response')
    assert _scope_safe_snapshot(snapshot).memory_items == ((),)
    request = CompactionEngine(PalCompactionPolicy())._request(snapshot, 'historical facts', attempt=0)
    assert 'expired finalization' not in str(request.messages)
    assert request.metadata.get('preferred_endpoint_source') is None
    assert snapshot.replay_request is replay
