from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping

from pal.foundation import HeatLevel, HeatState
from pal.llm.ir import LLMMessageIR, MessageRole, MessageState, ReasoningPartIR
from pal.llm.serde import message_from_payload, message_to_payload
from pal.memory.contracts import L2Entry
from pal.memory.service import MemoryService
from pal.memory.turn_ir import L1TurnIR, L1TurnState, L1TurnStore
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR


MEMORY_RUNTIME_STATE_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class _PreparedMemoryState:
    turns: L1TurnStore
    entries: dict[str, L2Entry]
    top_of_mind_refs: tuple[str, ...]
    heat: dict[str, HeatState]


@dataclass
class MemoryRuntimeStatePort:
    service: MemoryService
    module_id: str = "memory"
    schema_version: str = MEMORY_RUNTIME_STATE_SCHEMA_VERSION
    state_order: int = 100

    def snapshot_state(self) -> Mapping[str, Any]:
        return {
            "l1_turns": [
                {
                    "turn_id": turn.turn_id,
                    "state": turn.state.value,
                    "revision": turn.revision,
                    "metadata": dict(turn.metadata),
                    "messages": [message_to_payload(message) for message in turn.messages],
                }
                for turn in self.service.l1_store.turns.turns
            ],
            "l2_entries": [_entry_to_payload(entry) for entry in self.service.l2_store.items.values()],
            "l2_top_of_mind_refs": list(self.service.l2_store.top_of_mind_refs),
            "l2_heat": {
                key: {
                    "key": state.key,
                    "heat_level": state.heat_level.value,
                    "hot_ttl": state.hot_ttl,
                    "ghost_ttl": state.ghost_ttl,
                    "renewal_count": state.renewal_count,
                }
                for key, state in self.service.l2_store.heat_registry.items()
            },
        }

    def prepare_restore_state(self, payload: Mapping[str, Any]) -> _PreparedMemoryState:
        value = dict(payload)
        allowed_fields = {
            "l1_turns",
            "l2_entries",
            "l2_top_of_mind_refs",
            "l2_heat",
        }
        if extras := sorted(set(value) - allowed_fields):
            raise ValueError(
                f"memory runtime snapshot has unknown fields: {extras}"
            )
        turns = L1TurnStore()
        turn_ids: set[str] = set()
        for raw in list(value.get("l1_turns") or ()):
            if not isinstance(raw, Mapping):
                raise ValueError("memory runtime snapshot contains an invalid L1 turn")
            turn = _turn_from_payload(dict(raw))
            if not turn.turn_id or turn.turn_id in turn_ids:
                raise ValueError(
                    "memory runtime snapshot contains a duplicate/empty L1 turn"
                )
            turn_ids.add(turn.turn_id)
            turns.turns.append(turn)
        entries: dict[str, L2Entry] = {}
        for raw in list(value.get("l2_entries") or ()):
            if not isinstance(raw, Mapping):
                raise ValueError("memory runtime snapshot contains an invalid L2 entry")
            entry = _entry_from_payload(dict(raw))
            if not entry.entry_id or entry.entry_id in entries:
                raise ValueError(
                    "memory runtime snapshot contains a duplicate/empty L2 entry"
                )
            entries[entry.entry_id] = entry
        top_of_mind_refs = tuple(
            str(item) for item in list(value.get("l2_top_of_mind_refs") or ())
        )
        if len(set(top_of_mind_refs)) != len(top_of_mind_refs) or any(
            not item or item not in entries for item in top_of_mind_refs
        ):
            raise ValueError(
                "memory runtime snapshot contains invalid top-of-mind references"
            )
        heat: dict[str, HeatState] = {}
        for key, raw in dict(value.get("l2_heat") or {}).items():
            if not isinstance(raw, Mapping) or str(key) not in entries:
                raise ValueError(
                    "memory runtime snapshot contains invalid heat state"
                )
            item = dict(raw)
            if str(item.get("key") or key) != str(key):
                raise ValueError(
                    "memory runtime snapshot heat identity mismatch"
                )
            heat[str(key)] = HeatState(
                key=str(item.get("key") or key),
                heat_level=HeatLevel(str(item.get("heat_level") or "dormant")),
                hot_ttl=max(0, int(item.get("hot_ttl") or 0)),
                ghost_ttl=max(0, int(item.get("ghost_ttl") or 0)),
                renewal_count=max(0, int(item.get("renewal_count") or 0)),
            )
        return _PreparedMemoryState(
            turns=turns,
            entries=entries,
            top_of_mind_refs=top_of_mind_refs,
            heat=heat,
        )

    def install_prepared_state(self, prepared: _PreparedMemoryState) -> None:
        # All construction and validation happened in prepare_restore_state.
        # These assignments are the only visible restore boundary.
        self.service.l1_store.turns = prepared.turns
        self.service.l2_store.items = prepared.entries
        self.service.l2_store.top_of_mind_refs = list(prepared.top_of_mind_refs)
        self.service.l2_store.heat_registry = prepared.heat

    def reset_state(self, reason: str) -> None:
        _ = reason
        self.service.soft_reset()


