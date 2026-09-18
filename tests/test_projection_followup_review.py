"""Follow-up adversarial regression suite from the 2026-09-18 review of ae04397.

Source: ~/Documents/coding/pal_branch_review_2026-09-18/pal_projection_review_ae04397/
test_projection_followup_review.py.  The ten specifications assert intended
invariants (F1-F5); they are adapted to the repaired interfaces WITHOUT
weakening assertions.  All native values are visibly synthetic; no network,
credentials, or production state.
"""
from __future__ import annotations

import hashlib
import json
import unittest
from typing import Any

from pal.llm.continuation_policy import NativeCandidate  # noqa: F401  (API surface pin)
from pal.llm.ir import (
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    MessageRole,
    PromptRegionIR,
    TextPartIR,
    WireShape,
)
from pal.llm.projection_checkpoint import restore_projection, snapshot_projection
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
    NativeMaterial,
    OwnerFence,
    PreparedRequest,
    ProjectionContractError,
    ToolCallRecord,
    ToolOutcome,
    ToolResultRecord,
)
from pal.llm.projection_session import EndpointProjectionSession, HistoryView
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR


def new_session(shape: WireShape = WireShape.OPENAI_COMPLETION) -> EndpointProjectionSession:
    result = EndpointProjectionSession(LogicalSessionId("review2:resident"))
    result.bind(EndpointBinding(
        endpoint_id="test-endpoint",
        model_id="test-model",
        wire_shape=shape,
        endpoint_spec_revision="spec-1",
        continuation_policy_version="policy-1",
        config_fingerprint="test-config",
    ))
    return result


def key_for(s: EndpointProjectionSession, name: str, fence: int = 0) -> AttemptKey:
    assert s.identity is not None
    return AttemptKey(s.identity, OwnerFence(fence), name)


def text_message(role: MessageRole, text: str) -> LLMMessageIR:
    return LLMMessageIR(role=role, parts=(TextPartIR(text),))


def user(text: str) -> LLMMessageIR:
    return text_message(MessageRole.USER, text)


def assistant(text: str) -> LLMMessageIR:
    return text_message(MessageRole.ASSISTANT, text)


def shell(messages: tuple[LLMMessageIR, ...] = (), budget: int = 128) -> LLMRequestIR:
    return LLMRequestIR(
        messages=messages, tools=(),
        policy=GenerationPolicyIR(max_output_tokens=budget),
    )


def receipt(s: EndpointProjectionSession, key: AttemptKey, *,
            calls: tuple[str, ...] = (), native: bool = False) -> HistoryCommitReceipt:
    digest = hashlib.sha256((s.frontier.prefix_digest + key.attempt_id).encode()).hexdigest()
    after = HistoryCursor(s.frontier.history_epoch, s.frontier.block_sequence + 1, digest)
    return HistoryCommitReceipt(
        attempt=key,
        append=AppendReceipt(s.frontier, after, 1),
        closed_call_ids=calls,
        native_committed=native,
    )


def payload(prepared: PreparedRequest) -> dict[str, Any]:
    return json.loads(prepared.payload_json)


def seeded(shape: WireShape = WireShape.OPENAI_COMPLETION):
    s = new_session(shape)
    key = key_for(s, "first")
    question, answer = user("ORIGINAL_QUESTION"), assistant("ORIGINAL_ANSWER")
    request_shell = shell((question,))
    s.begin_round(key, requires_native=False)
    s.prepare(HistoryView(s.frontier, (question,)), request_shell=request_shell)
    s.observe_commit(receipt(s, key), accepted_messages=(answer,))
    return s, request_shell, question, answer


def repaired_round(s: EndpointProjectionSession, key: AttemptKey,
                   kind: NativeContinuationKind) -> ClosedRound:
    native_payload = {"output": [
        {"id": "rs-test", "type": "reasoning", "summary": [],
         "encrypted_content": "SYNTHETIC_ENCRYPTED_NOT_FOR_LIVE_API"},
        {"id": "fc-test", "type": "function_call", "call_id": "call-a",
         "name": "lookup", "arguments": "{}"},
    ]}
    material = NativeMaterial(key, ("call-a",), json.dumps(native_payload))
    return ClosedRound(
        attempt=key,
        calls=(ToolCallRecord("call-a", "lookup", "{}"),),
        results=(ToolResultRecord("call-a", "42", ToolOutcome.SUCCESS),),
        continuation=NativeContinuation(kind, material),
    )


