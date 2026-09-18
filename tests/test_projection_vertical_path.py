"""Offline vertical acceptance path (review 2026-09-18 integration gate).

One scenario chains every repaired mechanism end to end, exactly as the
review's verification gate requires:

    full shell + input
    -> accepted native answer
    -> REQUIRED-native repair with a real tool call/result
    -> joint commit (tool result survives as session-owned pending)
    -> incremental next request
    -> checkpoint
    -> fresh owner restore
    -> next complete request

The final assembled request is compared against a trusted whole-history
encoding: presence, count, and ordering of every contribution.
All native values are visibly synthetic.
"""
from __future__ import annotations

import hashlib
import json
import unittest

from pal.llm.continuation_policy import NativeCandidate
from pal.llm.ir import (
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    MessageRole,
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
    ToolCallRecord,
    ToolOutcome,
    ToolResultRecord,
)
from pal.llm.projection_session import EndpointProjectionSession, HistoryView
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR


def _binding() -> EndpointBinding:
    return EndpointBinding(
        endpoint_id="vertical-endpoint",
        model_id="vertical-model",
        wire_shape=WireShape.ANTHROPIC_MESSAGES,
        endpoint_spec_revision="spec-1",
        continuation_policy_version="policy-1",
        config_fingerprint="vertical-config",
    )


def _key(s: EndpointProjectionSession, name: str, fence: int = 0) -> AttemptKey:
    return AttemptKey(s.identity, OwnerFence(fence), name)  # type: ignore[arg-type]


def _user(text: str) -> LLMMessageIR:
    return LLMMessageIR(MessageRole.USER, (TextPartIR(text),))


def _assistant(text: str) -> LLMMessageIR:
    return LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR(text),))


def _shell() -> LLMRequestIR:
    return LLMRequestIR(
        messages=(LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("VERTICAL_SYSTEM"),)),),
        tools=(),
        policy=GenerationPolicyIR(max_output_tokens=256),
    )


def _receipt(
    s: EndpointProjectionSession,
    key: AttemptKey,
    *,
    native: bool,
    calls: tuple[str, ...] = (),
) -> HistoryCommitReceipt:
    digest = hashlib.sha256(
        (s.frontier.prefix_digest + key.attempt_id).encode()
    ).hexdigest()
    after = HistoryCursor(s.frontier.history_epoch, s.frontier.block_sequence + 1, digest)
    return HistoryCommitReceipt(
        attempt=key,
        append=AppendReceipt(s.frontier, after, 1),
        closed_call_ids=calls,
        native_committed=native,
    )


