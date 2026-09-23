"""Third external review (pal_projection_review_4afee09) regressions.

G1: preamble must never enter the frozen conversation prefix.
G2: position-sensitive codecs must consume an accurate boundary context and
    the incremental request must keep tail-hoisted head content.
G3: native/semantic compatibility is id+name+arguments, and native plus IR
    cannot duplicate the same assistant contribution.
G4: the sendable gate validates an ordered event stream with role-appropriate
    placement.

The scenario matrix pins the stronger contract this round establishes: for
all three wire shapes the assembled incremental request equals the
whole-history codec encoding of the same conversation, container and
top-level system included, across multi-round commits, head/tail developer
insertions, and subsequent continuation requests.
"""
from __future__ import annotations

import hashlib
import json
import unittest

from pal.llm.continuation_policy import NativeCandidate
from pal.llm.ir import (
    GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole,
    PromptRegionIR, TextPartIR, WireShape,
)
from pal.llm.projection_contracts import (
    AppendReceipt, AttemptKey, ClosedRound, EndpointBinding,
    HistoryCommitReceipt, HistoryCursor, LogicalSessionId,
    NativeContinuation, NativeContinuationKind, NativeMaterial,
    OwnerFence, PreparedRequest, ProjectionContractError,
    ToolCallRecord, ToolOutcome, ToolResultRecord,
)
from pal.llm.projection_session import (
    EndpointProjectionSession, HistoryView, ProjectionSessionError,
)
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.shared.json_values import thaw_json


ALL_SHAPES = (WireShape.OPENAI_COMPLETION, WireShape.OPENAI_RESPONSE, WireShape.ANTHROPIC_MESSAGES)
OPENAI_SHAPES = (WireShape.OPENAI_COMPLETION, WireShape.OPENAI_RESPONSE)


def msg(role: MessageRole, text: str) -> LLMMessageIR:
    return LLMMessageIR(role=role, parts=(TextPartIR(text),))


def developer(text: str) -> LLMMessageIR:
    return LLMMessageIR(
        role=MessageRole.DEVELOPER,
        parts=(TextPartIR(text),),
        prompt_region=PromptRegionIR.ACTIVE_HISTORY,
    )


