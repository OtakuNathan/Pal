from __future__ import annotations

from typing import Any, Mapping

from pal.llm.serde import message_from_payload
from pal.memory.turn_ir import L1TurnIR, L1TurnState
from pal.minion.v2.contracts import PermanentEffectError


AGENT_SESSION_CHECKPOINT_SCHEMA_VERSION = "6"


class AgentSessionCheckpointError(PermanentEffectError):
    """A deterministic continuation defect that unchanged retries cannot heal."""


def normalize_agent_session_checkpoint(
    checkpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the v27 logical-coroutine checkpoint envelope.

    Fresh runtime cutover makes old continuations intentionally unreachable.
    L1 is the only conversational truth source; Manager treats the remaining
    role payload as opaque and never reconstructs a second transcript.
    """

    value = dict(checkpoint)
    if str(value.get("schema_version") or "") != AGENT_SESSION_CHECKPOINT_SCHEMA_VERSION:
        raise AgentSessionCheckpointError(
            "manager-selected agent continuation uses an unsupported checkpoint schema"
        )
    raw_turns = value.get("l1_turns")
    if not isinstance(raw_turns, list):
        raise AgentSessionCheckpointError(
            "manager-selected agent continuation contains no L1 truth source"
        )
    _validate_l1_turn_payloads(raw_turns)
    return value


def _validate_l1_turn_payloads(raw_turns: list[Any]) -> None:
    for index, raw_turn in enumerate(raw_turns):
        if not isinstance(raw_turn, Mapping):
            raise AgentSessionCheckpointError(
                f"manager-selected agent continuation contains invalid L1 turn at index {index}"
            )
        if not str(raw_turn.get("turn_id") or "").strip():
            raise AgentSessionCheckpointError(
                f"manager-selected agent continuation contains an unnamed L1 turn at index {index}"
            )
        raw_messages = raw_turn.get("messages")
        if not isinstance(raw_messages, list) or any(
            not isinstance(message, Mapping) for message in raw_messages
        ):
            raise AgentSessionCheckpointError(
                f"manager-selected agent continuation contains invalid L1 messages at index {index}"
            )
        try:
            L1TurnIR(
                turn_id=str(raw_turn.get("turn_id") or ""),
                state=L1TurnState(str(raw_turn.get("state") or "active")),
                revision=int(raw_turn.get("revision") or 0),
                metadata=dict(raw_turn.get("metadata") or {}),
                messages=tuple(
                    message_from_payload(message)
                    for message in raw_messages
                ),
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            raise AgentSessionCheckpointError(
                f"manager-selected agent continuation contains invalid L1 turn at index {index}"
            ) from exc
