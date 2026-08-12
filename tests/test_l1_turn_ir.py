from __future__ import annotations

from pal.shared.tool_protocol import new_tool_call

import unittest

from pal.llm.ir import (
    LLMMessageIR,
    MessageRole,
    MessageState,
    ReasoningPartIR,
    ReplayEnvelope,
    TextPartIR,
    WireShape,
)
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR, ToolResultLifecycle
from pal.memory.turn_ir import L1TurnProtocolError, L1TurnState, L1TurnStore


class L1TurnIRTests(unittest.TestCase):
    def test_active_turn_retains_reasoning_and_settlement_retires_it_atomically(self) -> None:
        store = L1TurnStore()
        turn = store.begin("turn-1", user_text="hello")
        assistant = LLMMessageIR(
            role=MessageRole.ASSISTANT,
            state=MessageState.IN_PROGRESS,
            parts=(ReasoningPartIR("inspect"), TextPartIR("answer")),
            replay=ReplayEnvelope(WireShape.OPENAI_COMPLETION, "ep", "model", {"message": {}}),
        )
        turn = turn.upsert_assistant(assistant)
        store.replace(turn)
        self.assertEqual(store.require_active("turn-1").messages[-1].reasoning_text, "inspect")
        settled = store.require_active("turn-1").settle()
        store.replace(settled)
        self.assertEqual(settled.state, L1TurnState.SETTLED)
        self.assertEqual(settled.messages[-1].reasoning_text, "")
        self.assertIsNone(settled.messages[-1].replay)

    def test_tool_result_must_consume_one_pending_call_exactly_once(self) -> None:
        turn = L1TurnStore().begin("turn-1", user_text="hello")
        turn = turn.append(
            LLMMessageIR(
                MessageRole.ASSISTANT,
                (new_tool_call("call-1", "read", {"path": "a"}),),
            )
        )
        turn = turn.append_tool_result(ToolResultIR("call-1", "read", "ok"))
        self.assertFalse(turn.pending_call_ids)
        with self.assertRaises(L1TurnProtocolError):
            turn.append_tool_result(ToolResultIR("call-1", "read", "late"))

    def test_settlement_retires_full_result_but_keeps_temporary_pager_receipt(self) -> None:
        turn = L1TurnStore().begin("turn-1", user_text="inspect")
        turn = turn.append(
            LLMMessageIR(
                MessageRole.ASSISTANT,
                (new_tool_call("call-1", "read_file", {"path": "large.txt"}),),
            )
        )
        turn = turn.append_tool_result(
            ToolResultIR(
                "call-1",
                "read_file",
                "sensitive full result",
                structured={"full": "payload"},
                context_delivery={"source": "large.txt"},
                replay_result_ref="pager-1",
                visible_source_ranges=((0, 20),),
            )
        )

        settled = turn.settle()
        result = next(
            part
            for message in settled.messages
            for part in message.parts
            if isinstance(part, ToolResultIR)
        )

        self.assertEqual(result.lifecycle, ToolResultLifecycle.RETIRED)
        self.assertNotIn("sensitive full result", result.content)
        self.assertEqual(result.replay_result_ref, "pager-1")
        self.assertIn("read_tool_result", result.content)
        self.assertIsNone(result.context_delivery)
        self.assertEqual(result.visible_source_ranges, ())
        self.assertEqual(settled.messages[-1].role, MessageRole.ASSISTANT)
        self.assertEqual(
            settled.messages[-1].semantic_kind,
            "runtime_generated_closure",
        )

    def test_interruption_drops_unpaired_call_and_rejects_late_result(self) -> None:
        store = L1TurnStore()
        turn = store.begin("turn-1", user_text="hello")
        turn = turn.append(
            LLMMessageIR(
                MessageRole.ASSISTANT,
                (TextPartIR("partial"), new_tool_call("call-1", "write", {"path": "a"})),
                state=MessageState.IN_PROGRESS,
            )
        )
        interrupted = turn.interrupt(reason="user interrupt")
        store.replace(interrupted)
        self.assertEqual(interrupted.state, L1TurnState.INTERRUPTED)
        self.assertEqual(interrupted.messages[-1].text, "partial")
        self.assertEqual(interrupted.messages[-1].tool_calls, ())
        with self.assertRaises(L1TurnProtocolError):
            store.require_active("turn-1")

    def test_settlement_refuses_open_protocol(self) -> None:
        turn = L1TurnStore().begin("turn-1")
        turn = turn.append(LLMMessageIR(MessageRole.ASSISTANT, (new_tool_call("call-1", "read", {}),)))
        with self.assertRaises(L1TurnProtocolError):
            turn.settle()

    def test_unconsumed_truncated_assistant_can_be_discarded_atomically(self) -> None:
        turn = L1TurnStore().begin("turn-1", user_text="work")
        turn = turn.append(
            LLMMessageIR(
                MessageRole.ASSISTANT,
                (TextPartIR("partial"), new_tool_call("call-1", "write", {})),
                message_id="truncated-1",
            )
        )

        recovered = turn.discard_assistant("truncated-1")

        self.assertEqual(len(recovered.messages), 1)
        self.assertEqual(recovered.messages[0].role, MessageRole.USER)
        self.assertFalse(recovered.pending_call_ids)

    def test_consumed_assistant_cannot_be_discarded(self) -> None:
        turn = L1TurnStore().begin("turn-1", user_text="work")
        turn = turn.append(
            LLMMessageIR(
                MessageRole.ASSISTANT,
                (new_tool_call("call-1", "read", {}),),
                message_id="assistant-1",
            )
        )
        turn = turn.append_tool_result(ToolResultIR("call-1", "read", "ok"))

        with self.assertRaises(L1TurnProtocolError):
            turn.discard_assistant("assistant-1")


if __name__ == "__main__":
    unittest.main()