def make_session(shape: WireShape) -> EndpointProjectionSession:
    s = EndpointProjectionSession(LogicalSessionId("review3:resident"))
    s.bind(EndpointBinding(
        endpoint_id="review-endpoint", model_id="review-model", wire_shape=shape,
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
def assert_incremental_matches_reference(test, shape, prepared, reference):
    wire = body(prepared)
    container = container_key(shape)
    test.assertEqual(wire[container], reference[container])
    test.assertEqual(wire.get("system"), reference.get("system"))



def start_committed(s, request_shell):
    q1, a1 = msg(MessageRole.USER, "Q1"), msg(MessageRole.ASSISTANT, "A1")
    k = key(s, "first")
    first = prepare(s, k, (q1,), request_shell)
    s.observe_commit(receipt(s, k), accepted_messages=(a1,))
    return first, q1, a1


def reference_encode(shape, messages):
    """Whole-history encoding of the same logical request."""
    codec = codec_for_shape(shape)
    context = ShapeContext(wire_shape=shape,
                           endpoint_id="review-endpoint", model_id="review-model")
    encoded = codec.encode(
        LLMRequestIR(messages=tuple(messages), tools=(),
                     policy=GenerationPolicyIR(max_output_tokens=128)),
        context,
    )
    return thaw_json(dict(encoded.payload))


class ThirdReviewRegressions(unittest.TestCase):
    # -- G1 ----------------------------------------------------------------

    def test_openai_preamble_occurs_once_after_first_commit(self):
        for shape in OPENAI_SHAPES:
            with self.subTest(shape=shape.value):
                s = make_session(shape)
                request_shell = shell(msg(MessageRole.SYSTEM, "PREAMBLE_SENTINEL"))
                first, _, _ = start_committed(s, request_shell)
                second = prepare(s, key(s, "second"),
                                 (msg(MessageRole.USER, "Q2"),), request_shell)
                wire = body(second)
                self.assertEqual(text_occurrences(wire, "PREAMBLE_SENTINEL"), 1)
                first_items = body(first)[container_key(shape)]
                self.assertEqual(wire[container_key(shape)][:len(first_items)], first_items)

    def test_openai_second_commit_does_not_repeat_previous_answer(self):
        for shape in OPENAI_SHAPES:
            with self.subTest(shape=shape.value):
                s = make_session(shape)
                request_shell = shell(msg(MessageRole.SYSTEM, "PREAMBLE_SENTINEL"))
                start_committed(s, request_shell)
                k2 = key(s, "second")
                prepare(s, k2, (msg(MessageRole.USER, "Q2"),), request_shell)
                s.observe_commit(receipt(s, k2),
                                 accepted_messages=(msg(MessageRole.ASSISTANT, "A2"),))
                third = prepare(s, key(s, "third"),
                                (msg(MessageRole.USER, "Q3"),), request_shell)
                for text in ("Q1", "A1", "Q2", "A2", "Q3", "PREAMBLE_SENTINEL"):
                    self.assertEqual(text_occurrences(body(third), text), 1, text)


    # -- G2 ----------------------------------------------------------------

    def test_anthropic_late_developer_survives_tail_projection(self):
        s = make_session(WireShape.ANTHROPIC_MESSAGES)
        request_shell = shell()
        _, q1, a1 = start_committed(s, request_shell)
        guidance = developer("LATE_DEVELOPER_SENTINEL")
        q2 = msg(MessageRole.USER, "Q2")
        incremental = body(prepare(s, key(s, "second"), (guidance, q2), request_shell))
        reference = reference_encode(s.binding.wire_shape, (q1, a1, guidance, q2))
        self.assertEqual(text_occurrences(incremental, "LATE_DEVELOPER_SENTINEL"), 1)
        self.assertEqual(incremental["messages"], reference["messages"])

    def test_anthropic_late_system_degrades_identically(self):
        s = make_session(WireShape.ANTHROPIC_MESSAGES)
        request_shell = shell(msg(MessageRole.SYSTEM, "SHELL_SYSTEM"))
        _, q1, a1 = start_committed(s, request_shell)
        late_system = msg(MessageRole.SYSTEM, "LATE_SYSTEM_SENTINEL")
        q2 = msg(MessageRole.USER, "Q2")
        incremental = body(prepare(s, key(s, "second"), (late_system, q2), request_shell))
        reference = reference_encode(
            s.binding.wire_shape, (msg(MessageRole.SYSTEM, "SHELL_SYSTEM"), q1, a1, late_system, q2)
        )
        self.assertEqual(text_occurrences(incremental, "LATE_SYSTEM_SENTINEL"), 1)
        self.assertEqual(incremental["messages"], reference["messages"])
        self.assertEqual(incremental.get("system"), reference.get("system"))

    def test_anthropic_head_developer_hoists_into_merged_system(self):
        s = make_session(WireShape.ANTHROPIC_MESSAGES)
        request_shell = shell(msg(MessageRole.SYSTEM, "SHELL_SYSTEM"))
        d_head = developer("HEAD_DEVELOPER_SENTINEL")
        q1 = msg(MessageRole.USER, "Q1")
        incremental = prepare(s, key(s, "first"), (d_head, q1), request_shell)
        reference = reference_encode(
            s.binding.wire_shape,
            (msg(MessageRole.SYSTEM, "SHELL_SYSTEM"), d_head, q1),
        )
        self.assertEqual(text_occurrences(body(incremental), "HEAD_DEVELOPER_SENTINEL"), 1)
        assert_incremental_matches_reference(self, s.binding.wire_shape, incremental, reference)

    # -- G3 ----------------------------------------------------------------

    def test_repaired_native_must_match_call_name_and_arguments_not_only_id(self):
        for case, native_name, native_arguments in (
            ("different-arguments", "lookup", {"key": "OLD"}),
            ("different-name", "other_lookup", {"key": "NEW"}),
        ):
            with self.subTest(case=case):
                s = make_session(WireShape.OPENAI_RESPONSE)
                k = key(s, case)
                s.begin_round(k, requires_native=True)
                s.prepare(HistoryView(s.frontier, (msg(MessageRole.USER, "Q1"),)),
                          request_shell=shell())
                native = {"output": [
                    {"id": "reasoning-test", "type": "reasoning", "summary": [],
                     "encrypted_content": "SYNTHETIC_NOT_VALID_FOR_LIVE_API"},
                    {"id": "function-test", "type": "function_call", "call_id": "A",
                     "name": native_name, "arguments": json.dumps(native_arguments)},
                ]}
                material = NativeMaterial(k, ("A",), json.dumps(native))
                # Either construction or acceptance may reject the mismatch.
                with self.assertRaises((ProjectionContractError, ProjectionSessionError)):
                    closed = ClosedRound(
                        attempt=k,
                        calls=(ToolCallRecord("A", "lookup", '{"key":"NEW"}'),),
                        results=(ToolResultRecord("A", "42", ToolOutcome.SUCCESS),),
                        continuation=NativeContinuation(NativeContinuationKind.REQUIRED, material),
                    )
                    s.accept_repaired_round(closed,
                                            cursor_after=HistoryCursor(0, 1, "d" * 64),
                                            block_count=1)

    def test_native_and_ir_cannot_duplicate_the_same_plain_assistant_contribution(self):
        s = make_session(WireShape.OPENAI_COMPLETION)
        k = key(s, "plain-answer")
        s.begin_round(k, requires_native=False)
        s.prepare(HistoryView(s.frontier, (msg(MessageRole.USER, "Q1"),)),
                  request_shell=shell())
        s.attach_native(k, NativeCandidate(
            wire_shape=s.binding.wire_shape, endpoint_id=s.binding.endpoint_id,
            model_id=s.binding.model_id,
            payload_json=json.dumps({"message": {"role": "assistant", "content": "A1"}}),
            call_ids=(),
        ))
        with self.assertRaises((ProjectionContractError, ProjectionSessionError)):
            s.observe_commit(receipt(s, k, native=True),
                             accepted_messages=(msg(MessageRole.ASSISTANT, "A1"),))

    def test_native_arguments_numeric_forms_are_compatible_but_bool_is_not_a_number(self):
        s = make_session(WireShape.ANTHROPIC_MESSAGES)
        k_num = key(s, "numeric")
        native_num = {"content": [
            {"type": "tool_use", "id": "A", "name": "lookup", "input": {"n": 1.0}},
        ]}
        # 1 vs 1.0 is a numeric echo, not a different operation: accepted.
        ClosedRound(
            attempt=k_num,
            calls=(ToolCallRecord("A", "lookup", '{"n": 1}'),),
            results=(ToolResultRecord("A", "42", ToolOutcome.SUCCESS),),
            continuation=NativeContinuation(
                NativeContinuationKind.REQUIRED,
                NativeMaterial(k_num, ("A",), json.dumps(native_num)),
            ),
        )
        k_bool = key(s, "bool")
        native_bool = {"content": [
            {"type": "tool_use", "id": "B", "name": "lookup", "input": {"flag": True}},
        ]}
        with self.assertRaises(ProjectionContractError):
            ClosedRound(
                attempt=k_bool,
                calls=(ToolCallRecord("B", "lookup", '{"flag": 1}'),),
                results=(ToolResultRecord("B", "42", ToolOutcome.SUCCESS),),
                continuation=NativeContinuation(
                    NativeContinuationKind.REQUIRED,
                    NativeMaterial(k_bool, ("B",), json.dumps(native_bool)),
                ),
            )

    # -- G4 ----------------------------------------------------------------

    def test_anthropic_same_item_reversed_tool_blocks_are_rejected(self):
        s = make_session(WireShape.ANTHROPIC_MESSAGES)
        bad = {"model": "review-model", "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_result", "tool_use_id": "A", "content": "42"},
                {"type": "tool_use", "id": "A", "name": "lookup", "input": {}},
            ]},
        ]}
        with self.assertRaises(ProjectionContractError):
            PreparedRequest.build(key(s, "bad-block-order"), s.frontier, bad)

    def test_anthropic_tool_result_cannot_masquerade_as_assistant_content(self):
        s = make_session(WireShape.ANTHROPIC_MESSAGES)
        bad = {"model": "review-model", "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "A", "name": "lookup", "input": {}},
                {"type": "tool_result", "tool_use_id": "A", "content": "42"},
            ]},
        ]}
        with self.assertRaises(ProjectionContractError):
            PreparedRequest.build(key(s, "bad-role"), s.frontier, bad)

    def test_anthropic_tool_use_cannot_masquerade_as_user_content(self):
        s = make_session(WireShape.ANTHROPIC_MESSAGES)
        bad = {"model": "review-model", "messages": [
            {"role": "user", "content": [
                {"type": "tool_use", "id": "A", "name": "lookup", "input": {}},
            ]},
        ]}
        with self.assertRaises(ProjectionContractError):
            PreparedRequest.build(key(s, "bad-user-tool-use"), s.frontier, bad)

    def test_completion_tool_calls_outside_assistant_message_are_rejected(self):
        s = make_session(WireShape.OPENAI_COMPLETION)
        bad = {"model": "review-model", "messages": [
            {"role": "user", "content": "hi", "tool_calls": [
                {"id": "A", "type": "function",
                 "function": {"name": "lookup", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "A", "content": "42"},
        ]}
        with self.assertRaises(ProjectionContractError):
            PreparedRequest.build(key(s, "bad-tool-calls-role"), s.frontier, bad)


class ScenarioMatrix(unittest.TestCase):
    """One shared scenario across all three shapes (review's suggested gate).

    Round 1 inserts an ACTIVE_HISTORY developer at the tail head (request
    head), round 2 inserts one mid-tail, round 3 is plain.  Every assembled
    incremental request must EQUAL the whole-history codec encoding of the
    same logical conversation — container and top-level system included —
    including subsequent requests after multiple accepted rounds.
    """

    def test_matrix_incremental_equals_whole_history_across_shapes(self):
        for shape in ALL_SHAPES:
            with self.subTest(shape=shape.value):
                s = make_session(shape)
                request_shell = shell(msg(MessageRole.SYSTEM, "MATRIX_PREAMBLE"))
                d_head = developer("MATRIX_HEAD_DEV")
                d_late = developer("MATRIX_LATE_DEV")
                q1, a1 = msg(MessageRole.USER, "Q1"), msg(MessageRole.ASSISTANT, "A1")
                q2, a2 = msg(MessageRole.USER, "Q2"), msg(MessageRole.ASSISTANT, "A2")
                q3 = msg(MessageRole.USER, "Q3")
                system = msg(MessageRole.SYSTEM, "MATRIX_PREAMBLE")

                k1 = key(s, "r1")
                first = prepare(s, k1, (d_head, q1), request_shell)
                assert_incremental_matches_reference(
                    self, shape, first, reference_encode(shape, (system, d_head, q1))
                )
                s.observe_commit(receipt(s, k1), accepted_messages=(a1,))

                k2 = key(s, "second")
                second = prepare(s, k2, (d_late, q2), request_shell)
                assert_incremental_matches_reference(
                    self, shape, second,
                    reference_encode(shape, (system, d_head, q1, a1, d_late, q2)),
                )
                s.observe_commit(receipt(s, k2), accepted_messages=(a2,))

                third = prepare(s, key(s, "third"), (q3,), request_shell)
                assert_incremental_matches_reference(
                    self, shape, third,
                    reference_encode(
                        shape, (system, d_head, q1, a1, d_late, q2, a2, q3)
                    ),
                )
                # PREAMBLE/HEAD_DEV are covered by the exact-equality asserts
                # above (Completion legitimately merges them into one system
                # text, which exact-occurrence counting cannot see).
                for text in ("Q1", "A1", "Q2", "A2", "Q3"):
                    self.assertEqual(text_occurrences(body(third), text), 1, text)

    def test_matrix_multiple_commits_preserve_incremental_equality(self):
        for shape in ALL_SHAPES:
            with self.subTest(shape=shape.value):
                s = make_session(shape)
                request_shell = shell(msg(MessageRole.SYSTEM, "MATRIX_PREAMBLE"))
                d_head = developer("MATRIX_HEAD_DEV")
                q1, a1 = msg(MessageRole.USER, "Q1"), msg(MessageRole.ASSISTANT, "A1")
                q2, a2 = msg(MessageRole.USER, "Q2"), msg(MessageRole.ASSISTANT, "A2")
                system = msg(MessageRole.SYSTEM, "MATRIX_PREAMBLE")

                k1 = key(s, "r1")
                prepare(s, k1, (d_head, q1), request_shell)
                s.observe_commit(receipt(s, k1), accepted_messages=(a1,))
                k2 = key(s, "r2")
                prepare(s, k2, (q2,), request_shell)
                s.observe_commit(receipt(s, k2), accepted_messages=(a2,))

                q3 = msg(MessageRole.USER, "Q3")
                prepared = prepare(s, key(s, "next-round", 1),
                                   (q3,), request_shell)
                assert_incremental_matches_reference(
                    self, shape, prepared,
                    reference_encode(shape, (system, d_head, q1, a1, q2, a2, q3)),
                )
                # Content survives successive commits in every shape (substring level;
                # Completion merges preamble+head-dev into one system text).
                self.assertIn("MATRIX_PREAMBLE", prepared.payload_json)
                self.assertIn("MATRIX_HEAD_DEV", prepared.payload_json)


if __name__ == "__main__":
    unittest.main()
