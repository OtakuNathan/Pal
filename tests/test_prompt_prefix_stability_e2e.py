"""E2E wire-prefix stability across the real turn-executor assembly path.

For each wire shape, consecutive requests must share a byte-identical encoded
prefix: round 2 (same active turn after a tool round) extends round 1, and a
later turn after settlement extends round 2.  This pins the full chain —
message assembly order, replay-envelope preservation, and codec determinism —
not just the codec in isolation.
"""

from __future__ import annotations

import json

import pytest

from pal.core import PalCore, TurnContinuation, register_with_core
from pal.foundation import EventEnvelope
from pal.llm.ir import (
    LLMMessageIR,
    MessageRole,
    MessageState,
    ReasoningPartIR,
    ReplayEnvelope,
    TextPartIR,
    WireShape,
)
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.memory import MemoryService, register_with_core as register_memory
from pal.shared import PromptAssemblyContext
from pal.shared.tool_protocol import new_tool_call, ToolResultIR

ENDPOINT = "demo"
MODEL = "demo-model"
REASONING = "thinking about prefix stability"
REPLY = "inspected the file"


def _setup():
    core, service = PalCore(), MemoryService()
    register_with_core(core)
    register_memory(core.context, service)
    return core, service


def _request(core, turn_id: str):
    continuation = TurnContinuation(
        turn_id=turn_id, program=iter(()), correlation_id=turn_id
    )
    return core.turn_executor.build_turn_prompt(
        continuation,
        PromptAssemblyContext(
            event=EventEnvelope(
                event_kind="user.message",
                source_kind="channel",
                payload={"text": "continue"},
            )
        ),
        max_output_tokens=128,
    )


def _encoded_items(request, shape: WireShape) -> list:
    codec = codec_for_shape(shape)
    payload = codec.encode(
        request, ShapeContext(wire_shape=shape, endpoint_id=ENDPOINT, model_id=MODEL)
    ).payload
    key = "input" if shape == WireShape.OPENAI_RESPONSE else "messages"
    return payload[key]


def _assistant_message(shape: WireShape) -> LLMMessageIR:
    if shape == WireShape.OPENAI_COMPLETION:
        envelope_payload = {
            "message": {
                "role": "assistant",
                "reasoning_content": REASONING,
                "content": REPLY,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"file_path": "a.py"}),
                        },
                    }
                ],
            }
        }
    elif shape == WireShape.OPENAI_RESPONSE:
        envelope_payload = {
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": REPLY}],
                },
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "read_file",
                    "arguments": json.dumps({"file_path": "a.py"}),
                },
            ]
        }
    else:
        envelope_payload = {
            "content": [
                {"type": "thinking", "thinking": REASONING},
                {"type": "text", "text": REPLY},
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "read_file",
                    "input": {"file_path": "a.py"},
                },
            ]
        }
    return LLMMessageIR(
        role=MessageRole.ASSISTANT,
        state=MessageState.COMPLETE,
        parts=(
            ReasoningPartIR(REASONING),
            TextPartIR(REPLY),
            new_tool_call("call-1", "read_file", {"file_path": "a.py"}),
        ),
        replay=ReplayEnvelope(shape, ENDPOINT, MODEL, envelope_payload),
    )


@pytest.mark.parametrize(
    "shape",
    [
        WireShape.OPENAI_COMPLETION,
        WireShape.OPENAI_RESPONSE,
        WireShape.ANTHROPIC_MESSAGES,
    ],
)
def test_consecutive_requests_keep_byte_identical_prefix(shape: WireShape) -> None:
    core, service = _setup()
    service.begin_l1_turn("turn-one", user_text="inspect the setup")

    # Round 1: only the user message exists.
    items_one = _encoded_items(_request(core, "turn-one"), shape)

    # The provider responded with reasoning + a tool call; the tool ran.
    service.upsert_l1_assistant("turn-one", _assistant_message(shape))
    service.append_l1_tool_result(
        "turn-one",
        ToolResultIR(call_id="call-1", name="read_file", content="file bytes"),
    )

    # Round 2: same active turn, history now includes the assistant round.
    items_two = _encoded_items(_request(core, "turn-one"), shape)
    assert items_two[: len(items_one)] == items_one, (
        "round 2 changed the encoded prefix of round 1"
    )
    # The envelope really replayed (guard against a stripped, meaningless
    # prefix). Responses-shape reasoning items carry no plaintext, so check
    # the reply text plus the reasoning item itself per shape.
    new_items = json.dumps(items_two[len(items_one) :], ensure_ascii=False)
    assert REPLY in new_items, "assistant reply did not replay on the wire"
    if shape == WireShape.OPENAI_RESPONSE:
        assert '"type": "reasoning"' in new_items
    else:
        assert REASONING in new_items, "assistant reasoning did not replay on the wire"

    # Settlement, then a later turn.
    service.settle_l1_turn("turn-one")
    service.begin_l1_turn("turn-two", user_text="now the second request")
    items_three = _encoded_items(_request(core, "turn-two"), shape)
    assert items_three[: len(items_two)] == items_two, (
        "settlement changed the encoded prefix across turns"
    )
    assert len(items_three) > len(items_two)
