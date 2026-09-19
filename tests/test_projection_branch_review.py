"""Whole-branch external review (pal_full_branch_review_f12730e) regressions.

B1: the session's encode entry points must use the SAME validated endpoint
    capability profile as the legacy full encode (bind-time capabilities,
    deeply frozen, shared by shell/tail/accepted encodes and checkpoint).
B2: cancelled/rejected rounds must not leave unaccepted native material in
    the authoritative store or a snapshot; committed native survives.
B3: restore refuses populated targets instead of promising a fresh lineage
    behind a False return; fresh legacy restore still works.
B4: the CURRENT owner fence is persisted separately from historical
    receipt SOURCE fences, so cancel+restore cannot re-authorize a stale
    worker; pre-B4 snapshots fall back to the highest committed source
    fence.

The B1 case injects capabilities through the new bind() channel per the
review's own instruction ("pass that same trusted context into the session
fixture; keep the request-field assertions unchanged").
"""
from __future__ import annotations

import hashlib
import json
import unittest

from pal.llm.continuation_policy import NativeCandidate
from pal.llm.ir import (
    GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole,
    TextPartIR, WireShape,
)
from pal.llm.projection_checkpoint import (
    PROJECTION_CHECKPOINT_SCHEMA_VERSION, ProjectionCheckpointError,
    restore_projection, snapshot_projection,
)
from pal.llm.projection_contracts import (
    AppendReceipt, AttemptKey, EndpointBinding, HistoryCommitReceipt,
    HistoryCursor, LogicalSessionId, OwnerFence, ProjectionContractError,
)
from pal.llm.projection_session import (
    EndpointProjectionSession, HistoryView, ProjectionSessionError,
)
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.shared.json_values import thaw_json


SHAPES = (
    WireShape.OPENAI_COMPLETION,
    WireShape.OPENAI_RESPONSE,
    WireShape.ANTHROPIC_MESSAGES,
)
CAPABILITIES = {"unsupported_request_parameters": ["temperature"]}


def message(role: MessageRole, text: str) -> LLMMessageIR:
    return LLMMessageIR(role=role, parts=(TextPartIR(text),))


def new_session(shape: WireShape = WireShape.OPENAI_COMPLETION):
    s = EndpointProjectionSession(LogicalSessionId("branch-review:resident"))
    fingerprint = hashlib.sha256(json.dumps(CAPABILITIES, sort_keys=True).encode()).hexdigest()
    s.bind(
        EndpointBinding(
            endpoint_id="review-endpoint", model_id="review-model", wire_shape=shape,
            endpoint_spec_revision="review-spec", continuation_policy_version="review-policy",
            config_fingerprint=fingerprint,
        ),
        # B1 fix channel: the validated capability profile rides with the
        # binding (review header authorizes this fixture injection; the
        # request-field assertions below are unchanged).
        capabilities=CAPABILITIES,
    )
    return s


def key(s, name: str, fence: int = 0):
    return AttemptKey(s.identity, OwnerFence(fence), name)


def shell(temperature=None):
    return LLMRequestIR(
        messages=(message(MessageRole.SYSTEM, "SYSTEM"),), tools=(),
        policy=GenerationPolicyIR(max_output_tokens=128, temperature=temperature),
    )


def receipt(s, k, native: bool = False):
    after = HistoryCursor(
        s.frontier.history_epoch, s.frontier.block_sequence + 1,
        hashlib.sha256((s.frontier.prefix_digest + k.attempt_id).encode()).hexdigest(),
    )
    return HistoryCommitReceipt(k, AppendReceipt(s.frontier, after, 1), (), native)


def native_text(s, text: str):
    return NativeCandidate(
        wire_shape=s.binding.wire_shape,
        endpoint_id=s.binding.endpoint_id, model_id=s.binding.model_id,
        payload_json=json.dumps({"message": {"role": "assistant", "content": text}}),
        call_ids=(),
    )


def seeded(fence: int = 2):
    s = new_session()
    k = key(s, "accepted", fence)
    s.begin_round(k, requires_native=False)
    s.prepare(HistoryView(s.frontier, (message(MessageRole.USER, "OLD_Q"),)),
              request_shell=shell())
    s.attach_native(k, native_text(s, "OLD_ACCEPTED_NATIVE"))
    s.observe_commit(receipt(s, k, native=True))
    return s


