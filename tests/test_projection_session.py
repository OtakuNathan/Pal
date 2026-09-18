"""P3: session-level projection owner tests.

Gate scenarios (PLAN P3): append-proof refusal (same length is not proof),
late/conflicting receipts, idempotent commit replay, endpoint-switch lineage
destruction, explicit ContinuationUnavailable for degraded required-native,
incremental-equals-full-rebuild, and the anthropic trailing-user trim
(source-verified: _append_message merges adjacent same-role messages).
"""
from __future__ import annotations

import unittest

from pal.llm.ir import (
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    MessageRole,
    MessageState,
    TextPartIR,
    WireShape,
)
from pal.llm.projection_contracts import (
    AppendReceipt,
    AttemptKey,
    EndpointBinding,
    HistoryCommitReceipt,
    HistoryCursor,
    LogicalSessionId,
    OwnerFence,
    ProjectionIdentity,
)
from pal.llm.projection_session import (
    ContinuationUnavailable,
    EndpointProjectionSession,
    HistoryView,
    ProjectionSessionError,
)
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext


def _binding(shape: WireShape = WireShape.OPENAI_COMPLETION, suffix: str = "1") -> EndpointBinding:
    return EndpointBinding(
        endpoint_id=f"endpoint-{suffix}",
        model_id=f"model-{suffix}",
        wire_shape=shape,
        endpoint_spec_revision="rev-1",
        continuation_policy_version="policy-1",
        config_fingerprint=f"fp-{suffix}",
    )


def _attempt(session: EndpointProjectionSession, attempt_id: str) -> AttemptKey:
    return AttemptKey(
        identity=session.identity,  # type: ignore[arg-type]
        owner_fence=OwnerFence(0),
        attempt_id=attempt_id,
    )


def _user(text: str) -> LLMMessageIR:
    return LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR(text),))


def _assistant(text: str) -> LLMMessageIR:
    return LLMMessageIR(
        role=MessageRole.ASSISTANT,
        parts=(TextPartIR(text),),
        state=MessageState.COMPLETE,
    )


def _cursor(seq: int, digest: str) -> HistoryCursor:
    return HistoryCursor(history_epoch=0, block_sequence=seq, prefix_digest=digest)


def _receipt(attempt: AttemptKey, before: HistoryCursor, after: HistoryCursor, blocks: int = 1) -> HistoryCommitReceipt:
    return HistoryCommitReceipt(
        attempt=attempt,
        append=AppendReceipt(before=before, after=after, block_count=blocks),
        closed_call_ids=(),
        native_committed=False,
    )


def _items(request) -> list[dict]:
    import json

    return json.loads(request.payload_json)["messages"]


def _full_encode(shape: WireShape, binding: EndpointBinding, messages) -> list[dict]:
    codec = codec_for_shape(shape)
    context = ShapeContext(
        wire_shape=shape, endpoint_id=binding.endpoint_id, model_id=binding.model_id
    )
    request = LLMRequestIR(
        messages=tuple(messages), tools=(), policy=GenerationPolicyIR(max_output_tokens=4096)
    )
    return list(codec.encode(request, context).payload["messages"])


