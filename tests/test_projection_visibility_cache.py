"""Projection visibility and exact cache-address regression tests; no network."""
from dataclasses import replace
from tests.test_v3_n3_vertical_trace import _runtime, _executor, _request_for, _drive, CapturingTransport
from tests.test_v3_n1_root_lifecycle import user
from tests.test_cache_tail import context
from pal.memory import MemoryService
from pal.memory.context_view import projected_messages
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR, PromptRegionIR, WireShape, LLMRequestIR, GenerationPolicyIR
from pal.llm.prompt_cache import PromptCacheCoordinator


def test_expired_guidance_must_not_reappear_in_projection():
    memory = MemoryService()
    memory.begin_l1_turn('T', user_message=user('QUESTION_ONE', 'q1'))
    guidance = LLMMessageIR(
        role=MessageRole.DEVELOPER, parts=(TextPartIR('EXPIRED_FINALIZATION_SENTINEL'),),
        message_id='ctx1', semantic_kind='pal_prompt_context',
        metadata={'pal_authored': True, 'scope_turn_id': 'T'})
    turn = memory.l1_store.active('T')
    memory.append_l1_prompt_contexts('T', (guidance,), {}, expected_revision=turn.revision)
    runtime = _runtime(CapturingTransport(['ANSWER_ONE']))
    executor = _executor(memory, runtime)
    _drive(executor, _request_for(memory, ()))
    memory.settle_l1_turn('T')
    memory.begin_l1_turn('T2', user_message=user('QUESTION_TWO', 'q2'))
    request = _request_for(memory, ())
    visible = tuple(message for turn in memory.l1_store.turns.turns
                    for message in projected_messages(turn, settled=turn.turn_id != 'T2'))
    request = replace(request, messages=(request.messages[0], *visible))
    assert all(message.message_id != 'ctx1' for message in request.messages)
    pack = executor._prepare_turn_projection(runtime, request)
    assert pack is not None
    assert 'EXPIRED_FINALIZATION_SENTINEL' not in repr(pack[0].payload)
    session = pack[2]['session']
    generation = session.identity.projection_generation
    session.reject_commit(pack[2]['attempt'].attempt_id, reason='test_retry')
    retry = executor._prepare_turn_projection(runtime, request)
    assert retry is not None
    assert session.identity.projection_generation == generation
    assert 'EXPIRED_FINALIZATION_SENTINEL' not in repr(retry[0].payload)


def test_hybrid_keeps_fixed_turn_anchor_after_freezing_round():
    memory = MemoryService()
    memory.begin_l1_turn('T', user_message=replace(
        user('QUESTION_ONE', 'q1'), prompt_region=PromptRegionIR.ACTIVE_INPUT))
    runtime = _runtime(CapturingTransport(['ANSWER_ONE']))
    executor = _executor(memory, runtime)
    def request():
        result = _request_for(memory, ())
        return replace(result, messages=(replace(result.messages[0],
                       prompt_region=PromptRegionIR.STABLE_SYSTEM), *result.messages[1:]))
    _drive(executor, request())
    current = request()
    pack = executor._prepare_turn_projection(runtime, current)
    assert pack is not None
    assert 'QUESTION_ONE' in repr(pack[0].payload)
    coordinator = PromptCacheCoordinator()
    ctx = context('hybrid', WireShape.OPENAI_COMPLETION)
    cold = coordinator.plan(current, ctx)
    projected = coordinator.plan(current, ctx, pack[0])
    assert [(bp.label, bp.message_id, bp.path) for bp in projected.breakpoints] == [
        (bp.label, bp.message_id, bp.path) for bp in cold.breakpoints]


import json
import pytest
from pal.llm.projection_session import EndpointProjectionSession, HistoryView, LeftReplacement
from pal.llm.projection_contracts import LogicalSessionId
from pal.llm.projection_checkpoint import snapshot_projection, restore_projection
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from tests.test_projection_two_segment import _Lineage, _user, _assistant, _attempt, _cursor


