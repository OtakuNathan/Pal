from __future__ import annotations

from pal.shared.tool_protocol import new_tool_call

import unittest

from pal.llm.ir import (
    LLMMessageIR,
    MessageRole,
    ReasoningPartIR,
    ReplayEnvelope,
    TextPartIR,
    WireShape,
)
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
from pal.memory import MemoryPackRequest, MemoryService
from pal.memory.turn_ir import L1TurnProtocolError, L1TurnState


class MemoryL1IRServiceTests(unittest.TestCase):
    def test_active_turn_is_single_protocol_truth_and_settlement_retires_replay(self) -> None:
        service = MemoryService()
        service.begin_l1_turn("turn-1", user_text="inspect")
        assistant = LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(
                ReasoningPartIR("private current-turn reasoning"),
                new_tool_call(call_id="call-1", name="read", arguments={"path": "a"}),
            ),
            replay=ReplayEnvelope(
                wire_shape=WireShape.OPENAI_COMPLETION,
                endpoint_id="endpoint",
                model_id="model",
                payload={"reasoning_content": "private current-turn reasoning"},
            ),
        )
        service.upsert_l1_assistant("turn-1", assistant)
        service.append_l1_tool_result(
            "turn-1",
            ToolResultIR(call_id="call-1", name="read", content="ok"),
        )

        active = service.active_l1_turn("turn-1")
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.pending_call_ids, frozenset())
        self.assertEqual(service.build_pack(MemoryPackRequest()).l1_recent_context, [])

        settled = service.settle_l1_turn("turn-1")
        self.assertEqual(settled.state, L1TurnState.SETTLED)
        self.assertFalse(any(message.reasoning_text for message in settled.messages))
        self.assertFalse(any(message.replay for message in settled.messages))
        self.assertTrue(service.build_pack(MemoryPackRequest()).l1_recent_context)

    def test_late_result_after_interrupt_is_rejected(self) -> None:
        service = MemoryService()
        service.begin_l1_turn("turn-1", user_text="write")
        service.upsert_l1_assistant(
            "turn-1",
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(
                    TextPartIR("partial"),
                    new_tool_call(call_id="call-1", name="write", arguments={}),
                ),
            ),
        )
        interrupted = service.interrupt_l1_turn("turn-1", reason="user interrupt")
        self.assertEqual(interrupted.state, L1TurnState.INTERRUPTED)
        self.assertFalse(interrupted.pending_call_ids)
        with self.assertRaises(L1TurnProtocolError):
            service.append_l1_tool_result(
                "turn-1",
                ToolResultIR(call_id="call-1", name="write", content="late"),
            )

    def test_interjection_append_is_idempotent_by_message_id(self) -> None:
        service = MemoryService()
        service.begin_l1_turn("turn-1", user_text="start")
        message = LLMMessageIR(
            role=MessageRole.USER,
            parts=(TextPartIR("additional context"),),
            message_id="interjection-1",
            semantic_kind="user_interjection",
        )

        first = service.append_l1_user("turn-1", message)
        second = service.append_l1_user("turn-1", message)

        self.assertEqual(first.revision, second.revision)
        self.assertEqual(
            [item.message_id for item in second.messages].count("interjection-1"),
            1,
        )
        with self.assertRaises(L1TurnProtocolError):
            service.append_l1_user(
                "turn-1",
                LLMMessageIR(
                    role=MessageRole.USER,
                    parts=(TextPartIR("different content"),),
                    message_id="interjection-1",
                    semantic_kind="user_interjection",
                ),
            )


if __name__ == "__main__":
    unittest.main()