class IncrementalProjectionTests(unittest.TestCase):
    def test_incremental_prepare_matches_full_rebuild(self) -> None:
        session = EndpointProjectionSession(LogicalSessionId("pal:resident"))
        session.bind(_binding())
        first_round = (_user("first question"), _assistant("first answer"))
        second_round = (_user("second question"), _assistant("second answer"))

        session.begin_round(_attempt(session, "a1"), requires_native=False)
        request = session.prepare(HistoryView(cursor=HistoryCursor.initial(), messages=first_round))
        first_items = _items(request)

        after_first = _cursor(1, "d" * 64)
        session.observe_commit(_receipt(_attempt(session, "a1"), HistoryCursor.initial(), after_first))

        session.begin_round(_attempt(session, "a2"), requires_native=False)
        request_two = session.prepare(
            HistoryView(cursor=after_first, messages=second_round)
        )
        incremental_items = _items(request_two)

        full = _full_encode(
            WireShape.OPENAI_COMPLETION, _binding(), first_round + second_round
        )
        self.assertEqual(incremental_items, full)
        # The frozen chunk is a real prefix of both.  Public chunk items are
        # deep-frozen snapshots (review R7); thaw for value comparison.
        from pal.shared.json_values import thaw_json

        chunk_items = thaw_json([item for chunk in session.chunks for item in chunk.items])
        self.assertEqual(first_items, chunk_items)
        self.assertEqual(incremental_items[: len(chunk_items)], chunk_items)

    def test_same_length_cursor_is_not_an_append_proof(self) -> None:
        session = EndpointProjectionSession(LogicalSessionId("pal:resident"))
        session.bind(_binding())
        session.begin_round(_attempt(session, "a1"), requires_native=False)
        lookalike = _cursor(0, "f" * 64)  # same length, different digest
        with self.assertRaisesRegex(ProjectionSessionError, "append proof"):
            session.prepare(HistoryView(cursor=lookalike, messages=(_user("x"),)))

    def test_prepare_requires_open_round_and_binding(self) -> None:
        session = EndpointProjectionSession(LogicalSessionId("pal:resident"))
        with self.assertRaises(ProjectionSessionError):
            session.prepare(HistoryView(cursor=HistoryCursor.initial(), messages=()))
        session.bind(_binding())
        with self.assertRaises(ProjectionSessionError):
            session.prepare(HistoryView(cursor=HistoryCursor.initial(), messages=()))


class CommitReceiptTests(unittest.TestCase):
    def _committed_session(self):
        session = EndpointProjectionSession(LogicalSessionId("s"))
        session.bind(_binding())
        session.begin_round(_attempt(session, "a1"), requires_native=False)
        session.prepare(
            HistoryView(cursor=HistoryCursor.initial(), messages=(_user("q"), _assistant("a")))
        )
        receipt = _receipt(
            _attempt(session, "a1"), HistoryCursor.initial(), _cursor(1, "d" * 64)
        )
        session.observe_commit(receipt)
        return session, receipt

    def test_idempotent_replay_adds_no_duplicate_chunk(self) -> None:
        session, receipt = self._committed_session()
        self.assertEqual(len(session.chunks), 1)
        session.observe_commit(receipt)  # replay after the round closed
        self.assertEqual(len(session.chunks), 1)

    def test_conflicting_receipt_for_same_attempt_rejected(self) -> None:
        session, receipt = self._committed_session()
        conflicting = _receipt(
            receipt.attempt, HistoryCursor.initial(), _cursor(1, "e" * 64)
        )
        with self.assertRaisesRegex(ProjectionSessionError, "conflicting"):
            session.observe_commit(conflicting)

    def test_late_receipt_for_foreign_attempt_rejected(self) -> None:
        session, _ = self._committed_session()
        session.begin_round(_attempt(session, "a2"), requires_native=False)
        late = _receipt(
            _attempt(session, "ghost"), HistoryCursor.initial(), _cursor(1, "x" * 64)
        )
        with self.assertRaisesRegex(ProjectionSessionError, "does not match"):
            session.observe_commit(late)
        self.assertEqual(session.frontier, _cursor(1, "d" * 64))

    def test_commit_without_prepared_items_rejected(self) -> None:
        session = EndpointProjectionSession(LogicalSessionId("s"))
        session.bind(_binding())
        session.begin_round(_attempt(session, "a1"), requires_native=False)
        with self.assertRaisesRegex(ProjectionSessionError, "nothing to seal"):
            session.observe_commit(
                _receipt(
                    _attempt(session, "a1"), HistoryCursor.initial(), _cursor(1, "d" * 64)
                )
            )