@pytest.mark.parametrize('shape', list(WireShape))
def test_frozen_span_paths_survive_checkpoint_and_left_replacement(shape):
    if shape not in (WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION, WireShape.ANTHROPIC_MESSAGES):
        pytest.skip('not a projection codec')
    lineage = _Lineage(shape)
    old = (_user('OLD', 'old-q'), _assistant('OLD ANSWER', 'old-a'))
    keep = (_user('KEEP', 'keep-q'), _assistant('KEEP ANSWER', 'keep-a'))
    lineage.round_trip(old, 'old')
    lineage.round_trip(keep, 'keep')
    session = lineage.session
    restored = EndpointProjectionSession(LogicalSessionId('pal:resident'))
    restore_projection({'projection': snapshot_projection(session)},
                       l1_history_cursor=session.frontier, session=restored)
    for current in (session, restored):
        seed = _user('NEW SUMMARY', 'summary')
        current.on_left_replaced(LeftReplacement(
            seed_messages=(seed,), kept_frozen_messages=keep,
            cursor_after=_cursor(1, 'c' * 64, 1), left_revision=1,
            seed_coverage_ids=('summary',)))
        tail = _user('NEXT', 'next')
        current.begin_round(_attempt(current, 'next'), requires_native=False)
        prepared = current.prepare(HistoryView(current.frontier, (tail,)))
        full = codec_for_shape(shape).encode(
            LLMRequestIR(
                messages=(seed, *keep, tail), tools=(),
                policy=GenerationPolicyIR(max_output_tokens=4096)),
            ShapeContext(shape, 'endpoint-1', 'model-1'))
        actual = {s.message_id: s.cache_targets for s in prepared.message_spans}
        expected = {s.message_id: s.cache_targets for s in full.message_spans}
        # Rebase keeps surviving native item boundaries. Compare the exact
        # addressed blocks, not cold-encoder merging of adjacent user items.
        from pal.llm.cache_wire import at
        payload = json.loads(prepared.payload_json)
        assert actual.keys() == expected.keys()
        for mid in expected:
            assert [at(payload, p) for p in actual[mid]] == [
                at(full.payload, p) for p in expected[mid]]
        assert 'OLD ANSWER' not in prepared.payload_json


def test_pending_user_merge_retains_each_block_address():
    lineage = _Lineage(WireShape.ANTHROPIC_MESSAGES)
    lineage.round_trip((_user('FIRST', 'first'),), 'first')
    session = lineage.session
    session.begin_round(_attempt(session, 'next'), requires_native=False)
    prepared = session.prepare(HistoryView(session.frontier, (_user('SECOND', 'second'),)))
    spans = {s.message_id: s.cache_targets for s in prepared.message_spans}
    assert spans['first'] == (('messages', 0, 'content', 0),)
    assert spans['second'] == (('messages', 0, 'content', 1),)


