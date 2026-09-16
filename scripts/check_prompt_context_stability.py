"""Measure prompt structure offline; never calls an LLM or executes a shell tool.

Run with PYTHONPATH=src. Use a baseline checkout's src for a paired comparison.
"""
from __future__ import annotations

import argparse
import json

from pal.core import PalCore, register_with_core
from pal.core.turns import TurnContinuation
from pal.llm.ir import LLMMessageIR, MessageRole
from pal.memory import MemoryService, register_with_core as register_memory
from pal.shared import PromptAssemblyContext, PromptFragment
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR


class FixtureContext:
    provider_id = "comparison"
    module_id = "comparison"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        return [
            PromptFragment(
                section="runtime", title="Date", content="Today is 2026-09-16.",
                metadata={"prompt_target": "runtime_reminder", "block_id": "date"},
            ),
            PromptFragment(
                section="artifact", title="Reference", content="Fixture reference material.",
                metadata={"prompt_target": "user_context"},
            ),
        ]


def measure(rounds: int = 50) -> dict[str, int]:
    if rounds < 2:
        raise ValueError("at least two rounds are required")
    core = PalCore()
    try:
        register_with_core(core)
        memory = MemoryService()
        register_memory(core.context, memory)
        core.context.prompt_fragment_registry.register(FixtureContext())
        turn = TurnContinuation("comparison-turn", iter(()), "comparison-request")
        memory.begin_l1_turn(turn.turn_id, user_text="Inspect the fixture.")
        previous = None
        preserved = 0
        added_records = []
        stable_characters = 0
        for index in range(rounds):
            before = memory.active_l1_turn(turn.turn_id)
            request = core.turn_executor.build_turn_prompt(
                turn, PromptAssemblyContext(), max_output_tokens=100,
            )
            after = memory.active_l1_turn(turn.turn_id)
            added_records.append(len(after.messages) - len(before.messages))
            current = [(message.role, message.parts) for message in request.messages]
            if previous is not None:
                preserved += current[:len(previous)] == previous
            else:
                stable_characters = sum(
                    len(message.text) for message in request.messages
                    if message.prompt_region.value == "stable_system"
                )
            previous = current
            # Synthetic protocol history only: these calls are never dispatched.
            call = ToolCallIR(call_id=f"c{index}", name="fixture", arguments={})
            memory.upsert_l1_assistant(
                turn.turn_id, LLMMessageIR(role=MessageRole.ASSISTANT, parts=(call,)),
            )
            memory.append_l1_tool_result(
                turn.turn_id,
                ToolResultIR(call_id=call.call_id, name="fixture", content="fixture result"),
            )
        return {
            "stable_instruction_characters": stable_characters,
            "preserved_complete_prefix_pairs": preserved,
            "pairs": rounds - 1,
            "initial_l1_context_records": added_records[0],
            "later_l1_context_records": sum(added_records[1:]),
            "final_request_messages": len(previous),
        }
    finally:
        core.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=50)
    args = parser.parse_args()
    print(json.dumps(measure(args.rounds), sort_keys=True))
