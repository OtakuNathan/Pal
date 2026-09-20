"""N2/N11: the compact seed owned by L projects as its own settled reference.

Review NEXT_STEPS §3.2 / TEST_MATRIX N11: after a two-segment install the
cut owns the summary seed, so the model view of L is ONE standalone
historical reference — the same message the handoff sends — never summary
text parked inside a right-side user message, and never doubled.
full_source keeps the v2 anchor behavior.
"""
from __future__ import annotations

import unittest

from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.memory import MemoryService
from pal.memory.turn_ir import L1TurnIR, L1TurnState
from tests.test_v3_n1_root_lifecycle import Candidate, settled, user


def two_segment_service_with_seed() -> MemoryService:
    """A service whose root cut owns the compact seed (post-install state)."""
    memory = MemoryService()
    root = memory.history_root
    root.store.append(settled('L'))
    root.promote()
    root.begin_compact('op', reason='n11')
    root.mark_ready('op', Candidate())
    root.commit('op')
    memory.begin_l1_turn('T', user_text='CURRENT RIGHT')
    return memory


class ContinuityLViewTests(unittest.TestCase):
    def test_left_owned_seed_projects_standalone_not_into_r(self):
        memory = two_segment_service_with_seed()
        self.assertTrue(memory.l1_store.turns.summary_turn_id)
        seed_in_left = any(
            turn.turn_id == memory.l1_store.turns.summary_turn_id
            for turn in memory.history_root.left_turns()
        )
        self.assertTrue(seed_in_left, 'fixture: the cut must own the seed')

        system = LLMMessageIR(role=MessageRole.SYSTEM,
                              parts=(TextPartIR('BASE'),), message_id='sys')
        r_user = user('R question', 'ru1')
        out = memory.project_continuity([system, r_user])

        standalone = memory.l1_store.turns.continuity.standalone_id
        ids = [m.message_id for m in out]
        self.assertIn(standalone, ids)
        self.assertEqual(ids.index(standalone), 1,
                         'the L reference sits after the preamble, before R')
        self.assertEqual(ids.count(standalone), 1)
        # The right-side user message is untouched: no injected part, no
        # continuity identity stamped onto an R message.
        projected_r = out[ids.index('ru1')]
        self.assertEqual(projected_r.parts, r_user.parts)
        self.assertNotIn('continuity_id', projected_r.metadata)
        # The projected seed text equals the installed left seed: normal
        # and handoff share one L projection.
        seed_text = ''.join(
            part.text for m in memory.history_root.left_turns()[0].messages
            for part in m.parts if isinstance(part, TextPartIR))
        self.assertIn(seed_text.strip()[:32], out[1].text)

    def test_standalone_projection_is_idempotent(self):
        memory = two_segment_service_with_seed()
        standalone = memory.l1_store.turns.continuity.standalone_id
        r_user = user('again', 'ru2')
        once = memory.project_continuity([r_user])
        twice = memory.project_continuity(once)
        self.assertEqual(
            [m.message_id for m in twice].count(standalone), 1)

    def test_full_source_seed_keeps_anchor_behavior(self):
        memory = MemoryService()
        memory.l1_store.turns.append(L1TurnIR(
            turn_id='summary',
            messages=(LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(TextPartIR('v2 summary text'),),
                semantic_kind='runtime_context_summary'),),
            state=L1TurnState.SETTLED))
        memory.l1_store.turns.append(L1TurnIR(
            turn_id='T',
            messages=(user('anchored question', 'a-u'),),
            state=L1TurnState.SETTLED))
        seed_in_left = any(
            turn.turn_id == memory.l1_store.turns.summary_turn_id
            for turn in memory.history_root.left_turns())
        self.assertFalse(seed_in_left, 'fixture: full_source cut owns nothing')

        out = memory.project_continuity([user('anchored question', 'a-u')])
        standalone = memory.l1_store.turns.continuity.standalone_id
        self.assertNotIn(standalone, [m.message_id for m in out],
                         'v2 anchor path must not switch to standalone')
        anchored = next(m for m in out if m.message_id == 'a-u')
        self.assertIn('v2 summary text', anchored.text)
        self.assertEqual(anchored.metadata.get('continuity_id'),
                         memory.l1_store.turns.continuity.source_id)


if __name__ == '__main__':
    unittest.main()
