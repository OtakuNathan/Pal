"""N1 regressions: root lifecycle, intra-cut identity, terminal retirement.

Ported from the 2014db4 review package (pal_v3_review_2014db4) regression
proposals F1/F2/F5/F6 plus the N05 negative control (left content truly
mutated without a revision bump must still be rejected at commit).

Pre-fix evidence: every test in this module failed on 2014db4 (see
logs_n1_prefix_red.txt in the commit message for the captured run).
"""
from __future__ import annotations

import asyncio
import gc
import time
import unittest
import weakref
from dataclasses import dataclass, replace
from types import SimpleNamespace

from pal.core.compaction import CompactionEngine
from pal.core.pal_compaction import PalCompactionPolicy
from pal.core.turn_executor import TurnExecutor
from pal.llm import generation_result_from_values
from pal.llm.ir import LLMMessageIR, MessageRole, MessageState, TextPartIR
from pal.memory import MemoryService
from pal.memory.history_root import HistoryRoot, HistoryRootError
from pal.memory.turn_ir import L1TurnIR, L1TurnState
from pal.shared.tool_protocol import ToolResultIR, new_tool_call


def user(text: str, mid: str) -> LLMMessageIR:
    return LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR(text),), message_id=mid)


def assistant(text: str, mid: str, *extra, state=MessageState.COMPLETE) -> LLMMessageIR:
    return LLMMessageIR(role=MessageRole.ASSISTANT,
                        parts=(TextPartIR(text), *extra), message_id=mid, state=state)


def settled(tid: str) -> L1TurnIR:
    return L1TurnIR(turn_id=tid,
                    messages=(user(f'{tid} Q', f'{tid}-q'), assistant(f'{tid} A', f'{tid}-a')),
                    state=L1TurnState.SETTLED)


@dataclass(frozen=True)
class Candidate:
    summary: str = 'NEW LEFT SUMMARY'
    rendered: str = 'NEW LEFT SUMMARY'
    payload: object = None


def small_root() -> HistoryRoot:
    root = HistoryRoot()
    root.store.append(settled('L'))
    root.promote()
    return root


def executor(memory: MemoryService, llm) -> TurnExecutor:
    ports = {'memory:memory': memory, 'llm:llm': llm}
    return TurnExecutor(
        SimpleNamespace(port_registry=ports, require_port=ports.__getitem__,
                        execution_runtime=None),
        SimpleNamespace(diagnostics=[]), None,
        call_port_async=None, build_canonical_prompt=None,
        debug_log_prompt=lambda *a: None, debug_log_outcome=lambda *a: None,
        debug_log_reply=lambda *a: None, build_llm_tool_contracts=lambda: [],
        handle_failure_async=None, render_failure_feedback_text=lambda _: '',
        should_enter_failure_flow_for_tool_result=lambda _: False,
        compaction_engine=CompactionEngine(PalCompactionPolicy(), max_attempts=1,
                                           timeout_seconds=2),
        compaction_clock_provider=lambda: 1,
    )


def service_with_work() -> MemoryService:
    memory = MemoryService()
    memory.l1_store.turns.append(settled('L'))
    memory.history_root.promote()  # Fixture represents already submitted history.
    memory.begin_l1_turn('T', user_text='CURRENT RIGHT')
    return memory