class WholeBranchReview(unittest.TestCase):
    # -- B1 ----------------------------------------------------------------

    def test_endpoint_capabilities_must_reach_incremental_codec(self):
        for shape in SHAPES:
            with self.subTest(shape=shape.value):
                s = new_session(shape)
                q = message(MessageRole.USER, "Q")
                real_context = ShapeContext(
                    wire_shape=shape, endpoint_id=s.binding.endpoint_id,
                    model_id=s.binding.model_id, capabilities=CAPABILITIES,
                )
                request_shell = shell(0.37)
                full = LLMRequestIR(
                    messages=(*request_shell.messages, q), tools=(),
                    policy=request_shell.policy,
                )
                reference = thaw_json(codec_for_shape(shape).encode(full, real_context).payload)
                self.assertNotIn("temperature", reference)
                k = key(s, "with-endpoint-capabilities")
                s.begin_round(k, requires_native=False)
                wire = json.loads(s.prepare(
                    HistoryView(s.frontier, (q,)), request_shell=request_shell,
                ).payload_json)
                # An endpoint fingerprint is not the actual capability data.
                self.assertNotIn("temperature", wire)

    def test_unrestricted_endpoint_keeps_temperature(self):
        """Control: a normal endpoint (no restriction) still emits it."""
        for shape in SHAPES:
            with self.subTest(shape=shape.value):
                s = EndpointProjectionSession(LogicalSessionId("branch-review:plain"))
                s.bind(EndpointBinding(
                    endpoint_id="review-endpoint", model_id="review-model",
                    wire_shape=shape, endpoint_spec_revision="review-spec",
                    continuation_policy_version="review-policy",
                    config_fingerprint="plain",
                ))
                k = key(s, "plain")
                s.begin_round(k, requires_native=False)
                wire = json.loads(s.prepare(
                    HistoryView(s.frontier, (message(MessageRole.USER, "Q"),)),
                    request_shell=shell(0.37),
                ).payload_json)
                self.assertEqual(wire.get("temperature"), 0.37)

    def test_capabilities_are_frozen_against_caller_mutation(self):
        caps = {"unsupported_request_parameters": ["temperature"]}
        s = EndpointProjectionSession(LogicalSessionId("branch-review:frozen"))
        s.bind(EndpointBinding(
            endpoint_id="e", model_id="m", wire_shape=WireShape.OPENAI_COMPLETION,
            endpoint_spec_revision="1", continuation_policy_version="1",
            config_fingerprint="c",
        ), capabilities=caps)
        caps["unsupported_request_parameters"].append("top_p")
        caps["unsupported_request_parameters"] = []
        k = key(s, "frozen")
        s.begin_round(k, requires_native=False)
        wire = json.loads(s.prepare(
            HistoryView(s.frontier, (message(MessageRole.USER, "Q"),)),
            request_shell=shell(0.37),
        ).payload_json)
        self.assertNotIn("temperature", wire)

    def test_bind_rejects_non_mapping_capabilities(self):
        s = EndpointProjectionSession(LogicalSessionId("branch-review:bad"))
        with self.assertRaises(ProjectionSessionError):
            s.bind(EndpointBinding(
                endpoint_id="e", model_id="m", wire_shape=WireShape.OPENAI_COMPLETION,
                endpoint_spec_revision="1", continuation_policy_version="1",
                config_fingerprint="c",
            ), capabilities=["not", "a", "mapping"])

    def test_capabilities_survive_checkpoint_restore(self):
        s = new_session()
        k = key(s, "r1")
        s.begin_round(k, requires_native=False)
        s.prepare(HistoryView(s.frontier, (message(MessageRole.USER, "Q"),)),
                  request_shell=shell())
        s.observe_commit(receipt(s, k))
        successor = EndpointProjectionSession(s.session_id)
        self.assertTrue(restore_projection(
            {"projection": snapshot_projection(s)},
            l1_history_cursor=s.frontier, session=successor,
        ))
        k2 = key(successor, "after-restart", fence=1)
        successor.begin_round(k2, requires_native=False)
        wire = json.loads(successor.prepare(
            HistoryView(successor.frontier, (message(MessageRole.USER, "Q2"),)),
            request_shell=shell(0.37),
        ).payload_json)
        self.assertNotIn("temperature", wire)

    # -- B2 ----------------------------------------------------------------

    def test_cancelled_native_is_not_kept_as_authoritative_state(self):
        for cancellation in ("close", "reject"):
            with self.subTest(cancellation=cancellation):
                s = new_session()
                k = key(s, "unaccepted")
                s.begin_round(k, requires_native=False)
                s.attach_native(k, native_text(s, "UNACCEPTED_NATIVE"))
                if cancellation == "close":
                    s.close_round()
                else:
                    s.reject_commit(k.attempt_id, "semantic acceptance rejected")
                self.assertIsNone(s.native_for(k.attempt_id))
                saved = snapshot_projection(s)
                self.assertNotIn(k.attempt_id,
                                 [r["attempt_id"] for r in saved["native_records"]])
                self.assertNotIn("UNACCEPTED_NATIVE", json.dumps(saved))

    def test_cancel_cleanup_does_not_remove_committed_native(self):
        s = seeded()
        k = key(s, "cancelled-later", 3)
        s.begin_round(k, requires_native=False)
        s.attach_native(k, native_text(s, "CANCELLED_LATER"))
        s.close_round()
        self.assertIsNotNone(s.native_for("accepted"))
        self.assertIsNone(s.native_for("cancelled-later"))
        saved = snapshot_projection(s)
        self.assertEqual([r["attempt_id"] for r in saved["native_records"]], ["accepted"])

    def test_late_attach_after_cancel_is_still_rejected(self):
        s = new_session()
        k = key(s, "cancelled")
        s.begin_round(k, requires_native=False)
        s.close_round()
        with self.assertRaises(ProjectionSessionError):
            s.attach_native(k, native_text(s, "LATE_NATIVE"))

    # -- B3 ----------------------------------------------------------------

    def test_legacy_restore_into_populated_target_clears_or_refuses(self):
        payloads = (
            {},
            {"projection": {"schema_version": PROJECTION_CHECKPOINT_SCHEMA_VERSION,
                            "bound": False}},
        )
        for source in payloads:
            with self.subTest(source=source):
                s = seeded()
                before = snapshot_projection(s)
                try:
                    restored = restore_projection(
                        source, l1_history_cursor=HistoryCursor.initial(), session=s,
                    )
                except (ProjectionCheckpointError, ProjectionSessionError, ProjectionContractError):
                    # An explicitly fresh-target-only API is also coherent:
                    # it must reject without modifying the existing owner.
                    self.assertEqual(snapshot_projection(s), before)
                    continue
                self.assertFalse(restored)
                self.assertIsNone(s.identity)
                self.assertIsNone(s.binding)
                self.assertEqual(s.frontier, HistoryCursor.initial())
                self.assertEqual(s.chunks, ())
                self.assertEqual(s.native_by_attempt, {})
                self.assertEqual(s._pending_wire_tail, [])
                self.assertEqual(s._committed_head_system, [])

    def test_bound_restore_into_populated_target_is_refused_too(self):
        """The same pristine rule covers the bound path (no silent merges)."""
        donor = seeded()
        payload = {"projection": snapshot_projection(donor)}
        target = seeded()  # a DIFFERENT populated session
        before = snapshot_projection(target)
        with self.assertRaises(ProjectionCheckpointError):
            restore_projection(payload, l1_history_cursor=donor.frontier, session=target)
        self.assertEqual(snapshot_projection(target), before)

    def test_fresh_legacy_restore_is_still_supported(self):
        s = EndpointProjectionSession(LogicalSessionId("fresh"))
        self.assertFalse(restore_projection({}, l1_history_cursor=HistoryCursor.initial(),
                                             session=s))
        self.assertIsNone(s.identity)
        self.assertEqual(s.chunks, ())

    # -- B4 ----------------------------------------------------------------

    def test_current_owner_fence_does_not_roll_back_after_cancel_and_restore(self):
        s = seeded(fence=2)
        newer = key(s, "new-owner-cancelled", 7)
        s.begin_round(newer, requires_native=False)
        s.close_round()  # safe point, no unknown tool effects
        snapshot = {"projection": snapshot_projection(s)}
        restored = EndpointProjectionSession(s.session_id)
        self.assertTrue(restore_projection(snapshot, l1_history_cursor=s.frontier,
                                           session=restored))
        # A fence below 7 was already stale before this snapshot. Restoring
        # historical receipt.source_fence=2 must not authorize it again.
        with self.assertRaises(ProjectionSessionError):
            restored.begin_round(key(restored, "stale-owner", 6), requires_native=False)
        # The legitimate current owner still works.
        restored.begin_round(key(restored, "current-owner", 7), requires_native=False)

    def test_pre_b4_snapshot_without_owner_fence_falls_back_to_source_fences(self):
        """Snapshots written before B4 lack the field; fallback is documented."""
        s = seeded(fence=2)
        snapshot = {"projection": snapshot_projection(s)}
        del snapshot["projection"]["owner_fence"]
        restored = EndpointProjectionSession(s.session_id)
        self.assertTrue(restore_projection(snapshot, l1_history_cursor=s.frontier,
                                           session=restored))
        # Fallback: highest committed SOURCE fence (2). A stale fence below
        # it is refused; the pre-B4 semantics had no better reconstruction.
        with self.assertRaises(ProjectionSessionError):
            restored.begin_round(key(restored, "stale", 1), requires_native=False)
        restored.begin_round(key(restored, "ok", 2), requires_native=False)


if __name__ == "__main__":
    unittest.main()
