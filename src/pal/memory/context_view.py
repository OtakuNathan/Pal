"""Indexed L1 records and immutable request visibility, independent of plugins."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, TYPE_CHECKING

from pal.llm.ir import LLMMessageIR, MessageRole
from pal.memory.continuity import is_summary_projection

if TYPE_CHECKING:
    from pal.memory.turn_ir import L1TurnIR


def projected_messages(turn: "L1TurnIR", *, settled: bool = False) -> tuple[LLMMessageIR, ...]:
    excluded = frozenset(turn.metadata.get("prompt_context_state", {}).get("excluded", ()))
    return tuple(m for m in turn.messages if not is_summary_projection(m) and m.message_id not in excluded and not (
        settled and m.semantic_kind == "pal_prompt_context"
        and m.metadata.get("pal_authored") and m.role == MessageRole.DEVELOPER
    ))


@dataclass(frozen=True)
class TurnView:
    messages: Mapping[str, LLMMessageIR]
    tool_results: Mapping[str, str]

    @classmethod
    def build(cls, messages, calls):
        records = {m.message_id: m for m in messages}
        return cls(MappingProxyType(records), MappingProxyType({
            call: message for call, message in calls.items() if message in records}))


@dataclass(frozen=True)
class L1ContextView:
    turns: Mapping[str, TurnView]
    coverage: Mapping[tuple[str, str], tuple[tuple[str, Mapping[str, Any]], ...]]

    def __post_init__(self):
        object.__setattr__(self, "turns", MappingProxyType(dict(self.turns)))
        object.__setattr__(self, "coverage", MappingProxyType(dict(self.coverage)))

    def contains(self, turn_id: str, message_id: str) -> bool:
        turn = self.turns.get(turn_id)
        return turn is not None and message_id in turn.messages

    def tool_result(self, turn_id: str, call_id: str) -> str | None:
        turn = self.turns.get(turn_id)
        return turn.tool_results.get(call_id) if turn else None

    def state_proof(self, namespace: str, key: str, revision: int) -> dict[str, Any]:
        for owner_turn, proof in reversed(self.coverage.get((namespace, key), ())):
            proof_turn = proof.get("turn_id", owner_turn)
            if proof.get("revision") == revision and self.contains(proof_turn, proof.get("message_id")):
                return {**dict(proof), "turn_id": proof_turn}
        return {}
