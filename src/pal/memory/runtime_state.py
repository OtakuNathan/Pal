from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping

from pal.foundation import HeatLevel, HeatState
from pal.llm.ir import LLMMessageIR, MessageRole, MessageState, ReasoningPartIR
from pal.llm.serde import message_from_payload, message_to_payload
from pal.memory.contracts import CompactionReceipt, L2Entry
from pal.memory.service import MemoryService
from pal.memory.turn_ir import L1TurnIR, L1TurnState, L1TurnStore, _repair_replay
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
from pal.shared.json_values import thaw_json


MEMORY_RUNTIME_STATE_SCHEMA_VERSION = "4"

_RECEIPT_FIELDS = (
    "op_id", "status", "epoch_before", "epoch_after", "summary_source_id",
    "source_stamp", "successor_turn_id", "successor_revision",
    "removed_turn_ids", "removed_result_refs", "cleanup_status", "created_at",
)


def _receipt_to_payload(receipt: CompactionReceipt) -> dict[str, Any]:
    return {
        "op_id": receipt.op_id,
        "status": receipt.status,
        "epoch_before": receipt.epoch_before,
        "epoch_after": receipt.epoch_after,
        "summary_source_id": receipt.summary_source_id,
        "source_stamp": receipt.source_stamp,
        "successor_turn_id": receipt.successor_turn_id,
        "successor_revision": receipt.successor_revision,
        "removed_turn_ids": list(receipt.removed_turn_ids),
        "removed_result_refs": list(receipt.removed_result_refs),
        "cleanup_status": receipt.cleanup_status,
        "created_at": receipt.created_at,
    }


def _receipt_from_payload(value: Mapping[str, Any]) -> CompactionReceipt:
    item = dict(value)
    if extras := sorted(set(item) - set(_RECEIPT_FIELDS)):
        raise ValueError(f"compaction receipt has unknown fields: {extras}")
    op_id = str(item.get("op_id") or "").strip()
    status = str(item.get("status") or "").strip()
    stamp = str(item.get("source_stamp") or "").strip()
    if not op_id or not status or not stamp:
        raise ValueError("compaction receipt is missing required identity fields")
    integers: dict[str, int] = {}
    for key in ("epoch_before", "epoch_after", "successor_revision"):
        raw = item.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError(f"compaction receipt field {key} is invalid")
        integers[key] = raw
    for key in ("removed_turn_ids", "removed_result_refs"):
        raw = item.get(key)
        if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
            raise ValueError(f"compaction receipt field {key} is invalid")
    cleanup = str(item.get("cleanup_status") or "ok")
    if cleanup not in {"ok", "pending"}:
        raise ValueError("compaction receipt cleanup_status is invalid")
    return CompactionReceipt(
        op_id=op_id,
        status=status,
        epoch_before=integers["epoch_before"],
        epoch_after=integers["epoch_after"],
        summary_source_id=str(item.get("summary_source_id") or ""),
        source_stamp=stamp,
        successor_turn_id=str(item.get("successor_turn_id") or ""),
        successor_revision=integers["successor_revision"],
        removed_turn_ids=tuple(str(x) for x in item["removed_turn_ids"]),
        removed_result_refs=tuple(str(x) for x in item["removed_result_refs"]),
        cleanup_status=cleanup,
        created_at=str(item.get("created_at") or ""),
    )


@dataclass(frozen=True)
class _PreparedMemoryState:
    turns: L1TurnStore
    entries: dict[str, L2Entry]
    top_of_mind_refs: tuple[str, ...]
    heat: dict[str, HeatState]
    context_epoch: int = 0
    compaction_receipts: dict[str, CompactionReceipt] = field(default_factory=dict)
    # H02: (incarnation, cut, left_generation) — None means the payload
    # predates two-segment authority (explicit migration: fresh root).
    history_root: tuple[str, Any, int] | None = None