def _turn_from_payload(value: Mapping[str, Any]) -> L1TurnIR:
    allowed_fields = {"turn_id", "state", "revision", "metadata", "messages"}
    if extras := sorted(set(value) - allowed_fields):
        raise ValueError(f"runtime L1 turn has unknown fields: {extras}")
    raw_messages = value.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("runtime L1 turn messages must be a list")
    if any(not isinstance(item, Mapping) for item in raw_messages):
        raise ValueError("runtime L1 turn contains an invalid message")
    messages = tuple(message_from_payload(item) for item in raw_messages)
    normalized, repaired = _normalize_tool_protocol(messages)
    requested_state = L1TurnState(str(value.get("state") or "active"))
    state = (
        requested_state
        if not repaired
        else L1TurnState.INTERRUPTED
    )
    if state != L1TurnState.ACTIVE:
        normalized = _migrate_closed_turn_projection(normalized)
    raw_metadata = value.get("metadata") or {}
    if not isinstance(raw_metadata, Mapping):
        raise ValueError("runtime L1 turn metadata must be an object")
    metadata = dict(raw_metadata)
    if repaired:
        metadata["interrupt_reason"] = "runtime snapshot protocol normalization"
    return L1TurnIR(
        turn_id=str(value.get("turn_id") or ""),
        state=state,
        revision=max(0, int(value.get("revision") or 0)),
        metadata=metadata,
        messages=normalized,
    )


def _migrate_closed_turn_projection(
    messages: tuple[LLMMessageIR, ...],
) -> tuple[LLMMessageIR, ...]:
    """Upgrade pre-retirement checkpoints to the current closed-turn IR."""

    migrated: list[LLMMessageIR] = []
    for message in messages:
        parts = tuple(
            part.retire() if isinstance(part, ToolResultIR) else part
            for part in message.parts
            if not isinstance(part, ReasoningPartIR)
        )
        migrated.append(
            replace(
                message,
                parts=parts,
                state=MessageState.COMPLETE,
                replay=None,
            )
        )
    return tuple(migrated)


def _normalize_tool_protocol(
    messages: tuple[LLMMessageIR, ...],
) -> tuple[tuple[LLMMessageIR, ...], bool]:
    call_counts: dict[str, int] = {}
    result_counts: dict[str, int] = {}
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallIR):
                call_counts[part.call_id] = call_counts.get(part.call_id, 0) + 1
            elif isinstance(part, ToolResultIR):
                result_counts[part.call_id] = result_counts.get(part.call_id, 0) + 1
    valid = {
        call_id
        for call_id, count in call_counts.items()
        if count == 1 and result_counts.get(call_id) == 1
    }
    repaired = any(count != 1 or result_counts.get(call_id) != 1 for call_id, count in call_counts.items())
    repaired = repaired or any(call_counts.get(call_id) != 1 or count != 1 for call_id, count in result_counts.items())
    normalized: list[LLMMessageIR] = []
    for message in messages:
        parts = tuple(
            part
            for part in message.parts
            if not isinstance(part, (ToolCallIR, ToolResultIR)) or part.call_id in valid
        )
        changed = parts != message.parts
        if message.state == MessageState.IN_PROGRESS:
            changed = True
            parts = tuple(
                part for part in parts if not isinstance(part, ReasoningPartIR)
            )
        if not parts and message.role in {MessageRole.ASSISTANT, MessageRole.TOOL}:
            repaired = repaired or changed
            continue
        normalized.append(
            replace(
                message,
                parts=parts,
                state=MessageState.COMPLETE,
                replay=None,
            )
            if changed or message.replay is not None
            else message
        )
        repaired = repaired or changed
    return tuple(normalized), repaired


def _entry_to_payload(entry: L2Entry) -> dict[str, Any]:
    value = asdict(entry)
    value.pop("heat_state", None)
    return value


def _entry_from_payload(value: Mapping[str, Any]) -> L2Entry:
    return L2Entry(
        entry_id=str(value.get("entry_id") or ""),
        kind=str(value.get("kind") or "memory"),
        scope=str(value.get("scope") or "task"),
        title=str(value.get("title") or ""),
        summary=str(value.get("summary") or ""),
        task_id=str(value["task_id"]) if value.get("task_id") is not None else None,
        source_kind=str(value.get("source_kind") or "l3_recall"),
        source_ref=str(value.get("source_ref") or ""),
        candidate_state=str(value.get("candidate_state") or "stable"),
        touched_at=str(value.get("touched_at") or ""),
        rendered=str(value.get("rendered") or ""),
        search_text=str(value.get("search_text") or ""),
        canonical_key=str(value["canonical_key"]) if value.get("canonical_key") is not None else None,
        dedupe_fingerprint=str(value["dedupe_fingerprint"]) if value.get("dedupe_fingerprint") is not None else None,
        payload=dict(value.get("payload") or {}),
    )
