"""N1.1 regressions: settlement/reasoning boundary, single outcome exit,
final publication gate, left message state identity.

Ported from the 29a879f review package (pal_v3_n1_review_29a879f)
draft spec tests (N1-R1..R4). Red run captured on 29a879f before fixes.
"""
from __future__ import annotations
import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pal.core.compaction import CompactionEngine
from pal.llm import generation_result_from_values
from pal.llm.ir import ReasoningPartIR, MessageState, TextPartIR
from pal.memory import MemoryService
from pal.memory.service import MemoryCompactResult  # noqa: F401  (contract check)
import pal.memory.history_root as history_module
from pal.memory.history_root import (
    HistoryRoot, HistoryRootError, default_summary_builder,
)
from pal.memory.turn_ir import L1TurnState, left_span_stamp
from tests.test_runtime_compaction import _valid_pal_payload
from tests.test_v3_n1_root_lifecycle import (
    Candidate, assistant, user, small_root, executor, service_with_work,
)


class ImmediateLLM:
    async def agenerate(self, request):
        return generation_result_from_values(text=_valid_pal_payload('ACCEPTED_NEW_LEFT'))


def reasoning_boundary(root: HistoryRoot) -> tuple:
    """A legal complete generic group, not TextPartIR masquerading as reasoning."""
    root.begin_right_turn('T', user_text='Q1')
    root.stream_right_assistant('T', assistant(
        'answer', 'a1', ReasoningPartIR('ACTUAL_REASONING_PART')))
    root.promote(include_active=True)
    assert root.cut.intra_messages > 0
    root.append_right_user('T', user('Q2 remains on the right', 'q2'))
    root.stream_right_assistant('T', assistant('final right answer', 'a2'))
    return root.left_messages()


class PrefixSettlementTests(unittest.TestCase):
    def test_real_reasoning_prefix_can_settle_without_rewriting_frozen_left(self):
        root = HistoryRoot()
        left = reasoning_boundary(root)
        settled = root.settle_right_turn('T')
        self.assertEqual(settled.state, L1TurnState.SETTLED)
        self.assertEqual(root.left_messages(), left)
        self.assertIn('Q2 remains', repr(root.right_messages()))

    def test_memory_service_settlement_uses_same_consistent_contract(self):
        memory = MemoryService()
        root = memory.history_root
        left = reasoning_boundary(root)
        settled = memory.settle_l1_turn('T')
        self.assertEqual(settled.state, L1TurnState.SETTLED)
        self.assertEqual(root.left_messages(), left)

    def test_legacy_no_cut_settlement_still_retires_neutral_reasoning(self):
        root = HistoryRoot()
        root.begin_right_turn('T', user_text='Q')
        root.stream_right_assistant('T', assistant('A', 'a', ReasoningPartIR('reason')))
        settled = root.settle_right_turn('T')
        self.assertTrue(all(not m.reasoning_text for m in settled.messages))

    def test_plain_text_named_reasoning_is_only_a_positive_control(self):
        root = HistoryRoot()
        root.begin_right_turn('T', user_text='Q')
        root.stream_right_assistant('T', assistant('A', 'a', TextPartIR('SECRET_REASONING')))
        root.promote(include_active=True)
        left = root.left_messages()
        root.settle_right_turn('T')
        self.assertEqual(root.left_messages(), left)


class FinalPublicationDeadlineTests(unittest.TestCase):
    def test_deadline_crossing_during_local_construction_does_not_publish(self):
        root = small_root()
        before = root.all_turns()
        clock = SimpleNamespace(now=0.0)
        root.begin_compact('op', reason='review', deadline_at=10.0)
        root.mark_ready('op', Candidate())

        def builder(candidate):
            seed = default_summary_builder(candidate)
            clock.now = 11.0  # deterministic passage of time before publication
            return seed

        with patch.object(history_module, 'time',
                          SimpleNamespace(monotonic=lambda: clock.now)):
            try:
                outcome = root.commit('op', build_summary_turn=builder)
            except HistoryRootError:
                pass
            else:
                self.assertNotEqual(outcome.status, 'committed')
        self.assertEqual(root.all_turns(), before)
        self.assertIsNone(root.active_run)

    def test_exact_deadline_matches_expire_deadline_boundary(self):
        root = small_root()
        root.begin_compact('op', reason='review', deadline_at=10.0)
        root.mark_ready('op', Candidate())
        before = root.all_turns()
        with patch.object(history_module, 'time',
                          SimpleNamespace(monotonic=lambda: 10.0)):
            try:
                outcome = root.commit('op')
            except HistoryRootError:
                pass
            else:
                self.assertNotEqual(outcome.status, 'committed')
        self.assertEqual(root.all_turns(), before)

    def test_unexpired_commit_positive_control(self):
        root = small_root()
        root.begin_compact('op', reason='review', deadline_at=10.0)
        root.mark_ready('op', Candidate())
        with patch.object(history_module, 'time',
                          SimpleNamespace(monotonic=lambda: 9.0)):
            self.assertEqual(root.commit('op').status, 'committed')


