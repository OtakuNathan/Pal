"""Settlement must not mutate wire bytes: replay envelopes survive turn closure.

The regression these tests guard: ``_close_message`` used to drop the wire
replay envelope when a turn settled, so the same-endpoint prefix silently
changed bytes at every turn boundary and the whole cached prefix had to be
rewritten.  Provider-neutral reasoning parts are still retired (they are the
cross-endpoint fallback projection); only the endpoint-bound envelope is
preserved so same-endpoint replays stay byte-identical.
"""

from __future__ import annotations

import hashlib
import json
import unittest

from pal.llm.ir import (
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    MessageRole,
    MessageState,
    ReasoningPartIR,
    ReplayEnvelope,
    TextPartIR,
    WireShape,
)
from pal.llm.serde import message_from_payload, message_to_payload
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.memory.service import MemoryService
from pal.memory.runtime_state import MemoryRuntimeStatePort
from pal.memory.turn_ir import L1TurnState, L1TurnStore
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR


def _context(shape: WireShape, endpoint: str = "demo", model: str = "demo-model") -> ShapeContext:
    return ShapeContext(wire_shape=shape, endpoint_id=endpoint, model_id=model)


def _payload_hash(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def _assistant_message(shape: WireShape) -> LLMMessageIR:
    if shape == WireShape.OPENAI_COMPLETION:
        envelope_payload = {
            "message": {
                "role": "assistant",
                "reasoning_content": "chain of thought",
                "content": "answer",
            }
        }
    elif shape == WireShape.OPENAI_RESPONSE:
        envelope_payload = {
            "output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]},
            ]
        }
    else:
        envelope_payload = {
            "content": [
                {"type": "thinking", "thinking": "chain of thought"},
                {"type": "text", "text": "answer"},
            ]
        }
    return LLMMessageIR(
        role=MessageRole.ASSISTANT,
        state=MessageState.COMPLETE,
        parts=(ReasoningPartIR("chain of thought"), TextPartIR("answer")),
        replay=ReplayEnvelope(shape, "demo", "demo-model", envelope_payload),
    )


def _encode_messages(shape: WireShape, messages, endpoint: str = "demo", model: str = "demo-model"):
    request = LLMRequestIR(
        messages=tuple(messages),
        tools=(),
        policy=GenerationPolicyIR(max_output_tokens=100),
    )
    codec = codec_for_shape(shape)
    return codec.encode(request, _context(shape, endpoint, model)).payload