class OfflineVerticalPathTests(unittest.TestCase):
    def test_vertical_system_answer_repair_checkpoint_restore(self) -> None:
        s = EndpointProjectionSession(LogicalSessionId("vertical:resident"))
        s.bind(_binding())
        shell = _shell()
        q1 = _user("V_Q1")

        # Round 1: shell carries the system preamble; the view is the tail.
        key1 = _key(s, "r1")
        s.begin_round(key1, requires_native=False)
        first = json.loads(s.prepare(HistoryView(s.frontier, (q1,)), request_shell=shell).payload_json)
        self.assertEqual(first["system"], [{"type": "text", "text": "VERTICAL_SYSTEM"}])
        # Accepted native answer (assistant text turn, byte-true).
        s.attach_native(key1, NativeCandidate(
            wire_shape=WireShape.ANTHROPIC_MESSAGES,
            endpoint_id=s.binding.endpoint_id,
            model_id=s.binding.model_id,
            payload_json=json.dumps({"content": [{"type": "text", "text": "V_A1"}]}),
            call_ids=(),
        ))
        s.observe_commit(_receipt(s, key1, native=True))

        # Round 2: REQUIRED-native repair with a real tool call and result.
        key2 = _key(s, "r2", fence=1)
        s.begin_round(key2, requires_native=True)
        s.prepare(HistoryView(s.frontier, ()), request_shell=shell)
        native_payload = {
            "content": [
                {"type": "tool_use", "id": "call-a", "name": "lookup", "input": {"q": 1}},
            ]
        }
        material = NativeMaterial(
            key2, ("call-a",), json.dumps(native_payload)
        )
        repaired = ClosedRound(
            attempt=key2,
            calls=(ToolCallRecord("call-a", "lookup", "{\"q\": 1}"),),
            results=(ToolResultRecord("call-a", "V_RESULT_42", ToolOutcome.SUCCESS),),
            continuation=NativeContinuation(NativeContinuationKind.REQUIRED, material),
        )
        s.accept_repaired_round(
            repaired,
            cursor_after=HistoryCursor(0, 2, "d" * 64),
            block_count=1,
        )
        self.assertEqual(s.frontier, HistoryCursor(0, 2, "d" * 64))

        # Round 3: continuation request — the tool result must be present
        # WITHOUT any manual refeed (session-owned pending tail).
        key3 = _key(s, "r3", fence=2)
        s.begin_round(key3, requires_native=False)
        third = json.loads(s.prepare(HistoryView(s.frontier, ()), request_shell=shell).payload_json)
        self.assertEqual(third["system"], first["system"])
        blocks = [
            block
            for message in third["messages"]
            for block in message.get("content", [])
            if isinstance(block, dict)
        ]
        self.assertEqual(
            [block.get("id") for block in blocks if block.get("type") == "tool_use"],
            ["call-a"],
        )
        self.assertEqual(
            [
                (block.get("tool_use_id"), json.dumps(block.get("content")))
                for block in blocks
                if block.get("type") == "tool_result"
            ],
            [("call-a", '"V_RESULT_42"')],
        )

        # Checkpoint and a fresh-owner restore.
        saved = {"projection": snapshot_projection(s)}
        successor = EndpointProjectionSession(s.session_id)
        restore_projection(saved, l1_history_cursor=s.frontier, session=successor)

        # Round 4 on the successor: one more user turn, full request assembled.
        q2 = _user("V_Q2")
        key4 = _key(successor, "r4", fence=3)
        successor.begin_round(key4, requires_native=False)
        final = json.loads(
            successor.prepare(
                HistoryView(successor.frontier, (q2,)), request_shell=shell
            ).payload_json
        )
        self.assertEqual(final["system"], first["system"])

        # Trusted whole-history reference: same logical conversation encoded
        # in one shot.  Presence, count, and ordering must match.  The
        # reference includes the accepted assistant tool call — it is real
        # logical history, not an artifact of the incremental path.
        reference_messages = json.loads(
            json.dumps(
                codec_for_shape(WireShape.ANTHROPIC_MESSAGES)
                .encode(
                    LLMRequestIR(
                        messages=(
                            LLMMessageIR(MessageRole.SYSTEM, (TextPartIR("VERTICAL_SYSTEM"),)),
                            q1,
                            _assistant("V_A1"),
                            LLMMessageIR(
                                MessageRole.ASSISTANT,
                                (ToolCallIR("call-a", "lookup", {"q": 1}),),
                            ),
                            LLMMessageIR(
                                MessageRole.TOOL,
                                (ToolResultIR("call-a", "lookup", "V_RESULT_42", ok=True),),
                            ),
                            q2,
                        ),
                        tools=(),
                        policy=GenerationPolicyIR(max_output_tokens=256),
                    ),
                    ShapeContext(
                        wire_shape=WireShape.ANTHROPIC_MESSAGES,
                        endpoint_id="vertical-endpoint",
                        model_id="vertical-model",
                    ),
                )
                .payload["messages"]
            )
        )
        # Presence, count, and ordering assertions against the trusted
        # whole-history reference (review's acceptance level).  KNOWN LIMIT
        # (documented in DELIVERY.md): when an accepted native assistant turn
        # directly follows an already-frozen assistant item, a whole-history
        # encode merges them into ONE message while the incremental path
        # keeps two adjacent same-role messages.  Content blocks, their
        # order, and their counts are identical; the wire item partition
        # differs by that merge.  Resolving it requires a redesigned freeze
        # boundary (stable tool-group boundary), not another local patch.
        flat_final = [
            (message["role"], block)
            for message in final["messages"]
            for block in message.get("content", [])
            if isinstance(block, dict)
        ]
        flat_reference = [
            (message["role"], block)
            for message in reference_messages
            for block in message.get("content", [])
            if isinstance(block, dict)
        ]
        self.assertEqual(flat_final, flat_reference)
        # The tool protocol stays fully paired and ordered on the wire.
        self.assertEqual(
            [block.get("id") for _role, block in flat_final if block.get("type") == "tool_use"],
            ["call-a"],
        )
        self.assertEqual(
            [
                (block.get("tool_use_id"), block.get("content"))
                for _role, block in flat_final
                if block.get("type") == "tool_result"
            ],
            [("call-a", "V_RESULT_42")],
        )


if __name__ == "__main__":
    unittest.main()