@dataclass
class MemoryRuntimeStatePort:
    service: MemoryService
    module_id: str = "memory"
    schema_version: str = MEMORY_RUNTIME_STATE_SCHEMA_VERSION
    readable_schema_versions = ("1", "2", "3", "4")
    state_order: int = 100

    def snapshot_state(self) -> Mapping[str, Any]:
        return {
            "l1_turns": [
                {
                    "turn_id": turn.turn_id,
                    "state": turn.state.value,
                    "revision": turn.revision,
                    "metadata": thaw_json(turn.metadata),
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
            "context_epoch": int(getattr(self.service, "context_epoch", 0) or 0),
            "compaction_receipts": {
                receipt.op_id: _receipt_to_payload(receipt)
                for receipt in getattr(
                    self.service, "compaction_receipts", {}
                ).values()
            },
            # H02: the two-segment authority travels with the turns — without
            # it a restored process would see every turn uncut and lose the
            # incarnation/left-generation chain the projections rebase on.
            # None only when this service never attached a root at all.
            "history_root": (
                self.service.history_root.owner_state()
                if getattr(self.service, "_history_root", None) is not None
                else None
            ),
        }

    def prepare_restore_state(self, payload: Mapping[str, Any]) -> _PreparedMemoryState:
        value = dict(payload)
        allowed_fields = {
            "l1_turns",
            "l2_entries",
            "l2_top_of_mind_refs",
            "l2_heat",
            "context_epoch",
            "compaction_receipts",
            "history_root",
        }
        if extras := sorted(set(value) - allowed_fields):
            raise ValueError(
                f"memory runtime snapshot has unknown fields: {extras}"
            )
        # R04/R05: a present-but-invalid epoch/receipt fails closed; a
        # genuinely old snapshot without these fields migrates explicitly.
        raw_epoch = value.get("context_epoch")
        if raw_epoch is None:
            context_epoch = 0
        elif isinstance(raw_epoch, bool) or not isinstance(raw_epoch, int) or raw_epoch < 0:
            raise ValueError("memory runtime snapshot context_epoch is invalid")
        else:
            context_epoch = raw_epoch
        receipts: dict[str, CompactionReceipt] = {}
        raw_receipts = value.get("compaction_receipts")
        if raw_receipts is None:
            raw_receipts = {}
        if not isinstance(raw_receipts, Mapping):
            raise ValueError(
                "memory runtime snapshot compaction_receipts is invalid"
            )
        for key, raw in raw_receipts.items():
            if not isinstance(raw, Mapping):
                raise ValueError(
                    "memory runtime snapshot contains an invalid compaction receipt"
                )
            receipt = _receipt_from_payload(raw)
            if receipt.op_id != str(key):
                raise ValueError(
                    "memory runtime snapshot receipt identity mismatch"
                )
            receipts[receipt.op_id] = receipt
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
            turns.append(turn)
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
            context_epoch=context_epoch,
            compaction_receipts=receipts,
            history_root=_prepared_history_root(
                value.get("history_root"), turns,
            ),
        )

    def install_prepared_state(self, prepared: _PreparedMemoryState) -> None:
        # All construction and validation happened in prepare_restore_state.
        # These assignments are the only visible restore boundary.
        self.service.l1_store.turns = prepared.turns
        self.service.l2_store.items = prepared.entries
        self.service.l2_store.top_of_mind_refs = list(prepared.top_of_mind_refs)
        self.service.l2_store.heat_registry = prepared.heat
        self.service.context_epoch = int(prepared.context_epoch)
        self.service.compaction_receipts = dict(prepared.compaction_receipts)
        if prepared.history_root is not None:
            from pal.memory.history_root import CutPosition

            incarnation, cut, left_generation = prepared.history_root
            # The store object was swapped above, so this access heals a
            # fresh root over the restored turns first; the persisted owner
            # state is then reinstated on top of it (H02).
            root = self.service.history_root
            root.restore_owner_state(
                incarnation=incarnation,
                cut=CutPosition(**cut),
                left_generation=left_generation,
            )
        else:
            # Explicit migration (v2-era payload / root never attached):
            # closed imported turns form the initial compressible history;
            # active work remains R and no transport receipt is invented.
            self.service._history_root = None
            self.service.history_root.promote()

    def reset_state(self, reason: str) -> None:
        _ = reason
        self.service.soft_reset()


def _prepared_history_root(
    raw: Any, turns: L1TurnStore,
) -> tuple[str, dict[str, int | str], int] | None:
    """Validate the persisted owner state against the prepared turns (H02).

    ``None`` (explicit migration) is returned only for payloads that
    predate two-segment authority or services that never attached a root;
    anything present must be complete and must fit the restored history,
    so a corrupted snapshot fails closed here — before any install.
    """

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("memory runtime snapshot history_root is invalid")
    value = dict(raw)
    allowed = {"incarnation", "left_generation", "cut", "abandoned_run"}
    if extras := sorted(set(value) - allowed):
        raise ValueError(
            f"memory runtime snapshot history_root has unknown fields: {extras}"
        )
    incarnation = str(value.get("incarnation") or "").strip()
    if not incarnation:
        raise ValueError("memory runtime snapshot history_root has no incarnation")
    raw_generation = value.get("left_generation")
    if (
        isinstance(raw_generation, bool)
        or not isinstance(raw_generation, int)
        or raw_generation < 0
    ):
        raise ValueError(
            "memory runtime snapshot history_root left_generation is invalid"
        )
    raw_cut = value.get("cut")
    if not isinstance(raw_cut, Mapping):
        raise ValueError("memory runtime snapshot history_root has no cut")
    cut = dict(raw_cut)
    cut_allowed = {"turn_count", "intra_messages", "revision", "cut_id"}
    if extras := sorted(set(cut) - cut_allowed):
        raise ValueError(
            f"memory runtime snapshot history_root cut has unknown fields: {extras}"
        )
    counters: dict[str, int] = {}
    for key in ("turn_count", "intra_messages", "revision"):
        item = cut.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError(
                f"memory runtime snapshot history_root cut {key} is invalid"
            )
        counters[key] = item
    cut_id = str(cut.get("cut_id") or "").strip()
    if not cut_id:
        raise ValueError("memory runtime snapshot history_root cut has no cut_id")
    # The abandoned_run record is diagnostics only (a live run at save time
    # is abandoned; the restored process starts with a free lane).  Accept
    # None or an object with a run_id; reject anything else structurally.
    abandoned = value.get("abandoned_run")
    if abandoned is not None and (
        not isinstance(abandoned, Mapping)
        or not str(abandoned.get("run_id") or "").strip()
    ):
        raise ValueError(
            "memory runtime snapshot history_root abandoned_run is invalid"
        )
    turn_total = len(turns.turns)
    if counters["turn_count"] > turn_total:
        raise ValueError(
            "memory runtime snapshot history_root cut exceeds the restored turns"
        )
    if counters["intra_messages"]:
        if counters["turn_count"] >= turn_total:
            raise ValueError(
                "memory runtime snapshot history_root intra cut has no boundary turn"
            )
        boundary = turns.turns[counters["turn_count"]]
        if counters["intra_messages"] > len(boundary.messages):
            raise ValueError(
                "memory runtime snapshot history_root intra cut splits "
                "messages that never existed"
            )
    return (
        incarnation,
        {
            "turn_count": counters["turn_count"],
            "intra_messages": counters["intra_messages"],
            "revision": counters["revision"],
            "cut_id": cut_id,
        },
        raw_generation,
    )


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
        normalized = _normalize_closed_turn_projection(normalized)
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


def _normalize_closed_turn_projection(
    messages: tuple[LLMMessageIR, ...],
) -> tuple[LLMMessageIR, ...]:
    """Restore closure without changing any accepted continuation content."""
    return tuple(replace(message, state=MessageState.COMPLETE) for message in messages)


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
                # Protocol content changed during restore normalization: the
                # wire replay envelope still carries the removed/modified
                # protocol items, and same-endpoint encoders prefer replay
                # over parts, which would smuggle dangling calls back onto
                # the wire. Repair only revoked calls in native replay;
                # retain accepted reasoning and original source evidence.
                replay=(
                    _repair_replay(message.replay, parts) if changed else message.replay
                ),
            )
            if changed
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
