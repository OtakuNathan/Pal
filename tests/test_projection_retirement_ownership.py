"""Regression for flat item ownership after partial block retirement.

Adapted from the external 6b0e914 review; exercises real projection components
and installed ownership, including the private prefix and pending tail.
"""
from __future__ import annotations

import hashlib
import json
import unittest

from pal.llm.ir import (
    GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole,
    MessageState, TextPartIR, WireShape,
)
from pal.llm.projection_contracts import (
    AppendReceipt, AttemptKey, EndpointBinding, HistoryCommitReceipt,
    HistoryCursor, LogicalSessionId, OwnerFence,
)
from pal.llm.projection_session import (
    EndpointProjectionSession, HistoryView, LeftReplacement, _retire_wire_item,
)


def _item(*texts):
    return {"role": "user", "content": [{"type": "text", "text": text} for text in texts]}


def _message(role, text, mid):
    return LLMMessageIR(role=role, parts=(TextPartIR(text),), message_id=mid,
                        state=MessageState.COMPLETE)


def _cursor(epoch, sequence, label):
    return HistoryCursor(history_epoch=epoch, block_sequence=sequence,
                         prefix_digest=hashlib.sha256(label.encode()).hexdigest())


def _shell():
    return LLMRequestIR(
        messages=(_message(MessageRole.SYSTEM, "BASE", "base"),), tools=(),
        policy=GenerationPolicyIR(max_output_tokens=128),
    )


def _merged_session():
    """The existing supported component sequence, not a made-up root cut.

    No runtime submission event is fabricated. We use the session API to
    preserve R's whole accepted contribution while replacing only its L seam.
    """
    session = EndpointProjectionSession(LogicalSessionId("review-6b0e914"))
    session.bind(EndpointBinding(
        endpoint_id="review-endpoint", model_id="review-model",
        wire_shape=WireShape.ANTHROPIC_MESSAGES,
        endpoint_spec_revision="review-spec",
        continuation_policy_version="review-policy",
        config_fingerprint="review-fingerprint",
    ))
    left = (
        _message(MessageRole.USER, "RETIRED_SEED", "old-seed"),
        _message(MessageRole.USER, "RETIRED_INSTRUCTION", "old-user"),
    )
    session.on_left_replaced(LeftReplacement(
        seed_messages=left, seed_coverage_ids=tuple(m.message_id for m in left),
        kept_frozen_messages=(), cursor_after=_cursor(1, 1, "initial"),
        left_revision=1,
    ))
    right = (
        _message(MessageRole.USER, "KEEP_QUESTION", "right-q"),
        _message(MessageRole.ASSISTANT, "KEEP_ANSWER", "right-a"),
    )
    attempt = AttemptKey(session.identity, OwnerFence(0), "right-round")
    session.begin_round(attempt, requires_native=False)
    before = session.frontier
    session.prepare_normal(HistoryView(cursor=before, messages=(right[0],)),
                           request_shell=_shell())
    session.observe_commit(
        HistoryCommitReceipt(
            attempt=attempt,
            append=AppendReceipt(before=before, after=_cursor(1, 2, "accepted"), block_count=1),
            closed_call_ids=(), native_committed=False,
        ),
        accepted_messages=(right[1],), span_message_ids=tuple(m.message_id for m in right),
    )
    return session, right


class OwnershipShapeTests(unittest.TestCase):
    def test_T01_partial_retirement_returns_flat_item_ownership(self):
        original = _item("LEFT", "RIGHT")
        result = _retire_wire_item(original, ("L", "R"), (("L",), ("R",)), {"R"})
        self.assertIsNotNone(result)
        item, owners, blocks = result
        self.assertEqual(item, _item("RIGHT"))
        self.assertEqual(original, _item("LEFT", "RIGHT"))
        self.assertEqual(blocks, (("R",),), "the block axis stays nested")
        self.assertEqual(owners, ("R",), "the item axis contains message IDs, not owner tuples")
        self.assertTrue(all(isinstance(mid, str) for mid in owners))

    def test_T02_multi_block_union_flattens_ids_and_deduplicates_in_order(self):
        result = _retire_wire_item(
            _item("L", "R1/R2", "R2", "R1/R2 duplicate"),
            ("L", "R1", "R2"),
            (("L",), ("R1", "R2"), ("R2",), ("R1", "R2")),
            {"R1", "R2"},
        )
        self.assertIsNotNone(result)
        item, owners, blocks = result
        self.assertEqual(len(item["content"]), 3)
        self.assertEqual(blocks, (("R1", "R2"), ("R2",), ("R1", "R2")))
        self.assertEqual(owners, ("R1", "R2"))

    def test_T03_real_merge_accept_rebase_keeps_runtime_ownership_typed(self):
        session, right = _merged_session()
        replacement = _message(MessageRole.USER, "NEW_SEED", "new-seed")
        session.on_left_replaced(LeftReplacement(
            seed_messages=(replacement,), seed_coverage_ids=(replacement.message_id,),
            kept_frozen_messages=right, cursor_after=_cursor(2, 1, "replaced"),
            left_revision=2,
        ))
        attempt = AttemptKey(session.identity, OwnerFence(0), "next")
        session.begin_round(attempt, requires_native=False)
        try:
            request = session.prepare_normal(
                HistoryView(cursor=session.frontier, messages=(
                    _message(MessageRole.USER, "NEXT_QUESTION", "next-q"),)),
                request_shell=_shell(),
            )
            text = json.dumps(json.loads(request.payload_json))
            for retained in ("NEW_SEED", "KEEP_QUESTION", "KEEP_ANSWER", "NEXT_QUESTION"):
                self.assertEqual(text.count(retained), 1)
            self.assertNotIn("RETIRED_SEED", text)
            self.assertNotIn("RETIRED_INSTRUCTION", text)
        finally:
            session.reject_commit(attempt.attempt_id, reason="test cleanup")
        self.assertTrue(session.chunks)
        # Check real installed state, not a stand-alone helper's local tuple.
        for chunk in session.chunks:
            for item_span in chunk.item_spans:
                self.assertTrue(all(isinstance(mid, str) for mid in item_span),
                                f"invalid installed item ownership: {item_span!r}")
        for item_span in session._prefix_item_spans:
            self.assertTrue(all(isinstance(mid, str) for mid in item_span))
        for entry in session._pending_wire_tail:
            self.assertTrue(all(isinstance(mid, str) for mid in entry.span_ids))

    def test_T04_no_retirement_all_retired_and_legacy_controls(self):
        item = _item("L", "R")
        with self.subTest(case="all kept"):
            result = _retire_wire_item(item, ("L", "R"), (("L",), ("R",)), {"L", "R"})
            self.assertEqual(result, (item, ("L", "R"), (("L",), ("R",))))
        with self.subTest(case="all retired"):
            self.assertIsNone(_retire_wire_item(item, ("L", "R"), (("L",), ("R",)), set()))
        with self.subTest(case="legacy unknown"):
            self.assertEqual(_retire_wire_item(item, (), (), set()), (item, (), ()))
        with self.subTest(case="partial retirement with unowned block"):
            result = _retire_wire_item(
                _item("L", "UNKNOWN", "R"), ("L", "R"),
                (("L",), (), ("R",)), {"R"},
            )
            self.assertEqual(result, (_item("UNKNOWN", "R"), ("R",), ((), ("R",))))


if __name__ == "__main__":
    unittest.main()