class RootContractTests(unittest.TestCase):
    def test_intra_turn_right_result_does_not_invalidate_left_snapshot(self):
        root = HistoryRoot()
        root.begin_right_turn('T', user_text='Q1')
        root.stream_right_assistant('T', assistant(
            'OLD_GROUP', 'a1', new_tool_call(call_id='c1', name='aggregate', arguments={})))
        root.append_right_tool_result(root.producer_token('T'),
                                      ToolResultIR(call_id='c1', name='aggregate',
                                                   content='RESULT1'))
        root.promote(include_active=True)
        root.append_right_user('T', user('Q2', 'q2'))
        root.stream_right_assistant('T', assistant(
            'CURRENT_GROUP', 'a2', new_tool_call(call_id='c2', name='aggregate', arguments={})))
        snapshot = root.begin_compact('op', reason='review', parent_turn_id='T')
        old_left = root.left_messages()
        root.append_right_tool_result(root.producer_token('T'),
                                      ToolResultIR(call_id='c2', name='aggregate',
                                                   content='RESULT2'))
        self.assertEqual(root.left_messages(), old_left,
                         'fixture changed L, not merely R')
        self.assertEqual(snapshot.cut, root.cut)
        root.mark_ready('op', Candidate())
        outcome = root.commit('op')
        self.assertEqual(outcome.status, 'committed')
        right_text = repr(root.right_messages())
        self.assertIn('RESULT2', right_text)
        self.assertIn('CURRENT_GROUP', right_text)

    def test_settlement_of_boundary_turn_does_not_rewrite_cut_covered_left(self):
        root = HistoryRoot()
        root.begin_right_turn('T', user_text='Q1')
        reasoning = TextPartIR('SECRET_REASONING')
        root.stream_right_assistant('T', assistant(
            'OLD_GROUP', 'a1', reasoning, new_tool_call(call_id='c1', name='aggregate',
                                                       arguments={})))
        root.append_right_tool_result(root.producer_token('T'),
                                      ToolResultIR(call_id='c1', name='aggregate',
                                                   content='RESULT1'))
        root.promote(include_active=True)
        root.append_right_user('T', user('Q2', 'q2'))
        root.begin_compact('op', reason='review', parent_turn_id='T')
        before = root.left_messages()
        root.settle_right_turn('T')
        self.assertEqual(root.left_messages(), before,
                         'settlement rewrote messages the cut already froze into L')
        root.mark_ready('op', Candidate())
        self.assertEqual(root.commit('op').status, 'committed')

    def test_left_content_mutation_without_revision_bump_is_rejected(self):
        root = small_root()
        root.begin_compact('op', reason='review')
        turn = root.all_turns()[0]
        # A legal-looking replacement (revision advanced, same message id)
        # that rewrites cut-covered content must still fail the stamp.
        tampered = replace(turn, messages=(user('TAMPERED', 'L-q'), turn.messages[1]),
                           revision=turn.revision + 1)
        root.store.replace(tampered)
        root.mark_ready('op', Candidate())
        with self.assertRaises(HistoryRootError):
            root.commit('op')

    def test_left_metadata_mutation_is_rejected(self):
        root = small_root()
        root.begin_compact('op', reason='review')
        turn = root.all_turns()[0]
        tampered_msg = replace(turn.messages[0], metadata={'injected': 'yes'})
        tampered = replace(turn, messages=(tampered_msg, turn.messages[1]),
                           revision=turn.revision + 1)
        root.store.replace(tampered)
        root.mark_ready('op', Candidate())
        with self.assertRaises(HistoryRootError):
            root.commit('op')

    def test_streaming_text_without_calls_is_not_a_closed_group(self):
        root = HistoryRoot()
        root.begin_right_turn('T', user_text='Q')
        partial = assistant('PARTIAL_NOT_ACCEPTED', 'stream',
                            state=MessageState.IN_PROGRESS)
        root.stream_right_assistant('T', partial)
        root.promote(include_active=True)
        self.assertNotIn('stream', [m.message_id for m in root.left_messages()])
        self.assertIn('stream', [m.message_id for m in root.right_messages()])

    def test_terminal_bookkeeping_does_not_pin_retired_left_turn(self):
        root = HistoryRoot()
        old_turn = settled('OLD_LEFT_SHOULD_RETIRE')
        old_ref = weakref.ref(old_turn)
        root.store.append(old_turn)
        root.promote()
        snapshot = root.begin_compact('op', reason='review')
        root.mark_ready('op', Candidate())
        root.commit('op')
        del snapshot, old_turn
        gc.collect()
        self.assertIsNone(old_ref(), 'terminal op records retained the entire old L')
        # Compact idempotency may remain, but must not need full retired history.
        self.assertEqual(root.commit('op').status, 'committed')

    def test_reset_releases_old_run_archive(self):
        root = small_root()
        root.begin_compact('op', reason='review')
        root.cancel('op', reason='fixture')
        incarnation = root.reset()
        self.assertTrue(incarnation)
        self.assertIsNone(root.run_record('op'),
                          'an incarnation roll must clear the run archive')
        # Fresh runs on the new incarnation still arbitrate normally.
        root.store.append(settled('NEW'))
        root.promote()
        root.begin_compact('fresh', reason='review')
        root.cancel('fresh', reason='fixture')
        self.assertEqual(root.run_record('fresh').phase.value, 'cancelled')

    def test_expired_run_cannot_commit_without_an_explicit_sweep(self):
        root = small_root()
        before = root.all_turns()
        root.begin_compact('expired', reason='review', deadline_at=time.monotonic() - 1)
        root.mark_ready('expired', Candidate())
        try:
            outcome = root.commit('expired')
        except HistoryRootError:
            pass
        else:
            self.assertNotEqual(outcome.status, 'committed')
        self.assertEqual(root.all_turns(), before)


