"""Committed-attempt terminality and receipt replay regressions.

Duplicate dispatch and cancellation must not replace committed native content.
Standalone projection checkpoint-format tests were removed with that unused API."""
from __future__ import annotations

import hashlib
import json
import unittest
from tests.projection_state_assertions import committed_state
from dataclasses import replace

from pal.llm.continuation_policy import NativeCandidate
from pal.llm.ir import (
    GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole,
    TextPartIR, WireShape,
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


class ReviewC1C2(unittest.TestCase):
    # -- C1 ----------------------------------------------------------------

    def test_committed_attempt_cannot_be_reopened(self):
        """The committed ledger is a terminal tombstone for begin_round."""
        for fence in (2, 3):
            with self.subTest(fence=fence):
                session, accepted, _ = seeded()
                before = committed_state(session)
                reused = replace(accepted, owner_fence=OwnerFence(fence))
                with self.assertRaises(ProjectionSessionError):
                    session.begin_round(reused, requires_native=True)
                self.assertEqual(committed_state(session), before)

    def test_duplicate_dispatch_cancellation_cannot_delete_committed_native(self):
        for cancellation in ("close", "reject"):
            with self.subTest(cancellation=cancellation):
                session, accepted, _ = seeded()
                before = committed_state(session)
                try:
                    session.begin_round(accepted, requires_native=True)
                except ProjectionSessionError:
                    self.assertEqual(committed_state(session), before)
                    continue
                if cancellation == "close":
                    session.close_round()
                else:
                    session.reject_commit(accepted.attempt_id, "duplicate dispatch cancelled")
                self.assertEqual(committed_state(session), before)
                self.assertIsNotNone(session.native_for(accepted.attempt_id))

    def test_reopened_attempt_cannot_replace_committed_native(self):
        session, accepted, _ = seeded()
        before = committed_state(session)
        try:
            session.begin_round(accepted, requires_native=True)
            session.attach_native(accepted, native(session, "UNACCEPTED_REPLACEMENT"))
        except ProjectionSessionError:
            self.assertEqual(committed_state(session), before)
            return
        # An ID's presence in the committed ledger is insufficient if the
        # record stored under that ID has subsequently been replaced.
        after = committed_state(session)
        self.assertEqual(after, before)
        self.assertNotIn("UNACCEPTED_REPLACEMENT", json.dumps(after))

    def test_control_receipt_redelivery_stays_idempotent(self):
        session, _, receipt = seeded()
        before = committed_state(session)
        session.observe_commit(receipt)
        self.assertEqual(committed_state(session), before)
        session.begin_round(key(session, "genuinely-new", 3), requires_native=False)
        session.attach_native(key(session, "genuinely-new", 3), native(session, "DRAFT"))
        session.close_round()
        self.assertIsNotNone(session.native_for("accepted"))
        self.assertIsNone(session.native_for("genuinely-new"))


if __name__ == "__main__":
    unittest.main()
