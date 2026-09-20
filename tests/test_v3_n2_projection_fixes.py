"""N2 regressions: handoff base (F3), rebase keeper/native/epoch (F4),
full wire contract on PreparedRequest (W2).

Drafts ported from the review packages (test_review_regressions.py /
NEXT_STEPS §3.3) plus contract assertions for the new fields.  Red run on
the pre-N2 tree (7cf1d2d) captured in logs_n2_prefix_red.txt.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from types import SimpleNamespace

from pal.core.turn_executor import TurnExecutor
from pal.llm.continuation_policy import NativeCandidate
from pal.llm.ir import (
    GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole, TextPartIR,
    WireShape,
)
from pal.llm.projection_contracts import (
    AppendReceipt, AttemptKey, EndpointBinding, HistoryCommitReceipt,
    HistoryCursor, LogicalSessionId, OwnerFence,
)
from pal.llm.projection_session import (
    EndpointProjectionSession, HistoryView, LeftReplacement,
)
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.memory import MemoryService
from tests.test_v3_n1_root_lifecycle import (
    assistant, user, executor as make_executor,
)


def session(shape=WireShape.OPENAI_COMPLETION) -> EndpointProjectionSession:
    s = EndpointProjectionSession(LogicalSessionId('review:resident'))
    s.bind(EndpointBinding(endpoint_id='review-endpoint', model_id='review-model',
                           wire_shape=shape, endpoint_spec_revision='r1',
                           continuation_policy_version='p1', config_fingerprint='f1'))
    return s


def key(s, aid):
    return AttemptKey(s.identity, OwnerFence(0), aid)


def add_round(s, prefix: str, seq: int, *, native_text: str | None = None):
    q, a = user(prefix + ' Q', prefix + '-q'), assistant(prefix + ' A', prefix + '-a')
    attempt = key(s, prefix)
    before = s.frontier
    s.begin_round(attempt, requires_native=native_text is not None)
    s.prepare_normal(HistoryView(cursor=before, messages=(q,)))
    if native_text is not None:
        s.attach_native(attempt, NativeCandidate(
            wire_shape=s.binding.wire_shape, endpoint_id=s.binding.endpoint_id,
            model_id=s.binding.model_id, call_ids=(),
            payload_json=json.dumps({'message': {
                'role': 'assistant', 'content': 'R A',
                'reasoning_content': native_text}})))
    after = HistoryCursor(history_epoch=0, block_sequence=seq,
                          prefix_digest=hashlib.sha256(prefix.encode()).hexdigest())
    s.observe_commit(HistoryCommitReceipt(
        attempt=attempt, append=AppendReceipt(before=before, after=after, block_count=1),
        closed_call_ids=(), native_committed=native_text is not None),
        accepted_messages=() if native_text is not None else (a,),
        span_message_ids=(q.message_id, a.message_id))
    return (q, a)


class HandoffBaseTests(unittest.TestCase):
    def test_handoff_keeps_nonempty_base_for_all_shapes(self):
        for shape in (WireShape.OPENAI_COMPLETION, WireShape.OPENAI_RESPONSE,
                      WireShape.ANTHROPIC_MESSAGES):
            with self.subTest(shape=shape.value):
                s = session(shape)
                base = LLMMessageIR(role=MessageRole.SYSTEM,
                                    parts=(TextPartIR('BASE_SENTINEL'),),
                                    message_id='base')
                left = (user('LEFT Q', 'lq'), assistant('LEFT A', 'la'))
                instruction = user('HANDOFF_SENTINEL', 'instruction')
                shell = LLMRequestIR(messages=(base,), tools=(),
                                     policy=GenerationPolicyIR(max_output_tokens=4096))
                actual = json.loads(s.prepare_handoff(
                    left, instruction=instruction, attempt=key(s, 'handoff'),
                    request_shell=shell).payload_json)
                full = LLMRequestIR(messages=(base, *left, instruction), tools=(),
                                    policy=shell.policy)
                expected = json.loads(json.dumps(dict(
                    codec_for_shape(shape).encode(
                        full, ShapeContext(wire_shape=shape,
                                           endpoint_id='review-endpoint',
                                           model_id='review-model')).payload)))
                self.assertIn('BASE_SENTINEL', json.dumps(actual))
                self.assertEqual(actual, expected)

    def test_handoff_carries_full_wire_contract(self):
        for shape in (WireShape.OPENAI_COMPLETION, WireShape.ANTHROPIC_MESSAGES):
            with self.subTest(shape=shape.value):
                s = session(shape)
                base = LLMMessageIR(role=MessageRole.SYSTEM,
                                    parts=(TextPartIR('BASE_SENTINEL'),),
                                    message_id='base')
                left = (user('LEFT Q', 'lq'), assistant('LEFT A', 'la'))
                shell = LLMRequestIR(messages=(base,), tools=(),
                                     policy=GenerationPolicyIR(max_output_tokens=4096))
                request = s.prepare_handoff(
                    left, instruction=user('HANDOFF', 'instruction'),
                    attempt=key(s, 'handoff'), request_shell=shell)
                full = LLMRequestIR(messages=(base, *left, user('HANDOFF', 'instruction')),
                                    tools=(), policy=shell.policy)
                oracle = codec_for_shape(shape).encode(
                    full, s._shape_context())
                self.assertEqual(tuple(request.message_spans),
                                 tuple(oracle.message_spans))
                self.assertEqual(dict(request.extra_body),
                                 dict(oracle.extra_body))
                self.assertEqual(tuple(request.applied_cache_breakpoint_message_ids),
                                 tuple(oracle.applied_cache_breakpoint_message_ids))


class RebaseKeeperTests(unittest.TestCase):
    def test_left_replacement_replays_kept_native_not_just_kept_ir(self):
        s = session()
        add_round(s, 'L', 1)
        right = add_round(s, 'R', 2, native_text='SYNTHETIC_NATIVE_R_NOT_FOR_LIVE_API')
        self.assertIn('SYNTHETIC_NATIVE_R_NOT_FOR_LIVE_API', repr(s._prefix_items))
        s.on_left_replaced(LeftReplacement(
            seed_messages=(assistant('NEW SEED', 'seed'),),
            kept_frozen_messages=right,
            cursor_after=HistoryCursor(1, 1, 'a' * 64), left_revision=3))
        s.begin_round(key(s, 'next'), requires_native=False)
        actual = s.prepare_normal(
            HistoryView(cursor=s.frontier, messages=(user('NEXT', 'next-q'),)))
        self.assertIn('SYNTHETIC_NATIVE_R_NOT_FOR_LIVE_API', actual.payload_json)
        self.assertIn('R', s.native_by_attempt)

    def test_executor_rebase_keeps_already_populated_right_native(self):
        s = session()
        add_round(s, 'L', 1)
        right = add_round(s, 'R', 2, native_text='SYNTHETIC_R_NATIVE')
        memory = MemoryService()
        ex = make_executor(memory, SimpleNamespace())
        history = SimpleNamespace(
            left_messages=lambda: (assistant('NEW SEED', 'seed'),),
            right_messages=lambda: right, left_revision=4)
        hosted = SimpleNamespace(endpoint_projection_session=lambda scope: s)
        ex._rebase_projection_after_left_install(hosted, 'review:resident', history)
        self.assertIn('R', s.native_by_attempt,
                      'real executor helper treated all old chunks as replaced L')
        self.assertIn('SYNTHETIC_R_NATIVE', repr(s._prefix_items))
        # F4: the cursor epoch comes from the history authority, not a
        # hardcoded constant.
        self.assertEqual(s.frontier.history_epoch, 4)

    def test_rebase_head_system_re_owns_to_surviving_rounds(self):
        s = session(WireShape.ANTHROPIC_MESSAGES)
        head = LLMMessageIR(role=MessageRole.SYSTEM,
                            parts=(TextPartIR('HEAD_FROM_LEFT'),),
                            message_id='hsys')
        r1 = (head, user('q1', 'u1'), assistant('a1', 's1'))
        attempt1 = key(s, 'r1')
        s.begin_round(attempt1, requires_native=False)
        s.prepare_normal(HistoryView(cursor=s.frontier, messages=r1))
        s.observe_commit(HistoryCommitReceipt(
            attempt=attempt1,
            append=AppendReceipt(
                before=s.frontier,
                after=HistoryCursor(0, 1, 'b' * 32), block_count=1),
            closed_call_ids=(), native_committed=False),
            accepted_messages=(), span_message_ids=[m.message_id for m in r1])
        r2 = add_round(s, 'R2', 2)
        begin = key(s, 'probe')
        s.begin_round(begin, requires_native=False)
        probe = s.prepare_normal(
            HistoryView(cursor=s.frontier, messages=(user('p', 'p1'),)))
        self.assertIn('HEAD_FROM_LEFT', probe.payload_json)
        s.observe_commit(HistoryCommitReceipt(
            attempt=begin,
            append=AppendReceipt(
                before=s.frontier,
                after=HistoryCursor(0, 3, 'c' * 32), block_count=1),
            closed_call_ids=(), native_committed=False),
            accepted_messages=(), span_message_ids=['p1'])
        # Rebase keeping only r2 + probe: the hoisted head from the retired
        # left round dies with it.
        kept = (*r2, user('p', 'p1'))
        s.on_left_replaced(LeftReplacement(
            seed_messages=(assistant('NEW SEED', 'seed'),),
            kept_frozen_messages=kept,
            cursor_after=HistoryCursor(1, 1, 'd' * 64), left_revision=1))
        s.begin_round(key(s, 'after'), requires_native=False)
        after = s.prepare_normal(
            HistoryView(cursor=s.frontier, messages=(user('n', 'n1'),)))
        self.assertNotIn('HEAD_FROM_LEFT', after.payload_json)


if __name__ == '__main__':
    unittest.main()