class EndpointSwitchTests(unittest.TestCase):
    def test_switch_destroys_lineage_and_forces_full_view(self) -> None:
        session = EndpointProjectionSession(LogicalSessionId("s"))
        first = _binding(suffix="1")
        session.bind(first)
        session.begin_round(_attempt(session, "a1"), requires_native=False)
        session.prepare(
            HistoryView(cursor=HistoryCursor.initial(), messages=(_user("q"), _assistant("a")))
        )
        session.observe_commit(
            _receipt(_attempt(session, "a1"), HistoryCursor.initial(), _cursor(1, "d" * 64))
        )
        self.assertEqual(len(session.chunks), 1)

        session.bind(_binding(suffix="2"))  # endpoint switch
        self.assertEqual(session.chunks, ())
        self.assertEqual(session.native_by_attempt, {})
        self.assertEqual(session.identity.projection_generation, 1)
        self.assertEqual(session.frontier, HistoryCursor.initial())

        # Full view rebuild from scratch on the new binding.
        session.begin_round(_attempt(session, "b1"), requires_native=False)
        request = session.prepare(
            HistoryView(cursor=HistoryCursor.initial(), messages=(_user("q"), _assistant("a")))
        )
        rebuilt = _items(request)
        full = _full_encode(WireShape.OPENAI_COMPLETION, _binding(suffix="2"), (_user("q"), _assistant("a")))
        self.assertEqual(rebuilt, full)


class NativeGateTests(unittest.TestCase):
    def test_degraded_required_native_raises_continuation_unavailable(self) -> None:
        from pal.llm.continuation_policy import NativeCandidate

        session = EndpointProjectionSession(LogicalSessionId("s"))
        session.bind(_binding(WireShape.ANTHROPIC_MESSAGES))
        attempt = _attempt(session, "a1")
        session.begin_round(attempt, requires_native=True)
        # Thinking block without a signature -> Degraded by contract.
        candidate = NativeCandidate(
            wire_shape=WireShape.ANTHROPIC_MESSAGES,
            endpoint_id="endpoint-1",
            model_id="model-1",
            payload_json=(
                '{"content": [{"signature": "", "thinking": "private", "type": "thinking"},'
                ' {"text": "answer", "type": "text"}]}'
            ),
            call_ids=(),
        )
        with self.assertRaises(ContinuationUnavailable):
            session.attach_native(attempt, candidate)


class AnthropicFreezeBoundaryTests(unittest.TestCase):
    def test_trailing_user_items_are_trimmed_from_the_chunk(self) -> None:
        session = EndpointProjectionSession(LogicalSessionId("s"))
        session.bind(_binding(WireShape.ANTHROPIC_MESSAGES))
        round_messages = (
            _user("question"),
            _assistant("checking"),
            LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR("tool-result-body"),)),
        )
        session.begin_round(_attempt(session, "a1"), requires_native=False)
        request = session.prepare(
            HistoryView(cursor=HistoryCursor.initial(), messages=round_messages)
        )
        items = _items(request)
        # Encoder merged the adjacent user messages into one wire message
        # (source-verified _append_message merge); the merged tail is still
        # role=user and must not be frozen.
        self.assertEqual(items[-1]["role"], "user")

        session.observe_commit(
            _receipt(_attempt(session, "a1"), HistoryCursor.initial(), _cursor(1, "d" * 64))
        )
        chunk_items = [item for chunk in session.chunks for item in chunk.items]
        self.assertTrue(chunk_items)
        self.assertNotEqual(
            chunk_items[-1].get("role"), "user", "trailing user item was frozen"
        )
        # The unfrozen user content stays session-owned (pending wire tail):
        # the next prepare re-injects it automatically — callers never
        # re-supply already-committed results by hand (review F2).
        session.begin_round(_attempt(session, "a2"), requires_native=False)
        tail = (_user("next question"),)
        request_two = session.prepare(HistoryView(cursor=_cursor(1, "d" * 64), messages=tail))
        assembled = _items(request_two)
        full = _full_encode(
            WireShape.ANTHROPIC_MESSAGES,
            _binding(WireShape.ANTHROPIC_MESSAGES),
            (_user("question"), _assistant("checking"))
            + (
                LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR("tool-result-body"),)),
                _user("next question"),
            ),
        )
        self.assertEqual(assembled, full)


if __name__ == "__main__":
    unittest.main()