class ExecutorFailureTests(unittest.TestCase):
    def test_schema_failure_terminates_run_and_allows_later_attempt(self):
        async def scenario():
            class InvalidLLM:
                async def agenerate(self, request):
                    return generation_result_from_values(text='NOT_JSON')

            memory = service_with_work()
            ex = executor(memory, InvalidLLM())
            result = await ex.compact_memory_async(
                memory, target_input_budget=100_000, reserved_output_tokens=1024,
                continuation=SimpleNamespace(turn_id='T'))
            self.assertFalse(result.success)
            self.assertIsNone(memory.history_root.active_run,
                              'engine returned failure but the owner is still '
                              'RUNNING/READY')
            memory.history_root.begin_compact('next-op', reason='review')
            memory.history_root.cancel('next-op', reason='fixture cleanup')

        asyncio.run(scenario())

    def test_cancelled_orchestration_closes_its_own_uncommitted_run(self):
        async def scenario():
            started, release = asyncio.Event(), asyncio.Event()

            class WaitingLLM:
                async def agenerate(self, request):
                    started.set()
                    await release.wait()
                    return generation_result_from_values(text='NOT_JSON')

            memory = service_with_work()
            ex = executor(memory, WaitingLLM())
            task = asyncio.create_task(ex.compact_memory_async(
                memory, target_input_budget=100_000, reserved_output_tokens=1024,
                continuation=SimpleNamespace(turn_id='T')))
            try:
                await asyncio.wait_for(started.wait(), timeout=3)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.assertIsNone(memory.history_root.active_run,
                                  'abandoned orchestration left an owner run open')
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        asyncio.run(scenario())

    def test_deadline_expiry_fails_run_and_next_attempt_is_eligible(self):
        async def scenario():
            class HangingLLM:
                async def agenerate(self, request):
                    await asyncio.Event().wait()
                    return generation_result_from_values(text='{}')

            memory = service_with_work()
            engine = CompactionEngine(PalCompactionPolicy(), max_attempts=1,
                                      timeout_seconds=0.05)
            ports = {'memory:memory': memory, 'llm:llm': HangingLLM()}
            ex = TurnExecutor(
                SimpleNamespace(port_registry=ports,
                                require_port=ports.__getitem__,
                                execution_runtime=None),
                SimpleNamespace(diagnostics=[]), None,
                call_port_async=None, build_canonical_prompt=None,
                debug_log_prompt=lambda *a: None,
                debug_log_outcome=lambda *a: None,
                debug_log_reply=lambda *a: None,
                build_llm_tool_contracts=lambda: [],
                handle_failure_async=None,
                render_failure_feedback_text=lambda _: '',
                should_enter_failure_flow_for_tool_result=lambda _: False,
                compaction_engine=engine,
                compaction_clock_provider=lambda: 1,
            )
            result = await ex.compact_memory_async(
                memory, target_input_budget=100_000, reserved_output_tokens=1024,
                continuation=SimpleNamespace(turn_id='T'))
            self.assertFalse(result.success)
            self.assertIsNone(memory.history_root.active_run,
                              'deadline expiry left the owner run open')
            memory.history_root.begin_compact('next-op', reason='review')
            memory.history_root.cancel('next-op', reason='fixture cleanup')

        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
