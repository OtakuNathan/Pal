"""Shared MemoryService fixtures for compaction tests.

Rehomed from the retired whole-source vertical suite: these build generic
L1 state (seed + settled turns + one active tool turn) and carry no
mode-specific assumptions.
"""
from __future__ import annotations

from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.memory import MemoryService
from pal.memory.contracts import L1MessageKind, L1TranscriptMessage
from pal.shared.tool_protocol import ToolResultIR, new_tool_call


def _seed_transcript(text: str = "SEED0 prior summary") -> list[L1TranscriptMessage]:
    return [
        L1TranscriptMessage(
            role="assistant",
            content=text,
            kind=L1MessageKind.RUNTIME_CONTEXT_SUMMARY,
        )
    ]


def _settled_transcript(mark: str) -> list[L1TranscriptMessage]:
    return [
        L1TranscriptMessage(role="user", content=f"{mark} request",
                            kind=L1MessageKind.USER_REQUEST),
        L1TranscriptMessage(role="assistant", content=f"{mark} reply",
                            kind=L1MessageKind.ASSISTANT_REPLY),
    ]


def _service_with_active(*, seed: bool = True, settled: int = 1) -> tuple[MemoryService, str, dict]:
    """Service with previous seed + settled turns + one active tool turn.

    The active turn embeds the E01 sentinels: Q_ORIGINAL, A_DECISION, a
    single executed tool call A and its RESULT_A.
    """
    service = MemoryService()
    if seed:
        service.l1_store.append(_seed_transcript())
    for index in range(settled):
        service.l1_store.append(_settled_transcript(f"settled-{index}"))
    service.history_root.promote()  # Previous turns are already submitted history.
    turn_id = "logical-task-T"
    service.begin_l1_turn(turn_id, user_text="Q_ORIGINAL: implement the feature")
    service.upsert_l1_assistant(
        turn_id,
        LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(
                TextPartIR("A_DECISION: start with file a.cpp"),
                new_tool_call(call_id="call-A", name="read_file", arguments={"file": "a.cpp"}),
            ),
            message_id="assistant-1",
        ),
    )
    service.append_l1_tool_result(
        turn_id,
        ToolResultIR(call_id="call-A", name="read_file", content="RESULT_A: file body"),
    )
    return service, turn_id, {}
