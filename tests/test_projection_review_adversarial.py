"""Adversarial regression suite from the 2026-09-18 external branch review.

Source: ~/Documents/coding/pal_branch_review_2026-09-18/test_astra_projection_review.py
(review of 96b9140).  These tests assert the PLAN's intended invariants.
They are adapted to the repaired interfaces (materializing observe_commit,
binding-checked attach_native) WITHOUT weakening
the original assertions: full-prefix survival, native existence/identity,
and inventory consistency must all hold.

Per VALIDATION.md: synthetic signatures only test local preservation and
association; no provider calls, credentials, or real database are used.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import replace

from pal.llm.continuation_policy import (
    ContinuationDecisionKind,
    NativeCandidate,
    validate_candidate,
)
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR, WireShape
from pal.llm.projection_contracts import (
    AppendReceipt,
    AttemptKey,
    ClosedRound,
    EndpointBinding,
    HistoryCommitReceipt,
    HistoryCursor,
    LogicalSessionId,
    NativeContinuation,
    NativeContinuationKind,
    OwnerFence,
    PreparedRequest,
    ProjectionContractError,
    ToolCallRecord,
    ToolOutcome,
    ToolResultRecord,
)
from pal.llm.projection_session import (
    EndpointProjectionSession,
    HistoryView,
    ProjectionSessionError,
)


def binding(shape=WireShape.OPENAI_COMPLETION, suffix="1"):
    return EndpointBinding(
        endpoint_id=f"endpoint-{suffix}",
        model_id=f"model-{suffix}",
        wire_shape=shape,
        endpoint_spec_revision="rev-1",
        continuation_policy_version="policy-1",
        config_fingerprint=f"fp-{suffix}",
    )


def session(shape=WireShape.OPENAI_COMPLETION):
    s = EndpointProjectionSession(LogicalSessionId("review:resident"))
    s.bind(binding(shape))
    return s


def attempt(s, name="a1", fence=0):
    return AttemptKey(s.identity, OwnerFence(fence), name)


def user(text):
    return LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR(text),))


def assistant(text):
    return LLMMessageIR(role=MessageRole.ASSISTANT, parts=(TextPartIR(text),))


def receipt(s, key, *, native=False, calls=()):
    after = HistoryCursor(
        s.frontier.history_epoch,
        s.frontier.block_sequence + 1,
        "d" * 64,
    )
    return HistoryCommitReceipt(
        attempt=key,
        append=AppendReceipt(s.frontier, after, 1),
        closed_call_ids=tuple(calls),
        native_committed=native,
    )


def payload(request):
    return json.loads(request.payload_json)


def wire_texts(request):
    result = []
    body = payload(request)
    for message in body.get("messages", body.get("input", [])):
        content = message.get("content", "")
        if isinstance(content, str):
            result.append(content)
        elif isinstance(content, list):
            result.append("".join(str(b.get("text", "")) for b in content))
    return result


def seeded(fence=0):
    s = session()
    key = attempt(s, fence=fence)
    s.begin_round(key, requires_native=False)
    original = s.prepare(HistoryView(s.frontier, (user("old-question"), assistant("old-answer"))))
    proof = receipt(s, key)
    s.observe_commit(proof)
    return s, original, proof


class AstraProjectionReview(unittest.TestCase):


    def test_required_native_cannot_be_committed_by_boolean_claim(self):
        s = session()
        key = attempt(s)
        s.begin_round(key, requires_native=True)
        s.prepare(HistoryView(s.frontier, (user("question"),)))
        # No native record was attached. A Boolean is not a joint-commit proof.
        with self.assertRaises((ProjectionSessionError, ProjectionContractError)):
            s.observe_commit(receipt(s, key, native=True))

    def test_attached_native_is_actually_used_in_next_wire_request(self):
        s = session()
        key = attempt(s)
        s.begin_round(key, requires_native=True)
        s.prepare(HistoryView(s.frontier, (user("question"),)))
        s.attach_native(key, NativeCandidate(
            wire_shape=WireShape.OPENAI_COMPLETION,
            endpoint_id=s.binding.endpoint_id,
            model_id=s.binding.model_id,
            payload_json=json.dumps({"message": {
                "role": "assistant", "content": "answer",
                "reasoning_content": "SYNTHETIC_NATIVE_MARKER",
            }}),
            call_ids=(),
        ))
        s.observe_commit(receipt(s, key, native=True))
        s.begin_round(attempt(s, "a2"), requires_native=False)
        # The receipt has advanced past the accepted round. This view is the
        # strictly subsequent tail, as HistoryView's documented contract says.
        after = s.prepare(HistoryView(s.frontier, (user("next-question"),)))
        self.assertIn("SYNTHETIC_NATIVE_MARKER", after.payload_json)
        self.assertIn("answer", wire_texts(after))

    def test_closed_repaired_round_contributes_accepted_call_and_result(self):
        s = session(WireShape.OPENAI_RESPONSE)
        key = attempt(s)
        s.begin_round(key, requires_native=False)
        s.prepare(HistoryView(s.frontier, (user("check it"),)))
        # Runtime has accepted call-a/result-a, and removed unstarted call-b.
        repaired = ClosedRound(
            attempt=key,
            calls=(ToolCallRecord("call-a", "lookup", "{}"),),
            results=(ToolResultRecord("call-a", "42", ToolOutcome.SUCCESS),),
            continuation=NativeContinuation(NativeContinuationKind.ABSENT),
        )
        s.accept_repaired_round(repaired, cursor_after=HistoryCursor(0, 1, "d" * 64), block_count=1)
        s.begin_round(attempt(s, "a2"), requires_native=False)
        after = payload(s.prepare(HistoryView(s.frontier, (user("continue"),))))
        calls = [x for x in after["input"] if x.get("type") == "function_call"]
        results = [x for x in after["input"] if x.get("type") == "function_call_output"]
        self.assertEqual([x["call_id"] for x in calls], ["call-a"])
        # The codec renders outputs as [{type: input_text, text: ...}] blocks;
        # the assertion keeps the pairing and the exact body content.
        self.assertEqual(
            [(x["call_id"], x["output"][0]["text"]) for x in results],
            [("call-a", "42")],
        )

    def test_anthropic_prepare_keeps_system_preamble(self):
        s = session(WireShape.ANTHROPIC_MESSAGES)
        s.begin_round(attempt(s), requires_native=False)
        # The preamble belongs to the request shell (review F1/F2 follow-up):
        # the shell owns system/developer heads, the view owns the tail.
        shell_messages = (
            LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("SYSTEM_SENTINEL"),)),
        )
        from pal.llm.ir import GenerationPolicyIR, LLMRequestIR

        shell = LLMRequestIR(
            messages=shell_messages,
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=4096),
        )
        prepared = s.prepare(
            HistoryView(s.frontier, (user("hello"),)),
            request_shell=shell,
        )
        self.assertIn("system", payload(prepared))
        self.assertIn("SYSTEM_SENTINEL", prepared.payload_json)

    def test_prepared_request_is_not_just_a_checksum_for_pending_tool_calls(self):
        s = session()
        invalid = {"model": s.binding.model_id, "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "unanswered", "type": "function", "function": {
                    "name": "lookup", "arguments": "{}"}},
            ]},
        ]}
        with self.assertRaises((ProjectionSessionError, ProjectionContractError)):
            PreparedRequest.build(attempt(s), s.frontier, invalid)

    def test_committed_chunks_cannot_rewrite_already_prepared_prefix(self):
        s, first, _ = seeded()
        expected = payload(first)["messages"]
        try:
            s.chunks[0].items[0]["content"][0]["text"] = "CORRUPTED"
        except TypeError:
            return  # Deep immutability is an acceptable implementation.
        s.begin_round(attempt(s, "a2"), requires_native=False)
        next_request = s.prepare(HistoryView(s.frontier, (user("next"),)))
        self.assertEqual(payload(next_request)["messages"][:len(expected)], expected)

    def test_candidate_binding_must_match_attempt_binding(self):
        s = session()
        key = attempt(s)
        s.begin_round(key, requires_native=False)
        alien = NativeCandidate(
            wire_shape=WireShape.ANTHROPIC_MESSAGES,
            endpoint_id="OTHER_ENDPOINT", model_id="OTHER_MODEL",
            payload_json='{"content":[{"type":"text","text":"alien"}]}',
            call_ids=(),
        )
        with self.assertRaises((ProjectionSessionError, ProjectionContractError)):
            s.attach_native(key, alien)

    def test_cancelled_attempt_cannot_attach_late_native(self):
        s = session()
        old = attempt(s, "old", fence=0)
        s.begin_round(old, requires_native=False)
        s.close_round()
        s.begin_round(attempt(s, "new", fence=1), requires_native=False)
        stale = NativeCandidate(
            wire_shape=s.binding.wire_shape,
            endpoint_id=s.binding.endpoint_id, model_id=s.binding.model_id,
            payload_json='{"message":{"role":"assistant","content":"late"}}',
            call_ids=(),
        )
        with self.assertRaises((ProjectionSessionError, ProjectionContractError)):
            s.attach_native(old, stale)


    def test_empty_wire_call_inventory_does_not_match_nonempty_semantic_inventory(self):
        for shape, native in (
            (WireShape.ANTHROPIC_MESSAGES, {"content": []}),
            (WireShape.OPENAI_RESPONSE, {"output": []}),
            (WireShape.OPENAI_COMPLETION, {"message": {"role": "assistant", "content": ""}}),
        ):
            with self.subTest(shape=shape):
                candidate = NativeCandidate(shape, "endpoint-1", "model-1",
                                            json.dumps(native), ("call-a",))
                self.assertNotEqual(validate_candidate(candidate).kind,
                                    ContinuationDecisionKind.PRESERVED)


if __name__ == "__main__":
    unittest.main()