class SettledReplayIdentityTests(unittest.TestCase):
    def test_settled_replay_encode_is_byte_identical_for_all_shapes(self) -> None:
        for shape in (
            WireShape.OPENAI_COMPLETION,
            WireShape.OPENAI_RESPONSE,
            WireShape.ANTHROPIC_MESSAGES,
        ):
            with self.subTest(shape=shape):
                store = L1TurnStore()
                turn = store.begin("turn-1", user_text="hello")
                turn = turn.upsert_assistant(_assistant_message(shape))
                store.replace(turn)

                active_payload = _encode_messages(
                    shape, store.require_active("turn-1").messages
                )
                settled = store.require_active("turn-1").settle()
                store.replace(settled)

                self.assertEqual(settled.state, L1TurnState.SETTLED)
                settled_payload = _encode_messages(shape, settled.messages)

                self.assertEqual(
                    _payload_hash(active_payload),
                    _payload_hash(settled_payload),
                    f"settlement changed wire bytes for {shape}",
                )
                self.assertEqual(settled.messages[-1].reasoning_text, "")

    def test_settled_cross_endpoint_encoding_omits_reasoning(self) -> None:
        store = L1TurnStore()
        turn = store.begin("turn-1", user_text="hello")
        turn = turn.upsert_assistant(_assistant_message(WireShape.OPENAI_COMPLETION))
        store.replace(turn)
        settled = store.require_active("turn-1").settle()
        store.replace(settled)

        switched = _encode_messages(
            WireShape.OPENAI_COMPLETION, settled.messages, endpoint="other", model="other-model"
        )
        assistant = switched["messages"][1]
        self.assertEqual(assistant.get("content"), "answer")
        self.assertNotIn("reasoning_content", assistant)

    def test_interrupted_turn_also_preserves_replay(self) -> None:
        store = L1TurnStore()
        turn = store.begin("turn-1", user_text="hello")
        turn = turn.upsert_assistant(_assistant_message(WireShape.OPENAI_COMPLETION))
        store.replace(turn)
        interrupted = store.require_active("turn-1").interrupt(reason="test")
        store.replace(interrupted)

        self.assertEqual(interrupted.state, L1TurnState.INTERRUPTED)
        self.assertIsNotNone(interrupted.messages[-1].replay)
        self.assertEqual(interrupted.messages[-1].reasoning_text, "")

    def test_serde_roundtrip_preserves_settled_replay(self) -> None:
        store = L1TurnStore()
        turn = store.begin("turn-1", user_text="hello")
        turn = turn.upsert_assistant(_assistant_message(WireShape.ANTHROPIC_MESSAGES))
        store.replace(turn)
        settled = store.require_active("turn-1").settle()
        store.replace(settled)

        message = settled.messages[-1]
        restored = message_from_payload(message_to_payload(message))
        self.assertIsNotNone(restored.replay)
        self.assertEqual(
            _payload_hash(dict(restored.replay.payload)),
            _payload_hash(dict(message.replay.payload)),
        )
        self.assertEqual(restored.replay.endpoint_id, "demo")
        self.assertEqual(restored.replay.model_id, "demo-model")

    def test_multi_round_prefix_stays_identical_across_settlement(self) -> None:
        """Bunshin-style loops settle between phases; every later request must
        replay the earlier assistant wire bytes unchanged."""

        shape = WireShape.OPENAI_COMPLETION
        store = L1TurnStore()
        turn = store.begin("turn-1", user_text="hello")
        turn = turn.upsert_assistant(_assistant_message(shape))
        store.replace(turn)

        # Round 2 inside the same active turn: the prefix includes round 1.
        round_two = _encode_messages(shape, store.require_active("turn-1").messages)

        # The turn settles (post-turn commit, as bunshin does between phases).
        settled = store.require_active("turn-1").settle()
        store.replace(settled)

        # Round 3 in a later phase: same endpoint, the settled bytes must not move.
        round_three = _encode_messages(shape, settled.messages)

        self.assertEqual(_payload_hash(round_two), _payload_hash(round_three))

    def test_bunshin_settlement_path_preserves_replay_envelope(self) -> None:
        """Bunshin settles through ``MemoryService.settle_l1_turn`` between
        phases (``schedule_post_turn_commit_async``).  The committed turn must
        keep its replay envelope so later phase requests replay identical
        wire bytes on the same endpoint."""

        shape = WireShape.OPENAI_COMPLETION
        service = MemoryService()
        service.begin_l1_turn("bunshin-turn", user_text="run the task")
        service.upsert_l1_assistant("bunshin-turn", _assistant_message(shape))

        before = _encode_messages(
            shape, service.l1_store.turns.require_active("bunshin-turn").messages
        )
        settled = service.settle_l1_turn("bunshin-turn")
        self.assertEqual(settled.state, L1TurnState.SETTLED)

        after = _encode_messages(shape, settled.messages)
        self.assertEqual(
            _payload_hash(before),
            _payload_hash(after),
            "bunshin settlement path changed wire bytes",
        )
        restored = service.l1_store.turns.get("bunshin-turn")
        self.assertIsNotNone(restored.messages[-1].replay)

    def test_runtime_state_restore_keeps_encoded_prefix_identical(self) -> None:
        """Restart-style snapshot/restore must not move the encoded prefix:
        restore used to strip replay envelopes, killing the cache prefix at
        every restart."""

        envelope = {
            "message": {
                "role": "assistant",
                "reasoning_content": "chain of thought",
                "content": "answer",
            }
        }
        service = MemoryService()
        service.begin_l1_turn("turn-1", user_text="hello")
        service.upsert_l1_assistant(
            "turn-1",
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(ReasoningPartIR("chain of thought"), TextPartIR("answer")),
                replay=ReplayEnvelope(
                    WireShape.OPENAI_COMPLETION, "demo", "demo-model", envelope
                ),
            ),
        )
        settled = service.settle_l1_turn("turn-1")
        before = _encode_messages(WireShape.OPENAI_COMPLETION, settled.messages)

        payload = dict(MemoryRuntimeStatePort(service).snapshot_state())
        restored = MemoryService()
        port = MemoryRuntimeStatePort(restored)
        port.install_prepared_state(port.prepare_restore_state(payload))

        turn = restored.l1_store.turns.get("turn-1")
        self.assertIsNotNone(turn.messages[-1].replay, "restore dropped the replay envelope")
        after = _encode_messages(WireShape.OPENAI_COMPLETION, turn.messages)
        self.assertEqual(
            _payload_hash(before),
            _payload_hash(after),
            "restore changed the encoded prefix",
        )

