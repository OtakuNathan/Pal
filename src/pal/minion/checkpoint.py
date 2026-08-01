from __future__ import annotations

from typing import Any, Mapping

from pal.llm.serde import message_from_payload, message_to_payload
from pal.memory.compact import normalize_l1_transcript
from pal.memory.service import InMemoryL1Store
from pal.memory.turn_ir import L1TurnIR, L1TurnState
from pal.minion.v2.contracts import PermanentEffectError


AGENT_SESSION_CHECKPOINT_SCHEMA_VERSION = "5"

_LEGACY_L1_FIELDS = frozenset(
    {
        "active_tool_protocol_messages",
        "l1_items",
        "l1_protocol_committed_count",
        "request_snapshot",
        "tool_delivery_records",
    }
)


class AgentSessionCheckpointError(PermanentEffectError):
    """A deterministic continuation defect that unchanged retries cannot heal."""


def normalize_agent_session_checkpoint(
    checkpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the current checkpoint shape or migrate the legacy L1 shape.

    The pre-IR and IR checkpoints accidentally shared schema version ``5``.
    Shape therefore has to participate in admission: a checkpoint containing
    ``l1_items`` is migrated, never relabelled, and an admitted current
    checkpoint must contain ``l1_turns``.
    """

    value = dict(checkpoint)
    raw_turns = value.get("l1_turns")
    if isinstance(raw_turns, list):
        if str(value.get("schema_version") or "") != AGENT_SESSION_CHECKPOINT_SCHEMA_VERSION:
            raise AgentSessionCheckpointError(
                "manager-selected agent continuation uses an unsupported checkpoint schema"
            )
        _validate_l1_turn_payloads(raw_turns)
        for field in _LEGACY_L1_FIELDS:
            value.pop(field, None)
        return value

    raw_items = value.get("l1_items")
    if not isinstance(raw_items, list):
        raise AgentSessionCheckpointError(
            "manager-selected agent continuation contains neither current nor migratable L1"
        )

    store = InMemoryL1Store()
    try:
        store.items = [normalize_l1_transcript(item) for item in raw_items]
    except (TypeError, ValueError, RuntimeError) as exc:
        raise AgentSessionCheckpointError(
            "manager-selected agent continuation contains invalid legacy L1"
        ) from exc

    value["schema_version"] = AGENT_SESSION_CHECKPOINT_SCHEMA_VERSION
    value["l1_turns"] = [
        {
            "turn_id": turn.turn_id,
            "state": turn.state.value,
            "revision": turn.revision,
            "metadata": dict(turn.metadata),
            "messages": [message_to_payload(message) for message in turn.messages],
        }
        for turn in store.turns.turns
    ]
    for field in _LEGACY_L1_FIELDS:
        value.pop(field, None)
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
