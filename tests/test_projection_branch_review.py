"""Endpoint capability consistency and native cancellation regressions.

All encode entry points share the bound capability profile. Cancelling a draft
removes only its unaccepted native material, preserving committed contributions."""
from __future__ import annotations

import hashlib
import json
import unittest

from pal.llm.continuation_policy import NativeCandidate
from pal.llm.ir import (
    GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole,
    TextPartIR, WireShape,
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
                self.assertNotIn(k.attempt_id, s.native_by_attempt)
                self.assertNotIn("UNACCEPTED_NATIVE", repr(s.chunks))

    def test_cancel_cleanup_does_not_remove_committed_native(self):
        s = seeded()
        k = key(s, "cancelled-later", 3)
        s.begin_round(k, requires_native=False)
        s.attach_native(k, native_text(s, "CANCELLED_LATER"))
        s.close_round()
        self.assertIsNotNone(s.native_for("accepted"))
        self.assertIsNone(s.native_for("cancelled-later"))
        self.assertEqual(list(s.native_by_attempt), ["accepted"])

    def test_late_attach_after_cancel_is_still_rejected(self):
        s = new_session()
        k = key(s, "cancelled")
        s.begin_round(k, requires_native=False)
        s.close_round()
        with self.assertRaises(ProjectionSessionError):
            s.attach_native(k, native_text(s, "LATE_NATIVE"))


if __name__ == "__main__":
    unittest.main()
