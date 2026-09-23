"""Fourth external review (pal_projection_review_422c7ba) regressions.

H1: a refused commit must not install half of the pending state; a round
    whose items beyond the frontier are ALL user-role (Anthropic) is
    accepted semantically — coverage advances, items stay in the open
    tail, the round seals as an empty chunk.
H2: a no-native repaired round can carry its PRESERVED assistant text
    through ClosedRound.assistant_texts; native rounds refuse semantic
    texts (one representation per assistant contribution).
H3: a missing or explicit-null native arguments field is NOT an empty
    object; per-shape raw wire types are validated before semantic
    comparison (OpenAI: JSON string, Anthropic: JSON object).

Positive controls re-exercise the committed-head-system lifecycle
(cancel, receipt replay, binding switch) alongside the new paths.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from tests.projection_state_assertions import committed_state
from dataclasses import replace

from pal.llm.continuation_policy import NativeCandidate
from pal.llm.ir import (
    GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole,
    PromptRegionIR, TextPartIR, WireShape,
)
from pal.llm.projection_contracts import (
    AppendReceipt, AttemptKey, ClosedRound, EndpointBinding,
    HistoryCommitReceipt, HistoryCursor, LogicalSessionId,
    NativeContinuation, NativeContinuationKind, NativeMaterial,
    OwnerFence, ProjectionContractError, ToolCallRecord, ToolOutcome,
    ToolResultRecord,
)
from pal.llm.projection_contracts import (
    _native_arguments_match, _native_call_inventory_mismatch,
)
from pal.llm.projection_session import (
    ContinuationUnavailable, EndpointProjectionSession, HistoryView,
    ProjectionSessionError,
)
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
from pal.shared.json_values import thaw_json


ALL_SHAPES = (WireShape.OPENAI_COMPLETION, WireShape.OPENAI_RESPONSE, WireShape.ANTHROPIC_MESSAGES)


def msg(role: MessageRole, text: str) -> LLMMessageIR:
    return LLMMessageIR(role=role, parts=(TextPartIR(text),))


def developer(text: str) -> LLMMessageIR:
    return LLMMessageIR(
        role=MessageRole.DEVELOPER,
        parts=(TextPartIR(text),),
        prompt_region=PromptRegionIR.ACTIVE_HISTORY,
    )


def make_session(shape: WireShape) -> EndpointProjectionSession:
    s = EndpointProjectionSession(LogicalSessionId("review4:resident"))
    s.bind(EndpointBinding(
        endpoint_id="review4-endpoint", model_id="review4-model", wire_shape=shape,
        endpoint_spec_revision="spec-1", continuation_policy_version="policy-1",
        config_fingerprint="config-1",
    ))
    return s


def key(s, name: str, fence: int = 0) -> AttemptKey:
    return AttemptKey(s.identity, OwnerFence(fence), name)


def shell(*preamble: LLMMessageIR) -> LLMRequestIR:
    return LLMRequestIR(messages=tuple(preamble), tools=(),
                        policy=GenerationPolicyIR(max_output_tokens=128))


def receipt(s, k, calls=(), native=False) -> HistoryCommitReceipt:
    # Fixture cursor, not evidence of a real storage transaction.
    digest = hashlib.sha256((s.frontier.prefix_digest + k.attempt_id).encode()).hexdigest()
    after = HistoryCursor(s.frontier.history_epoch, s.frontier.block_sequence + 1, digest)
    return HistoryCommitReceipt(k, AppendReceipt(s.frontier, after, 1), tuple(calls), native)


def prepare(s, k, messages, request_shell):
    s.begin_round(k, requires_native=False)
    return s.prepare(HistoryView(s.frontier, tuple(messages)), request_shell=request_shell)


def body(prepared):
    return json.loads(prepared.payload_json)


def container_key(shape):
    return "input" if shape == WireShape.OPENAI_RESPONSE else "messages"


def text_occurrences(payload, sentinel: str) -> int:
    def visit(value):
        if isinstance(value, dict):
            n = int(value.get("text") == sentinel)
            if isinstance(value.get("content"), str):
                n += int(value["content"] == sentinel)
            return n + sum(visit(v) for v in value.values() if isinstance(v, (dict, list)))
        if isinstance(value, list):
            return sum(visit(v) for v in value)
        return 0
    return visit(payload)
def native_call(shape, args=..., text="NATIVE_ANSWER"):
    """A provider-native assistant turn with one call; args may be missing."""
    if shape == WireShape.OPENAI_COMPLETION:
        function = {"name": "lookup"}
        if args is not ...:
            function["arguments"] = args
        return {"message": {
            "role": "assistant", "content": text,
            "tool_calls": [{"id": "A", "type": "function", "function": function}],
        }}
    if shape == WireShape.OPENAI_RESPONSE:
        item = {"id": "fc-A", "type": "function_call", "call_id": "A", "name": "lookup"}
        if args is not ...:
            item["arguments"] = args
        return {"output": [item]}
    item = {"type": "tool_use", "id": "A", "name": "lookup"}
    if args is not ...:
        item["input"] = args
    return {"content": [{"type": "text", "text": text}, item]}


def valid_args(shape):
    return "{}" if shape in (WireShape.OPENAI_COMPLETION, WireShape.OPENAI_RESPONSE) else {}



def reference_encode(shape, messages):
    """Whole-history encoding of the same logical request."""
    codec = codec_for_shape(shape)
    context = ShapeContext(wire_shape=shape,
                           endpoint_id="review4-endpoint", model_id="review4-model")
    encoded = codec.encode(
        LLMRequestIR(messages=tuple(messages), tools=(),
                     policy=GenerationPolicyIR(max_output_tokens=128)),
        context,
    )
    return thaw_json(dict(encoded.payload))


def assert_incremental_matches_reference(test, shape, prepared, reference):
    wire = body(prepared)
    container = container_key(shape)
    test.assertEqual(wire[container], reference[container])
    test.assertEqual(wire.get("system"), reference.get("system"))


class FourthReviewRegressions(unittest.TestCase):
    # -- H1 ----------------------------------------------------------------

    def test_all_user_commit_is_accepted_with_open_tail(self):
        """Anthropic round with zero prefix-stable items: accept, hold, seal empty.

        The interrupted round had every unstarted call pruned and no preserved
        assistant contribution — the receipt is durable truth, so coverage
        advances, the user item stays in the session-owned open tail, and the
        next request re-feeds nothing.
        """
        s = make_session(WireShape.ANTHROPIC_MESSAGES)
        request_shell = shell(msg(MessageRole.SYSTEM, "P"))
        k = key(s, "input-only")
        s.begin_round(k, requires_native=False)
        s.prepare(HistoryView(s.frontier, (msg(MessageRole.USER, "Q_EXACTLY_ONCE"),)),
                  request_shell=request_shell)
        proof = receipt(s, k)
        s.observe_commit(proof)
        self.assertEqual(s.frontier, proof.append.after)
        self.assertEqual(s.chunks[-1].items, ())
        self.assertTrue(s._pending_wire_tail)
        # Next round supplies NO tail: the pending item carries Q alone.
        s.begin_round(key(s, "next"), requires_native=False)
        prepared = s.prepare(HistoryView(s.frontier, ()), request_shell=request_shell)
        self.assertEqual(text_occurrences(body(prepared), "Q_EXACTLY_ONCE"), 1)
        assert_incremental_matches_reference(
            self, WireShape.ANTHROPIC_MESSAGES, prepared,
            reference_encode(WireShape.ANTHROPIC_MESSAGES,
                             (msg(MessageRole.SYSTEM, "P"), msg(MessageRole.USER, "Q_EXACTLY_ONCE"))),
        )

    def test_retry_after_zero_freeze_commit_does_not_duplicate_input(self):
        """History-owner retry from the (advanced) frontier cannot double Q."""
        s = make_session(WireShape.ANTHROPIC_MESSAGES)
        request_shell = shell(msg(MessageRole.SYSTEM, "P"))
        k = key(s, "input-only")
        s.begin_round(k, requires_native=False)
        s.prepare(HistoryView(s.frontier, (msg(MessageRole.USER, "Q_EXACTLY_ONCE"),)),
                  request_shell=request_shell)
        s.observe_commit(receipt(s, k))
        s.begin_round(key(s, "retry"), requires_native=False)
        prepared = s.prepare(HistoryView(s.frontier, ()), request_shell=request_shell)
        self.assertEqual(text_occurrences(body(prepared), "Q_EXACTLY_ONCE"), 1)

    def test_zero_freeze_round_freezes_on_next_commit(self):
        shape = WireShape.ANTHROPIC_MESSAGES
        s = make_session(shape)
        request_shell = shell(msg(MessageRole.SYSTEM, "P"))
        k = key(s, "input-only")
        s.begin_round(k, requires_native=False)
        s.prepare(HistoryView(s.frontier, (msg(MessageRole.USER, "Q1"),)),
                  request_shell=request_shell)
        s.observe_commit(receipt(s, k))

        q2, a2 = msg(MessageRole.USER, "Q2"), msg(MessageRole.ASSISTANT, "A2")
        k2 = key(s, "second", fence=1)
        prepare(s, k2, (q2,), request_shell)
        s.observe_commit(receipt(s, k2), accepted_messages=(a2,))

        k3 = key(s, "third", fence=2)
        prepared = prepare(s, k3, (msg(MessageRole.USER, "Q3"),), request_shell)
        system = msg(MessageRole.SYSTEM, "P")
        assert_incremental_matches_reference(
            self, shape, prepared,
            reference_encode(shape, (system, msg(MessageRole.USER, "Q1"), q2, a2,
                                    msg(MessageRole.USER, "Q3"))),
        )
        self.assertEqual(text_occurrences(body(prepared), "Q1"), 1)

    def test_refused_commit_leaves_state_untouched_and_retries_deterministically(self):
        """The remaining refusal (nothing to seal) is atomic and replayable."""
        s = make_session(WireShape.ANTHROPIC_MESSAGES)
        request_shell = shell(msg(MessageRole.SYSTEM, "P"))
        k1 = key(s, "first")
        prepare(s, k1, (msg(MessageRole.USER, "Q1"),), request_shell)
        s.observe_commit(receipt(s, k1), accepted_messages=(msg(MessageRole.ASSISTANT, "A1"),))
        before = committed_state(s)

        k2 = key(s, "empty")
        s.begin_round(k2, requires_native=False)
        s.prepare(HistoryView(s.frontier, ()), request_shell=request_shell)
        for _ in range(2):
            with self.assertRaisesRegex(ProjectionSessionError, "nothing to seal"):
                s.observe_commit(receipt(s, k2))
            self.assertEqual(committed_state(s), before)
        s.close_round()

        k3 = key(s, "next")
        prepared = prepare(s, k3, (msg(MessageRole.USER, "Q2"),), request_shell)
        assert_incremental_matches_reference(
            self, WireShape.ANTHROPIC_MESSAGES, prepared,
            reference_encode(WireShape.ANTHROPIC_MESSAGES, (
                msg(MessageRole.SYSTEM, "P"), msg(MessageRole.USER, "Q1"),
                msg(MessageRole.ASSISTANT, "A1"), msg(MessageRole.USER, "Q2"),
            )),
        )

    # -- H2 ----------------------------------------------------------------

    def _text_repair_round(self, s, k, *, texts, calls=(), results=()):
        return ClosedRound(
            attempt=k,
            calls=tuple(calls),
            results=tuple(results),
            continuation=NativeContinuation(NativeContinuationKind.ABSENT),
            assistant_texts=tuple(texts),
        )

    def test_repaired_round_preserves_assistant_text_with_partial_calls(self):
        """SCENARIOS.md main case: KEEP_THIS_CONCLUSION + call A kept, B pruned."""
        for shape in ALL_SHAPES:
            with self.subTest(shape=shape.value):
                s = make_session(shape)
                request_shell = shell(msg(MessageRole.SYSTEM, "P"))
                k = key(s, "repair")
                s.begin_round(k, requires_native=False)
                s.prepare(HistoryView(s.frontier, (msg(MessageRole.USER, "Q"),)),
                          request_shell=request_shell)
                closed = self._text_repair_round(
                    s, k,
                    texts=("KEEP_THIS_CONCLUSION",),
                    calls=(ToolCallRecord("A", "lookup", '{"key": "A"}'),),
                    results=(ToolResultRecord("A", "RESULT_A", ToolOutcome.SUCCESS),),
                )
                s.accept_repaired_round(closed, cursor_after=HistoryCursor(0, 1, "d" * 64),
                                        block_count=1)
                # Continuation request: text once, A once, B nowhere — and the
                # assembled request equals the whole-history encoding of the
                # repaired conversation.
                assistant = LLMMessageIR(
                    role=MessageRole.ASSISTANT,
                    parts=(TextPartIR("KEEP_THIS_CONCLUSION"),
                           ToolCallIR("A", "lookup", {"key": "A"})),
                )
                tool = LLMMessageIR(
                    role=MessageRole.TOOL,
                    parts=(ToolResultIR("A", "lookup", "RESULT_A", ok=True),),
                )
                system = msg(MessageRole.SYSTEM, "P")
                k2 = key(s, "continuation")
                prepared = prepare(s, k2, (), request_shell)
                assert_incremental_matches_reference(
                    self, shape, prepared,
                    reference_encode(shape, (system, msg(MessageRole.USER, "Q"), assistant, tool)),
                )
                wire = body(prepared)
                self.assertEqual(text_occurrences(wire, "KEEP_THIS_CONCLUSION"), 1)
                self.assertEqual(text_occurrences(wire, "RESULT_A"), 1)
                self.assertNotIn("B", json.dumps(
                    [item for item in wire[container_key(shape)]
                     if isinstance(item, dict)]))

                # Commit the continuation and repeat the assertions on the next round.
                s.observe_commit(receipt(s, k2), accepted_messages=(msg(MessageRole.ASSISTANT, "A2"),))
                k3 = key(s, "next-round", fence=1)
                prepared = prepare(s, k3, (msg(MessageRole.USER, "Q2"),), request_shell)
                assert_incremental_matches_reference(
                    self, shape, prepared,
                    reference_encode(shape, (system, msg(MessageRole.USER, "Q"), assistant, tool,
                                            msg(MessageRole.ASSISTANT, "A2"),
                                            msg(MessageRole.USER, "Q2"))),
                )
                self.assertEqual(text_occurrences(body(prepared), "KEEP_THIS_CONCLUSION"), 1)

    def test_text_only_repair_survives_when_all_calls_are_pruned(self):
        for shape in ALL_SHAPES:
            with self.subTest(shape=shape.value):
                s = make_session(shape)
                request_shell = shell(msg(MessageRole.SYSTEM, "P"))
                k = key(s, "text-only")
                s.begin_round(k, requires_native=False)
                s.prepare(HistoryView(s.frontier, (msg(MessageRole.USER, "Q"),)),
                          request_shell=request_shell)
                closed = self._text_repair_round(s, k, texts=("KEEP_THIS_CONCLUSION",))
                s.accept_repaired_round(closed, cursor_after=HistoryCursor(0, 1, "d" * 64),
                                        block_count=1)
                k2 = key(s, "continuation")
                prepared = prepare(s, k2, (), request_shell)
                wire = body(prepared)
                self.assertEqual(text_occurrences(wire, "KEEP_THIS_CONCLUSION"), 1)
                self.assertEqual(text_occurrences(wire, "Q"), 1)

    def test_all_pruned_no_text_round_commits_without_duplication(self):
        """Zero-content repair: on Anthropic this is the H1 zero-freeze path."""
        for shape in ALL_SHAPES:
            with self.subTest(shape=shape.value):
                s = make_session(shape)
                request_shell = shell(msg(MessageRole.SYSTEM, "P"))
                k = key(s, "pruned")
                s.begin_round(k, requires_native=False)
                s.prepare(HistoryView(s.frontier, (msg(MessageRole.USER, "Q_EXACTLY_ONCE"),)),
                          request_shell=request_shell)
                closed = self._text_repair_round(s, k, texts=())
                s.accept_repaired_round(closed, cursor_after=HistoryCursor(0, 1, "d" * 64),
                                        block_count=1)
                k2 = key(s, "continuation")
                prepared = prepare(s, k2, (msg(MessageRole.USER, "Q2"),), request_shell)
                wire = body(prepared)
                self.assertEqual(text_occurrences(wire, "Q_EXACTLY_ONCE"), 1)
                self.assertEqual(text_occurrences(wire, "Q2"), 1)

    def test_native_round_refuses_semantic_assistant_texts(self):
        s = make_session(WireShape.OPENAI_RESPONSE)
        k = key(s, "native-with-text")
        s.begin_round(k, requires_native=True)
        s.prepare(HistoryView(s.frontier, (msg(MessageRole.USER, "Q"),)),
                  request_shell=shell(msg(MessageRole.SYSTEM, "P")))
        material = NativeMaterial(
            k, ("A",),
            json.dumps(native_call(WireShape.OPENAI_RESPONSE, "{}")),
        )
        with self.assertRaises(ProjectionContractError):
            ClosedRound(
                attempt=k,
                calls=(ToolCallRecord("A", "lookup", "{}"),),
                results=(ToolResultRecord("A", "42", ToolOutcome.SUCCESS),),
                continuation=NativeContinuation(NativeContinuationKind.REQUIRED, material),
                assistant_texts=("DUPLICATE_TEXT",),
            )

    # -- H3 ----------------------------------------------------------------

    def test_null_or_missing_native_arguments_are_not_empty_objects(self):
        for shape in ALL_SHAPES:
            for name, args in (("explicit-null", None), ("missing", ...)):
                with self.subTest(shape=shape.value, args=name):
                    s = make_session(shape)
                    k = key(s, name)
                    s.begin_round(k, requires_native=True)
                    s.prepare(HistoryView(s.frontier, (msg(MessageRole.USER, "Q"),)),
                              request_shell=shell(msg(MessageRole.SYSTEM, "P")))
                    material = NativeMaterial(k, ("A",), json.dumps(native_call(shape, args)))
                    with self.assertRaises((ProjectionContractError, ProjectionSessionError)):
                        closed = ClosedRound(
                            attempt=k,
                            calls=(ToolCallRecord("A", "lookup", "{}"),),
                            results=(ToolResultRecord("A", "42", ToolOutcome.SUCCESS),),
                            continuation=NativeContinuation(
                                NativeContinuationKind.REQUIRED, material),
                        )
                        s.accept_repaired_round(
                            closed, cursor_after=HistoryCursor(0, 1, "d" * 64), block_count=1)
                        s.begin_round(key(s, "next"), requires_native=False)
                        s.prepare(HistoryView(s.frontier, ()), request_shell=shell(
                            msg(MessageRole.SYSTEM, "P")))

    def test_valid_empty_argument_object_still_continues(self):
        for shape in ALL_SHAPES:
            with self.subTest(shape=shape.value):
                s = make_session(shape)
                k = key(s, "valid-empty")
                s.begin_round(k, requires_native=True)
                s.prepare(HistoryView(s.frontier, (msg(MessageRole.USER, "Q"),)),
                          request_shell=shell(msg(MessageRole.SYSTEM, "P")))
                material = NativeMaterial(
                    k, ("A",), json.dumps(native_call(shape, valid_args(shape))))
                closed = ClosedRound(
                    attempt=k,
                    calls=(ToolCallRecord("A", "lookup", "{}"),),
                    results=(ToolResultRecord("A", "42", ToolOutcome.SUCCESS),),
                    continuation=NativeContinuation(NativeContinuationKind.REQUIRED, material),
                )
                s.accept_repaired_round(closed, cursor_after=HistoryCursor(0, 1, "d" * 64),
                                        block_count=1)
                s.begin_round(key(s, "next"), requires_native=False)
                prepared = s.prepare(HistoryView(s.frontier, ()), request_shell=shell(
                    msg(MessageRole.SYSTEM, "P")))
                self.assertIn("42", prepared.payload_json)

    def test_native_argument_equality_semantics_pinned(self):
        # null / missing never coerce to an empty object
        self.assertFalse(_native_arguments_match(None, "{}"))
        self.assertFalse(_native_arguments_match("null", "{}"))
        # valid empty object, numeric echo, key order
        self.assertTrue(_native_arguments_match({}, "{}"))
        self.assertTrue(_native_arguments_match({"n": 1.0}, '{"n": 1}'))
        self.assertFalse(_native_arguments_match({"flag": True}, '{"flag": 1}'))
        self.assertTrue(_native_arguments_match({"a": 1, "b": 2}, '{"b": 2, "a": 1}'))

    def test_native_argument_wire_types_validated_per_shape(self):
        calls = (ToolCallRecord("A", "lookup", "{}"),)
        # OpenAI arguments must be a JSON string, not an embedded object.
        self.assertIn(
            "JSON string",
            _native_call_inventory_mismatch(
                "openai_response",
                json.dumps({"output": [{"type": "function_call", "call_id": "A",
                                       "name": "lookup", "arguments": {}}]}),
                calls),
        )
        self.assertIn(
            "JSON string",
            _native_call_inventory_mismatch(
                "openai_completion",
                json.dumps({"message": {"role": "assistant", "content": "",
                                        "tool_calls": [{"id": "A", "type": "function",
                                                        "function": {"name": "lookup",
                                                                     "arguments": {}}}]}},
                           ),
                calls),
        )
        # Anthropic input must be a JSON object, not a string.
        self.assertIn(
            "JSON object",
            _native_call_inventory_mismatch(
                "anthropic_messages",
                json.dumps({"content": [{"type": "tool_use", "id": "A", "name": "lookup",
                                         "input": "{}"}]}),
                calls),
        )
        # Missing / explicit-null report their own cause.
        self.assertIn(
            "no arguments field",
            _native_call_inventory_mismatch(
                "openai_response",
                json.dumps({"output": [{"type": "function_call", "call_id": "A",
                                       "name": "lookup"}]}),
                calls),
        )
        self.assertIn(
            "explicit null",
            _native_call_inventory_mismatch(
                "anthropic_messages",
                json.dumps({"content": [{"type": "tool_use", "id": "A", "name": "lookup",
                                         "input": None}]}),
                calls),
        )
        # The valid forms still pass cleanly.
        self.assertEqual(
            _native_call_inventory_mismatch(
                "openai_response",
                json.dumps({"output": [{"type": "function_call", "call_id": "A",
                                       "name": "lookup", "arguments": "{}"}]}),
                calls),
            "",
        )
        self.assertEqual(
            _native_call_inventory_mismatch(
                "anthropic_messages",
                json.dumps({"content": [{"type": "tool_use", "id": "A", "name": "lookup",
                                         "input": {}}]}),
                calls),
            "",
        )

    def test_attach_native_degrades_on_null_or_missing_arguments(self):
        """The continuation-policy layer rejects them without ClosedRound."""
        for name, block in (
            ("missing", {"type": "tool_use", "id": "A", "name": "lookup"}),
            ("explicit-null", {"type": "tool_use", "id": "A", "name": "lookup", "input": None}),
        ):
            with self.subTest(args=name):
                s = make_session(WireShape.ANTHROPIC_MESSAGES)
                k = key(s, name)
                s.begin_round(k, requires_native=True)
                with self.assertRaises(ContinuationUnavailable):
                    s.attach_native(k, NativeCandidate(
                        wire_shape=s.binding.wire_shape,
                        endpoint_id=s.binding.endpoint_id,
                        model_id=s.binding.model_id,
                        payload_json=json.dumps({"content": [block]}),
                        call_ids=("A",),
                    ))

    # -- positive controls: committed-head-system lifecycle ------------------

    def test_cancelled_head_system_does_not_leak_into_next_attempt(self):
        s = make_session(WireShape.ANTHROPIC_MESSAGES)
        k = key(s, "cancelled")
        s.begin_round(k, requires_native=False)
        s.prepare(HistoryView(s.frontier, (developer("CANCELLED_HEAD"),
                                           msg(MessageRole.USER, "Q"))),
                  request_shell=shell(msg(MessageRole.SYSTEM, "BASE_SYSTEM")))
        s.close_round()
        s.begin_round(key(s, "new-attempt"), requires_native=False)
        prepared = s.prepare(HistoryView(s.frontier, (msg(MessageRole.USER, "Q2"),)),
                             request_shell=shell(msg(MessageRole.SYSTEM, "BASE_SYSTEM")))
        self.assertNotIn("CANCELLED_HEAD", prepared.payload_json)

    def test_committed_head_survives_receipt_replay(self):
        s = make_session(WireShape.ANTHROPIC_MESSAGES)
        request_shell = shell(msg(MessageRole.SYSTEM, "BASE_SYSTEM"))
        k = key(s, "committed")
        s.begin_round(k, requires_native=False)
        s.prepare(HistoryView(s.frontier, (developer("COMMITTED_HEAD"),
                                           msg(MessageRole.USER, "Q"))),
                  request_shell=request_shell)
        proof = receipt(s, k)
        s.observe_commit(proof, accepted_messages=(msg(MessageRole.ASSISTANT, "A"),))
        s.observe_commit(proof)  # replayed receipt is a no-op

        s.begin_round(key(s, "continued", fence=1),
                                     requires_native=False)
        wire = body(s.prepare(
            HistoryView(s.frontier, (msg(MessageRole.USER, "Q2"),)),
            request_shell=request_shell))
        self.assertEqual(text_occurrences(wire, "COMMITTED_HEAD"), 1)
        self.assertEqual(text_occurrences(wire, "BASE_SYSTEM"), 1)

    def test_binding_switch_discards_old_hoisted_head(self):
        s = make_session(WireShape.ANTHROPIC_MESSAGES)
        k = key(s, "old-binding")
        s.begin_round(k, requires_native=False)
        s.prepare(HistoryView(s.frontier, (developer("OLD_BINDING_HEAD"),
                                           msg(MessageRole.USER, "Q"))),
                  request_shell=shell(msg(MessageRole.SYSTEM, "BASE_SYSTEM")))
        s.observe_commit(receipt(s, k),
                         accepted_messages=(msg(MessageRole.ASSISTANT, "A"),))
        s.bind(replace(s.binding, endpoint_id="new-endpoint", config_fingerprint="new-config"))
        self.assertEqual(s._committed_head_system, [])


if __name__ == "__main__":
    unittest.main()