@pytest.mark.parametrize('shape', [WireShape.OPENAI_RESPONSE, WireShape.OPENAI_COMPLETION])
def test_explicit_keeps_previous_tail_and_turn_anchor_across_projected_rounds(shape):
    from pal.llm.shapes.base import EncodedRequest
    from tests.test_projection_two_segment import _receipt
    lineage = _Lineage(shape)
    session = lineage.session
    coordinator = PromptCacheCoordinator()
    ctx = context('explicit', shape)
    history = []
    previous = None
    for round_index in range(3):
        tail = (
            replace(_assistant(f'A{round_index}', f'a{round_index}'), prompt_region=PromptRegionIR.ACTIVE_HISTORY),
            replace(_user(f'RESULT{round_index}', f'r{round_index}'), prompt_region=PromptRegionIR.ACTIVE_HISTORY),
        )
        if not history:
            tail = (replace(_user('TASK', 'task'), prompt_region=PromptRegionIR.ACTIVE_INPUT), *tail)
        history.extend(tail)
        request = LLMRequestIR(messages=tuple(history), tools=(),
                               policy=GenerationPolicyIR(max_output_tokens=4096), metadata={'turn_id': 'T'})
        attempt = _attempt(session, str(round_index))
        session.begin_round(attempt, requires_native=False)
        prepared = session.prepare(HistoryView(session.frontier, tail), request_shell=request)
        encoded = EncodedRequest(json.loads(prepared.payload_json), prepared.message_spans)
        plan, _, _ = coordinator.prepare_attempt(request, ctx, encoded, str(round_index))
        points = {point.label: point for point in plan.breakpoints}
        assert points['anchor_fixed'].message_id == 'task'
        assert points['tail_current'].message_id == f'r{round_index}'
        if previous is not None:
            assert points['tail_previous'].message_id == previous.message_id
            assert points['tail_previous'].path == previous.path
        previous = points['tail_current']
        session.observe_commit(_receipt(attempt, session.frontier, _cursor(round_index + 1, 'c' * 64)),
                               span_message_ids=tuple(m.message_id for m in tail))


def test_partial_block_retirement_moves_only_surviving_addresses():
    from pal.llm.projection_spans import retire_spans
    from pal.llm.shapes.base import EncodedMessageSpan
    entries = (
        EncodedMessageSpan('old', (('content', 0),), wire_item_paths=((),)),
        EncodedMessageSpan('kept', (('content', 1),), wire_item_paths=((),)),
    )
    item = {'role': 'user', 'content': [
        {'type': 'text', 'text': 'old'}, {'type': 'text', 'text': 'kept'}]}
    result = retire_spans(entries, item, (('old',), ('kept',)), {'kept'})
    assert [(s.message_id, s.cache_targets) for s in result] == [('kept', (('content', 0),))]


def test_bootstrap_left_view_does_not_reintroduce_expired_context():
    memory = MemoryService()
    memory.begin_l1_turn('T', user_message=user('QUESTION', 'q'))
    turn = memory.l1_store.active('T')
    guidance = LLMMessageIR(role=MessageRole.DEVELOPER,
        parts=(TextPartIR('OLD_GUIDANCE'),), message_id='guidance',
        semantic_kind='pal_prompt_context', metadata={'pal_authored': True, 'scope_turn_id': 'T'})
    memory.append_l1_prompt_contexts('T', (guidance,), {}, expected_revision=turn.revision)
    memory.upsert_l1_assistant('T', _assistant('ANSWER', 'a'))
    memory.settle_l1_turn('T')
    memory.history_root.promote()
    executor = _executor(memory, _runtime(CapturingTransport([])))
    seed, coverage = executor._left_view_seed(memory, memory.history_root)
    assert 'guidance' not in coverage
    assert 'OLD_GUIDANCE' not in repr(seed)
    assert {'q', 'a'} <= set(coverage)


def test_hoisted_head_span_survives_commit_and_restore():
    lineage = _Lineage(WireShape.ANTHROPIC_MESSAGES)
    developer = LLMMessageIR(role=MessageRole.DEVELOPER, parts=(TextPartIR('HEAD_GUIDE'),),
                             message_id='head')
    lineage.round_trip((developer, _user('ASK', 'ask'), _assistant('ANSWER', 'answer')), 'first')
    original = lineage.session
    restored = EndpointProjectionSession(LogicalSessionId('pal:resident'))
    restore_projection({'projection': snapshot_projection(original)},
                       l1_history_cursor=original.frontier, session=restored)
    for session in (original, restored):
        session.begin_round(_attempt(session, 'next'), requires_native=False)
        prepared = session.prepare(HistoryView(session.frontier, (_user('NEXT', 'next'),)))
        spans = {s.message_id: s.cache_targets for s in prepared.message_spans}
        assert spans['head'] == (('system', 0),)
        assert json.loads(prepared.payload_json)['system'][0]['text'] == 'HEAD_GUIDE'