class RestoreProtocolRepairTests(unittest.TestCase):
    """Restore repair must reach the wire, not just the IR view.

    The regression these tests guard: ``_normalize_tool_protocol`` removed
    dangling tool calls from ``parts`` but kept the stale wire replay
    envelope, and same-endpoint encoders prefer replay over parts — so the
    repaired-away call reappeared on the wire with no matching result.
    """

    def _assistant_with_dangling_call(self) -> LLMMessageIR:
        envelope = {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "let me check"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": "{}",
                },
            ]
        }
        return LLMMessageIR(
            role=MessageRole.ASSISTANT,
            state=MessageState.COMPLETE,
            parts=(
                TextPartIR("let me check"),
                ToolCallIR(call_id="call-1", name="lookup"),
            ),
            replay=ReplayEnvelope(
                WireShape.OPENAI_RESPONSE, "demo", "demo-model", envelope
            ),
        )

    def test_restore_repair_does_not_smuggle_dangling_call_onto_wire(self) -> None:
        service = MemoryService()
        service.begin_l1_turn("turn-1", user_text="hello")
        service.upsert_l1_assistant(
            "turn-1", self._assistant_with_dangling_call()
        )

        payload = dict(MemoryRuntimeStatePort(service).snapshot_state())
        restored = MemoryService()
        port = MemoryRuntimeStatePort(restored)
        port.install_prepared_state(port.prepare_restore_state(payload))

        assistant = restored.l1_store.turns.get("turn-1").messages[-1]
        # The IR view was repaired...
        self.assertFalse(
            any(isinstance(part, ToolCallIR) for part in assistant.parts),
            "dangling call survived restore normalization in parts",
        )
        # ...and the stale envelope no longer rides along.
        self.assertIsNone(
            assistant.replay,
            "repaired message kept its stale replay envelope",
        )

        encoded = _encode_messages(
            WireShape.OPENAI_RESPONSE,
            restored.l1_store.turns.get("turn-1").messages,
        )
        dumped = json.dumps(encoded, ensure_ascii=False, default=str)
        self.assertNotIn("call-1", dumped, "dangling call re-entered the wire request")
        self.assertIn("let me check", dumped)

    def test_restore_keeps_replay_for_protocol_intact_messages(self) -> None:
        """Only repaired messages lose their envelope; a healthy
        call+result pair keeps its byte-stable replay across restore."""

        envelope = {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "checking"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call-2",
                    "name": "lookup",
                    "arguments": "{}",
                },
            ]
        }
        service = MemoryService()
        service.begin_l1_turn("turn-1", user_text="hello")
        service.upsert_l1_assistant(
            "turn-1",
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                state=MessageState.COMPLETE,
                parts=(
                    TextPartIR("checking"),
                    ToolCallIR(call_id="call-2", name="lookup"),
                ),
                replay=ReplayEnvelope(
                    WireShape.OPENAI_RESPONSE, "demo", "demo-model", envelope
                ),
            ),
        )
        service.append_l1_tool_result(
            "turn-1", ToolResultIR(call_id="call-2", name="lookup", content="42")
        )

        before = _encode_messages(
            WireShape.OPENAI_RESPONSE,
            service.l1_store.turns.get("turn-1").messages,
        )

        payload = dict(MemoryRuntimeStatePort(service).snapshot_state())
        restored = MemoryService()
        port = MemoryRuntimeStatePort(restored)
        port.install_prepared_state(port.prepare_restore_state(payload))

        assistant = restored.l1_store.turns.get("turn-1").messages[1]
        self.assertIsNotNone(
            assistant.replay,
            "protocol-intact message lost its replay envelope at restore",
        )
        after = _encode_messages(
            WireShape.OPENAI_RESPONSE,
            restored.l1_store.turns.get("turn-1").messages,
        )
        self.assertEqual(
            _payload_hash(before),
            _payload_hash(after),
            "restore changed the encoded prefix for a protocol-intact turn",
        )


if __name__ == "__main__":
    unittest.main()
