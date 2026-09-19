"""Seventh external review (pal_projection_review_4c20311) regressions.

C1: committed attempts are TERMINAL — begin_round refuses an attempt id
    already in the committed ledger (receipt replay is the idempotent
    path), so a duplicate dispatch can never hand the cancel path back
    to a finished attempt and drop its committed native, and a reopened
    draft can never be exported as committed native by the snapshot
    filter.
C2: a PRESENT but malformed owner_fence is a corrupt NEW-format
    snapshot, not a legacy one — restore fails closed BEFORE any state
    is installed.  A genuinely absent field keeps the documented
    pre-B4 fallback; valid fences keep the B4 semantics.

Test scenarios and assertions follow the review's attachment; controls
verify receipt-replay idempotency, different-ID draft cancellation, the
legacy fallback, and the valid-fence path.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace

from pal.llm.continuation_policy import NativeCandidate
from pal.llm.ir import (
    GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole,
    TextPartIR, WireShape,
)
from pal.llm.projection_checkpoint import (
    ProjectionCheckpointError, restore_projection, snapshot_projection,
)
from pal.llm.projection_contracts import (
    AppendReceipt, AttemptKey, EndpointBinding, HistoryCommitReceipt,
    HistoryCursor, LogicalSessionId, OwnerFence,
)
from pal.llm.projection_session import (
    EndpointProjectionSession, HistoryView, ProjectionSessionError,
)


def key(session, name, fence=2):
    return AttemptKey(session.identity, OwnerFence(fence), name)


def native(session, text):
    return NativeCandidate(
        wire_shape=session.binding.wire_shape,
        endpoint_id=session.binding.endpoint_id,
        model_id=session.binding.model_id,
        payload_json=json.dumps({"message": {"role": "assistant", "content": text}}),
        call_ids=(),
    )


def seeded():
    session = EndpointProjectionSession(LogicalSessionId("review-c:resident"))
    session.bind(EndpointBinding(
        endpoint_id="test-endpoint",
        model_id="test-model",
        wire_shape=WireShape.OPENAI_COMPLETION,
        endpoint_spec_revision="spec-1",
        continuation_policy_version="test-policy",
        config_fingerprint="test-config",
    ))
    accepted = key(session, "accepted", 2)
    session.begin_round(accepted, requires_native=True)
    shell = LLMRequestIR(
        messages=(), tools=(), policy=GenerationPolicyIR(max_output_tokens=64),
    )
    session.prepare(
        HistoryView(session.frontier, (
            LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR("Q"),)),
        )),
        request_shell=shell,
    )
    session.attach_native(accepted, native(session, "ACCEPTED_NATIVE"))
    after = HistoryCursor(
        session.frontier.history_epoch,
        session.frontier.block_sequence + 1,
        hashlib.sha256(b"fixture accepted span").hexdigest(),
    )
    receipt = HistoryCommitReceipt(
        accepted, AppendReceipt(session.frontier, after, 1), (), True,
    )
    session.observe_commit(receipt)
    return session, accepted, receipt


def high_fence_snapshot():
    session, _, _ = seeded()
    session.begin_round(key(session, "cancelled-new-owner", 7), requires_native=False)
    session.close_round()
    return session, {"projection": snapshot_projection(session)}


class ReviewC1C2(unittest.TestCase):
    # -- C1 ----------------------------------------------------------------

    def test_committed_attempt_cannot_be_reopened(self):
        """The committed ledger is a terminal tombstone for begin_round."""
        for fence in (2, 3):
            with self.subTest(fence=fence):
                session, accepted, _ = seeded()
                before = snapshot_projection(session)
                reused = replace(accepted, owner_fence=OwnerFence(fence))
                with self.assertRaises(ProjectionSessionError):
                    session.begin_round(reused, requires_native=True)
                self.assertEqual(snapshot_projection(session), before)

    def test_duplicate_dispatch_cancellation_cannot_delete_committed_native(self):
        for cancellation in ("close", "reject"):
            with self.subTest(cancellation=cancellation):
                session, accepted, _ = seeded()
                before = snapshot_projection(session)
                try:
                    session.begin_round(accepted, requires_native=True)
                except ProjectionSessionError:
                    self.assertEqual(snapshot_projection(session), before)
                    continue
                if cancellation == "close":
                    session.close_round()
                else:
                    session.reject_commit(accepted.attempt_id, "duplicate dispatch cancelled")
                self.assertEqual(snapshot_projection(session), before)
                self.assertIsNotNone(session.native_for(accepted.attempt_id))

    def test_reopened_attempt_cannot_export_draft_as_committed_native(self):
        session, accepted, _ = seeded()
        before = snapshot_projection(session)
        try:
            session.begin_round(accepted, requires_native=True)
            session.attach_native(accepted, native(session, "UNACCEPTED_REPLACEMENT"))
        except ProjectionSessionError:
            self.assertEqual(snapshot_projection(session), before)
            return
        # An ID's presence in the committed ledger is insufficient if the
        # record stored under that ID has subsequently been replaced.
        after = snapshot_projection(session)
        self.assertEqual(after["native_records"], before["native_records"])
        self.assertNotIn("UNACCEPTED_REPLACEMENT", json.dumps(after))

    def test_control_receipt_redelivery_stays_idempotent(self):
        session, _, receipt = seeded()
        before = snapshot_projection(session)
        session.observe_commit(receipt)
        self.assertEqual(snapshot_projection(session), before)
        session.begin_round(key(session, "genuinely-new", 3), requires_native=False)
        session.attach_native(key(session, "genuinely-new", 3), native(session, "DRAFT"))
        session.close_round()
        self.assertIsNotNone(session.native_for("accepted"))
        self.assertIsNone(session.native_for("genuinely-new"))

    # -- C2 ----------------------------------------------------------------

    def test_present_malformed_fence_is_not_legacy(self):
        invalid_values = (None, True, False, -1, 7.0, "7", [], {})
        for value in invalid_values:
            with self.subTest(value=repr(value)):
                source, saved = high_fence_snapshot()
                saved["projection"]["owner_fence"] = value
                target = EndpointProjectionSession(source.session_id)
                before = snapshot_projection(target)
                with self.assertRaises(ProjectionCheckpointError):
                    restore_projection(saved, l1_history_cursor=source.frontier, session=target)
                self.assertEqual(snapshot_projection(target), before)

    def test_control_legacy_absent_fence_uses_documented_fallback(self):
        source, saved = high_fence_snapshot()
        del saved["projection"]["owner_fence"]
        target = EndpointProjectionSession(source.session_id)
        self.assertTrue(restore_projection(
            saved, l1_history_cursor=source.frontier, session=target,
        ))
        with self.assertRaises(ProjectionSessionError):
            target.begin_round(key(target, "too-old", 1), requires_native=False)
        target.begin_round(key(target, "legacy-current", 2), requires_native=False)

    def test_control_valid_current_fence_survives_restore(self):
        source, saved = high_fence_snapshot()
        target = EndpointProjectionSession(source.session_id)
        self.assertTrue(restore_projection(
            saved, l1_history_cursor=source.frontier, session=target,
        ))
        with self.assertRaises(ProjectionSessionError):
            target.begin_round(key(target, "stale", 6), requires_native=False)
        target.begin_round(key(target, "current", 7), requires_native=False)


if __name__ == "__main__":
    unittest.main()