class SettlementOutcomeTests(unittest.TestCase):
    def test_post_commit_result_error_cannot_be_reported_as_uncommitted_failure(self):
        async def scenario():
            memory = service_with_work()
            ex = executor(memory, ImmediateLLM())
            ex._rebase_projection_after_left_install = Mock()
            original = CompactionEngine._result
            injected = []

            def fail_only_after_actual_commit(snapshot, **kwargs):
                if kwargs.get('status') == 'compacted':
                    self.assertEqual(memory.history_root.last_run.phase.value,
                                     'committed')
                    injected.append(True)
                    raise RuntimeError(
                        'injected result packaging failure AFTER actual commit')
                return original(snapshot, **kwargs)

            # Real engine and real install run. Only the post-commit
            # result-packaging boundary fails.
            with patch.object(CompactionEngine, '_result',
                              new=staticmethod(fail_only_after_actual_commit)):
                result = await ex.compact_memory_async(
                    memory, target_input_budget=100_000, reserved_output_tokens=1024,
                    continuation=SimpleNamespace(turn_id='T'))
            self.assertEqual(injected, [True])
            self.assertEqual(memory.history_root.last_run.phase.value, 'committed')
            self.assertIn('ACCEPTED_NEW_LEFT',
                          repr(memory.history_root.left_messages()))
            self.assertTrue(
                result.success,
                'accepted installation must dominate post-commit packaging failure')
            self.assertIsNotNone(
                result.memory_result,
                'callers need the accepted result, not an invented empty success')
            ex._rebase_projection_after_left_install.assert_called_once()
        asyncio.run(scenario())

    def test_reset_during_generation_does_not_make_old_cleanup_raise_stale_run(self):
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()

            class WaitingLLM:
                async def agenerate(self, request):
                    entered.set()
                    await release.wait()
                    return generation_result_from_values(
                        text=_valid_pal_payload())

            memory = service_with_work()
            ex = executor(memory, WaitingLLM())
            task = asyncio.create_task(ex.compact_memory_async(
                memory, target_input_budget=100_000, reserved_output_tokens=1024,
                continuation=SimpleNamespace(turn_id='T')))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                old_root = memory.history_root
                old_run_id = old_root.active_run.run_id
                old_root.reset()  # archive cleared, prior handle retired
                self.assertIsNone(old_root.run_record(old_run_id))
                release.set()
                try:
                    result = await asyncio.wait_for(task, 5)
                except asyncio.CancelledError:
                    result = None  # explicit cancellation is also valid terminal
                if result is not None:
                    self.assertFalse(result.success)
                self.assertEqual(memory.history_root.all_turns(), ())
                self.assertIsNone(memory.history_root.active_run)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        asyncio.run(scenario())

    def test_generation_failure_still_returns_failure_with_old_left(self):
        async def scenario():
            class BadLLM:
                async def agenerate(self, request):
                    return generation_result_from_values(text='NOT_JSON')

            memory = service_with_work()
            before = memory.l1_store.turns.turns
            ex = executor(memory, BadLLM())
            result = await ex.compact_memory_async(
                memory, target_input_budget=100_000, reserved_output_tokens=1024,
                continuation=SimpleNamespace(turn_id='T'))
            self.assertFalse(result.success)
            self.assertEqual(memory.l1_store.turns.turns, before)
            self.assertIsNone(memory.history_root.active_run)
        asyncio.run(scenario())


class LeftIdentityTighteningTests(unittest.TestCase):
    def test_state_only_change_is_part_of_selected_left_identity(self):
        root = HistoryRoot()
        root.begin_right_turn('T', user_text='Q')
        root.stream_right_assistant('T', assistant('A', 'a'))
        root.promote(include_active=True)
        root.begin_compact('op', reason='review')
        turn = root.store.get('T')
        changed = replace(turn.messages[-1], state=MessageState.IN_PROGRESS)
        root.store.replace(replace(
            turn, messages=(*turn.messages[:-1], changed),
            revision=turn.revision + 1))
        root.mark_ready('op', Candidate())
        with self.assertRaises(HistoryRootError):
            root.commit('op')

    def test_turn_revision_only_does_not_change_left_content_digest(self):
        root = small_root()
        turn = root.left_turns()[0]
        self.assertEqual(
            left_span_stamp((turn,)),
            left_span_stamp((replace(turn, revision=turn.revision + 100),)))


if __name__ == '__main__':
    unittest.main()