class ProjectionFollowupReview(unittest.TestCase):
    def test_anthropic_system_survives_second_incremental_prepare(self):
        s = new_session(WireShape.ANTHROPIC_MESSAGES)
        system = text_message(MessageRole.SYSTEM, "SYSTEM_SENTINEL")
        q = user("first question")
        request_shell = shell((system, q))
        first_key = key_for(s, "first")
        s.begin_round(first_key, requires_native=False)
        # Single-supply contract (review G1/G2): the shell owns the system
        # preamble; the view owns the chronological conversation.  Supplying
        # the same system message through BOTH channels would legitimately
        # produce two top-level system parts now that tail-hoisted head
        # content is merged instead of silently dropped.
        first = payload(s.prepare(HistoryView(s.frontier, (q,)),
                                  request_shell=request_shell))
        self.assertIn("system", first)
        s.observe_commit(receipt(s, first_key), accepted_messages=(assistant("first answer"),))
        s.begin_round(key_for(s, "second"), requires_native=False)
        second = payload(s.prepare(HistoryView(s.frontier, (user("second question"),)),
                                   request_shell=request_shell))
        self.assertEqual(second.get("system"), first["system"])

    def test_tail_developer_keeps_whole_history_role_semantics(self):
        s, request_shell, q, a = seeded()
        guidance = LLMMessageIR(
            role=MessageRole.DEVELOPER,
            parts=(TextPartIR("LATE_GUIDANCE"),),
            prompt_region=PromptRegionIR.ACTIVE_HISTORY,
        )
        s.begin_round(key_for(s, "second"), requires_native=False)
        incremental = payload(s.prepare(HistoryView(s.frontier, (guidance,)),
                                        request_shell=request_shell))
        context = ShapeContext(
            wire_shape=s.binding.wire_shape,
            endpoint_id=s.binding.endpoint_id,
            model_id=s.binding.model_id,
        )
        full = thaw_json(codec_for_shape(s.binding.wire_shape).encode(
            shell((q, a, guidance)), context,
        ).payload)
        self.assertEqual(incremental["messages"], full["messages"])

    def test_restored_nonempty_history_accepts_zero_tail_with_explicit_shell(self):
        original, request_shell, _, _ = seeded()
        saved = {"projection": snapshot_projection(original)}
        restored = EndpointProjectionSession(original.session_id)
        restore_projection(saved, l1_history_cursor=original.frontier, session=restored)
        restored.begin_round(key_for(restored, "after-restart", fence=1), requires_native=False)
        wire = payload(restored.prepare(HistoryView(restored.frontier, ()),
                                        request_shell=request_shell))
        self.assertEqual(wire["model"], "test-model")
        self.assertIn("ORIGINAL_QUESTION", json.dumps(wire))
        self.assertIn("ORIGINAL_ANSWER", json.dumps(wire))

    def test_zero_tail_honors_new_attempt_output_budget(self):
        s, _, _, _ = seeded()
        s.begin_round(key_for(s, "second"), requires_native=False)
        wire = payload(s.prepare(HistoryView(s.frontier, ()),
                                 request_shell=shell(budget=37)))
        self.assertEqual(wire["max_tokens"], 37)

    def test_anthropic_committed_tool_result_does_not_need_manual_refeed(self):
        s = new_session(WireShape.ANTHROPIC_MESSAGES)
        key = key_for(s, "tool-round")
        s.begin_round(key, requires_native=False)
        s.prepare(HistoryView(s.frontier, (user("lookup please"),)))
        call_message = LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(ToolCallIR("call-a", "lookup", {}),),
        )
        result_message = LLMMessageIR(
            role=MessageRole.TOOL,
            parts=(ToolResultIR("call-a", "lookup", "42", ok=True),),
        )
        s.observe_commit(receipt(s, key, calls=("call-a",)),
                         accepted_messages=(call_message, result_message))
        s.begin_round(key_for(s, "continuation"), requires_native=False)
        wire = payload(s.prepare(HistoryView(s.frontier, ())))
        blocks = [block for message in wire["messages"]
                  for block in message.get("content", []) if isinstance(block, dict)]
        calls = [block["id"] for block in blocks if block.get("type") == "tool_use"]
        results = [(block["tool_use_id"], block.get("content"))
                   for block in blocks if block.get("type") == "tool_result"]
        self.assertEqual(calls, ["call-a"])
        self.assertIn(("call-a", "42"), [
            (call_id, json.dumps(content) if not isinstance(content, str) else content)
            for call_id, content in results
        ])

    def test_required_native_repair_emits_each_accepted_call_once(self):
        s = new_session(WireShape.OPENAI_RESPONSE)
        key = key_for(s, "repair")
        s.begin_round(key, requires_native=True)
        s.prepare(HistoryView(s.frontier, (user("lookup please"),)))
        closed = repaired_round(s, key, NativeContinuationKind.REQUIRED)
        s.accept_repaired_round(closed, cursor_after=HistoryCursor(0, 1, "d" * 64), block_count=1)
        s.begin_round(key_for(s, "continuation"), requires_native=False)
        wire = payload(s.prepare(HistoryView(s.frontier, ())))
        calls = [item for item in wire["input"] if item.get("type") == "function_call"]
        outputs = [item for item in wire["input"] if item.get("type") == "function_call_output"]
        self.assertEqual([item["call_id"] for item in calls], ["call-a"])
        self.assertEqual([item["call_id"] for item in outputs], ["call-a"])
        self.assertIn("SYNTHETIC_ENCRYPTED_NOT_FOR_LIVE_API", json.dumps(wire))

    def test_optional_native_repair_consumes_material_carried_by_closed_round(self):
        s = new_session(WireShape.OPENAI_RESPONSE)
        key = key_for(s, "repair-optional")
        s.begin_round(key, requires_native=False)
        s.prepare(HistoryView(s.frontier, (user("lookup please"),)))
        closed = repaired_round(s, key, NativeContinuationKind.OPTIONAL)
        s.accept_repaired_round(closed, cursor_after=HistoryCursor(0, 1, "e" * 64), block_count=1)
        s.begin_round(key_for(s, "continuation"), requires_native=False)
        wire = payload(s.prepare(HistoryView(s.frontier, ())))
        calls = [item for item in wire["input"] if item.get("type") == "function_call"]
        outputs = [item for item in wire["input"] if item.get("type") == "function_call_output"]
        self.assertEqual([item["call_id"] for item in calls], ["call-a"])
        self.assertEqual([item["call_id"] for item in outputs], ["call-a"])

    def test_sendable_gate_rejects_orphan_duplicate_and_reversed_protocol(self):
        s = new_session(WireShape.OPENAI_RESPONSE)
        call = {"type": "function_call", "call_id": "call-a", "name": "lookup", "arguments": "{}"}
        result = {"type": "function_call_output", "call_id": "call-a", "output": "42"}
        cases = {
            "orphan-result": [result],
            "duplicate-call-in-one-round": [call, dict(call), result],
            "result-before-call": [result, call],
        }
        for name, items in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ProjectionContractError):
                    PreparedRequest.build(key_for(s, name), s.frontier,
                                          {"model": "test-model", "input": items})

    def test_prepared_constructor_cannot_bypass_build_validation(self):
        s = new_session(WireShape.OPENAI_RESPONSE)
        encoded = json.dumps({"model": "test-model", "input": [
            {"type": "function_call", "call_id": "unanswered", "name": "lookup", "arguments": "{}"},
        ]})
        with self.assertRaises(ProjectionContractError):
            PreparedRequest(
                attempt=key_for(s, "direct-constructor"),
                base_cursor=s.frontier,
                payload_json=encoded,
                payload_digest=hashlib.sha256(encoded.encode()).hexdigest(),
            )

    def test_restore_does_not_alias_callers_mutable_snapshot(self):
        original, request_shell, _, _ = seeded()
        saved = {"projection": snapshot_projection(original)}
        restored = EndpointProjectionSession(original.session_id)
        restore_projection(saved, l1_history_cursor=original.frontier, session=restored)
        old_cursor = restored.frontier
        # Alter the caller-owned input AFTER a successful restore. No private
        # session field or public frozen chunk is modified by this test.
        saved["projection"]["chunks"][0]["items"][0]["content"][0]["text"] = "CORRUPTED_BY_CALLER"
        restored.begin_round(key_for(restored, "after-restart", fence=1), requires_native=False)
        prepared = restored.prepare(HistoryView(restored.frontier, (user("next question"),)),
                                    request_shell=request_shell)
        self.assertEqual(restored.frontier, old_cursor)
        self.assertIn("ORIGINAL_QUESTION", prepared.payload_json)
        self.assertNotIn("CORRUPTED_BY_CALLER", prepared.payload_json)


if __name__ == "__main__":
    unittest.main()
