"""Atomic checkpoint codec for the projection session (PLAN §8.2).

One logical snapshot couples the semantic history cursor with the active
session's binding/generation/frontier/native records.  Restore refuses
mixed generations and truncated sections — never stitches newer native onto
older IR.  Snapshots without a projection section load as LEGACY: the
session starts a fresh generation 0 lineage and the old per-message
ReplayEnvelope data stays untouched (read-only compatibility).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from pal.llm.ir import WireShape
from pal.llm.projection_contracts import (
    AppendReceipt,
    AttemptKey,
    EndpointBinding,
    HistoryCommitReceipt,
    HistoryCursor,
    LogicalSessionId,
    OwnerFence,
    ProjectionContractError,
    ProjectionIdentity,
)
from pal.llm.projection_session import EndpointProjectionSession
from pal.shared.json_values import freeze_json_mapping, thaw_json

__all__ = [
    "PROJECTION_CHECKPOINT_SCHEMA_VERSION",
    "ProjectionCheckpointError",
    "snapshot_projection",
    "restore_projection",
    "legacy_projection_payload",
]

PROJECTION_CHECKPOINT_SCHEMA_VERSION = "projection-checkpoint-1"

_PROJECTION_SECTION_KEY = "projection"


class ProjectionCheckpointError(ValueError):
    """A projection checkpoint could not be validated or restored."""


def _cursor_fields(cursor: HistoryCursor) -> dict[str, Any]:
    return {
        "history_epoch": cursor.history_epoch,
        "block_sequence": cursor.block_sequence,
        "prefix_digest": cursor.prefix_digest,
    }


def snapshot_projection(session: EndpointProjectionSession) -> dict[str, Any]:
    """Serialize the session's durable continuation state.

    Derived chunks ARE persisted as wire items (review R5): a restored
    session must rebuild its materialized prefix, or the next request would
    silently drop every committed block while claiming coverage through a
    non-zero frontier.
    """

    if session.identity is None or session.binding is None:
        return {
            "schema_version": PROJECTION_CHECKPOINT_SCHEMA_VERSION,
            "scope": session.session_id.scope,
            "bound": False,
        }
    binding = session.binding
    return {
        "schema_version": PROJECTION_CHECKPOINT_SCHEMA_VERSION,
        "scope": session.session_id.scope,
        "bound": True,
        "binding": {
            "endpoint_id": binding.endpoint_id,
            "model_id": binding.model_id,
            "wire_shape": binding.wire_shape.value,
            "endpoint_spec_revision": binding.endpoint_spec_revision,
            "continuation_policy_version": binding.continuation_policy_version,
            "config_fingerprint": binding.config_fingerprint,
        },
        "projection_generation": session.identity.projection_generation,
        "frontier": _cursor_fields(session.frontier),
        # Validated encode profile for this lineage (review B1): a restored
        # session must encode with the SAME capabilities or a capability-
        # restricted endpoint would regain unsupported optional fields.
        "capabilities": thaw_json(session._capabilities),
        # The CURRENT write fence is a separate fact from every historical
        # receipt's SOURCE fence (review B4): cancelled rounds advance it
        # without ever producing a receipt, so it must be persisted itself.
        "owner_fence": session._owner_fence,
        "pending_wire_tail": thaw_json(list(session._pending_wire_tail)),
        "committed_head_system": thaw_json(list(session._committed_head_system)),
        "chunks": [
            {
                "attempt_id": chunk.round_attempt_id,
                "cursor_before": _cursor_fields(chunk.cursor_before),
                "cursor_after": _cursor_fields(chunk.cursor_after),
                "items": thaw_json(list(chunk.items)),
                "prefix_digest": chunk.prefix_digest,
            }
            for chunk in session.chunks
        ],
        "native_records": [
            {
                "attempt_id": attempt_id,
                "payload_json": record["payload_json"],
                "call_ids": list(record["call_ids"]),
            }
            for attempt_id, record in sorted(session.native_by_attempt.items())
            # Authoritative snapshot scope is COMMITTED material only
            # (review B2): a draft native attached to a still-open or
            # cancelled round never belongs in a checkpoint.
            if attempt_id in session._committed_attempts
        ],
        "committed_attempts": [
            {
                "attempt_id": receipt.attempt.attempt_id,
                "owner_fence": receipt.attempt.owner_fence.fence,
                "before": _cursor_fields(receipt.append.before),
                "after": _cursor_fields(receipt.append.after),
                "block_count": receipt.append.block_count,
                "closed_call_ids": list(receipt.closed_call_ids),
                "native_committed": receipt.native_committed,
            }
            for attempt_id, receipt in sorted(session._committed_attempts.items())
        ],
    }


def legacy_projection_payload() -> dict[str, Any]:
    """Section value for pre-projection snapshots: explicit legacy marker."""

    return {"schema_version": PROJECTION_CHECKPOINT_SCHEMA_VERSION, "bound": False}


@dataclass(frozen=True)
class _PreparedProjection:
    scope: LogicalSessionId
    binding: EndpointBinding | None
    generation: int
    frontier: HistoryCursor
    native_records: tuple[dict[str, Any], ...]


def restore_projection(
    payload: Mapping[str, Any],
    *,
    l1_history_cursor: HistoryCursor,
    session: EndpointProjectionSession,
) -> bool:
    """Validate and install a projection snapshot onto ``session``.

    ``l1_history_cursor`` is the semantic history position restored in the
    SAME checkpoint transaction.  Returns True when a bound lineage was
    restored, False for a legacy/unbound section (fresh generation 0).

    Refusals (mixed generations, schema mismatch, frontier beyond the L1
    cursor, duplicate native records) raise before any state is touched.
    The target session must be PRISTINE (review B3): restore installs a
    lineage wholesale or refuses — it never merges into an existing one,
    and a legacy/unbound section (which promises a fresh generation-0
    lineage) must not leave old state reachable behind a ``False`` return.
    """

    if (
        session.identity is not None
        or session._active is not None
        or session.chunks
        or session.native_by_attempt
        or session._committed_attempts
        or session._pending_wire_tail
        or session._committed_head_system
        or session.frontier != HistoryCursor.initial()
    ):
        raise ProjectionCheckpointError(
            "restore_projection requires a pristine session target; refusing "
            "to install over existing projection state"
        )
    section = payload.get(_PROJECTION_SECTION_KEY)
    if section is None:
        # Legacy snapshot: fresh lineage; old ReplayEnvelope messages are the
        # compatibility layer and remain authoritative for their messages.
        session.retired = False
        return False
    if not isinstance(section, Mapping):
        raise ProjectionCheckpointError("projection section is not an object")
    if section.get("schema_version") != PROJECTION_CHECKPOINT_SCHEMA_VERSION:
        raise ProjectionCheckpointError(
            f"projection checkpoint schema {section.get('schema_version')!r} is not supported"
        )
    if not section.get("bound"):
        session.retired = False
        return False

    scope = str(section.get("scope") or "").strip()
    if not scope or scope != session.session_id.scope:
        raise ProjectionCheckpointError("projection checkpoint scope mismatch")

    binding_raw = section.get("binding")
    if not isinstance(binding_raw, Mapping):
        raise ProjectionCheckpointError("projection checkpoint has no binding")
    try:
        binding = EndpointBinding(
            endpoint_id=str(binding_raw.get("endpoint_id") or ""),
            model_id=str(binding_raw.get("model_id") or ""),
            wire_shape=WireShape(str(binding_raw.get("wire_shape") or "")),
            endpoint_spec_revision=str(binding_raw.get("endpoint_spec_revision") or ""),
            continuation_policy_version=str(
                binding_raw.get("continuation_policy_version") or ""
            ),
            config_fingerprint=str(binding_raw.get("config_fingerprint") or ""),
        )
    except ProjectionContractError as exc:
        raise ProjectionCheckpointError(f"projection binding is invalid: {exc}") from exc

    frontier_raw = section.get("frontier")
    if not isinstance(frontier_raw, Mapping):
        raise ProjectionCheckpointError("projection checkpoint has no frontier")
    try:
        frontier = HistoryCursor(
            history_epoch=int(frontier_raw.get("history_epoch", -1)),
            block_sequence=int(frontier_raw.get("block_sequence", -1)),
            prefix_digest=str(frontier_raw.get("prefix_digest") or ""),
        )
    except (ProjectionContractError, TypeError, ValueError) as exc:
        raise ProjectionCheckpointError(f"projection frontier is invalid: {exc}") from exc

    # Joint-consistency gate (review R6): the projection and the L1 history
    # must come from the SAME durable snapshot.  "Not newer than L1" alone
    # proves nothing — equal position with a different digest, or a projection
    # from a pre-compaction epoch, are both mismatched generations.
    if frontier.history_epoch != l1_history_cursor.history_epoch:
        raise ProjectionCheckpointError(
            "projection frontier epoch differs from the L1 history cursor; "
            "refusing to stitch mismatched checkpoint generations "
            "(including pre-compaction native lineages)"
        )
    if frontier.block_sequence > l1_history_cursor.block_sequence:
        raise ProjectionCheckpointError(
            "projection frontier is ahead of the L1 history cursor; "
            "refusing to combine mismatched checkpoint generations"
        )
    if (
        frontier.block_sequence == l1_history_cursor.block_sequence
        and frontier.prefix_digest != l1_history_cursor.prefix_digest
    ):
        raise ProjectionCheckpointError(
            "projection frontier matches the L1 position but not its digest; "
            "same length is not an identity proof"
        )

    records_raw = section.get("native_records")
    if not isinstance(records_raw, (list, tuple)):
        raise ProjectionCheckpointError("projection checkpoint native_records is invalid")
    seen_attempts: set[str] = set()
    native_records: list[dict[str, Any]] = []
    for item in records_raw:
        if not isinstance(item, Mapping):
            raise ProjectionCheckpointError("native record is not an object")
        attempt_id = str(item.get("attempt_id") or "").strip()
        payload_json = str(item.get("payload_json") or "")
        if not attempt_id or not payload_json:
            raise ProjectionCheckpointError("native record is truncated")
        if attempt_id in seen_attempts:
            raise ProjectionCheckpointError(f"duplicate native record: {attempt_id}")
        seen_attempts.add(attempt_id)
        try:
            json.loads(payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProjectionCheckpointError(
                f"native record {attempt_id} payload is not valid JSON"
            ) from exc
        native_records.append(
            {
                "attempt_id": attempt_id,
                "payload_json": payload_json,
                "call_ids": [str(call) for call in (item.get("call_ids") or ())],
            }
        )

    generation = int(section.get("projection_generation", 0))
    if generation < 0:
        raise ProjectionCheckpointError("negative projection generation")

    committed_raw = section.get("committed_attempts") or ()
    if not isinstance(committed_raw, (list, tuple)):
        raise ProjectionCheckpointError("committed_attempts is invalid")
    committed_attempts: dict[str, HistoryCommitReceipt] = {}
    seen_committed: set[str] = set()
    for item in committed_raw:
        if not isinstance(item, Mapping):
            raise ProjectionCheckpointError("committed attempt entry is not an object")
        attempt_id = str(item.get("attempt_id") or "")
        if not attempt_id:
            raise ProjectionCheckpointError("committed attempt entry has no attempt id")
        if attempt_id in seen_committed:
            raise ProjectionCheckpointError(
                f"duplicate committed attempt entry: {attempt_id}"
            )
        seen_committed.add(attempt_id)
        try:
            before = HistoryCursor(
                history_epoch=int(item["before"]["history_epoch"]),
                block_sequence=int(item["before"]["block_sequence"]),
                prefix_digest=str(item["before"]["prefix_digest"]),
            )
            after = HistoryCursor(
                history_epoch=int(item["after"]["history_epoch"]),
                block_sequence=int(item["after"]["block_sequence"]),
                prefix_digest=str(item["after"]["prefix_digest"]),
            )
            receipt = HistoryCommitReceipt(
                attempt=AttemptKey(
                    identity=ProjectionIdentity(
                        session=session.session_id,
                        binding=binding,
                        projection_generation=generation,
                    ),
                    # Source owner identity is preserved verbatim (review R6):
                    # rewriting history to fence 0 would turn an idempotent
                    # receipt replay into a false "conflicting receipt".
                    owner_fence=OwnerFence(int(item.get("owner_fence", 0) or 0)),
                    attempt_id=attempt_id,
                ),
                append=AppendReceipt(
                    before=before,
                    after=after,
                    block_count=int(item.get("block_count", 0)),
                ),
                closed_call_ids=tuple(str(call) for call in (item.get("closed_call_ids") or ())),
                native_committed=bool(item.get("native_committed")),
            )
        except (KeyError, TypeError, ValueError, ProjectionContractError) as exc:
            raise ProjectionCheckpointError(
                f"committed attempt entry is invalid: {exc}"
            ) from exc
        committed_attempts[attempt_id] = receipt

    # Chunk chain (review R5/R6): rebuild the materialized prefix and verify
    # the chain is contiguous from the initial cursor to the frontier.
    chunks_raw = section.get("chunks") or ()
    if not isinstance(chunks_raw, (list, tuple)):
        raise ProjectionCheckpointError("projection chunks section is invalid")
    from pal.llm.projection_session import ProjectionChunk

    chunks: list[ProjectionChunk] = []
    prefix_items: list[dict] = []
    expected_before = HistoryCursor.initial()
    for raw in chunks_raw:
        if not isinstance(raw, Mapping):
            raise ProjectionCheckpointError("chunk entry is not an object")
        try:
            cursor_before = HistoryCursor(
                history_epoch=int(raw["cursor_before"]["history_epoch"]),
                block_sequence=int(raw["cursor_before"]["block_sequence"]),
                prefix_digest=str(raw["cursor_before"]["prefix_digest"]),
            )
            cursor_after = HistoryCursor(
                history_epoch=int(raw["cursor_after"]["history_epoch"]),
                block_sequence=int(raw["cursor_after"]["block_sequence"]),
                prefix_digest=str(raw["cursor_after"]["prefix_digest"]),
            )
        except (KeyError, TypeError, ValueError, ProjectionContractError) as exc:
            raise ProjectionCheckpointError(f"chunk cursor is invalid: {exc}") from exc
        if cursor_before != expected_before:
            raise ProjectionCheckpointError(
                "chunk chain is not contiguous; refusing to stitch a broken prefix"
            )
        items_raw = raw.get("items")
        if not isinstance(items_raw, (list, tuple)) or not all(
            isinstance(entry, Mapping) for entry in items_raw
        ):
            raise ProjectionCheckpointError("chunk items are invalid")
        # Ownership transfer at the restore boundary (review F5): deep-copy
        # the caller's snapshot data ONCE here.  Shallow dict() copies would
        # alias nested content lists back to the caller's mutable checkpoint,
        # letting post-restore mutations rewrite the private prefix while
        # frontier/digest stay unchanged.
        items = tuple(json.loads(json.dumps(list(items_raw))))
        chunks.append(
            ProjectionChunk(
                round_attempt_id=str(raw.get("attempt_id") or ""),
                cursor_before=cursor_before,
                cursor_after=cursor_after,
                items=tuple(freeze_json_mapping(item) for item in items),
                prefix_digest=str(raw.get("prefix_digest") or ""),
            )
        )
        # The restored private prefix holds the deep-copied owned dicts (the
        # public chunk snapshot is deep-frozen); the two share nothing.
        prefix_items.extend(dict(item) for item in items)
        expected_before = cursor_after
    pending_raw = section.get("pending_wire_tail") or ()
    if not isinstance(pending_raw, (list, tuple)):
        raise ProjectionCheckpointError("pending_wire_tail section is invalid")
    pending_wire_tail = json.loads(json.dumps(list(pending_raw)))
    head_system_raw = section.get("committed_head_system")
    # Absent key = pre-G2-persistence snapshot: empty is the only faithful
    # reconstruction (the prototype never persisted hoisted head parts).
    if head_system_raw is None:
        head_system_raw = ()
    if not isinstance(head_system_raw, (list, tuple)) or not all(
        isinstance(part, Mapping) for part in head_system_raw
    ):
        raise ProjectionCheckpointError("committed_head_system section is invalid")
    committed_head_system = json.loads(json.dumps(list(head_system_raw)))
    if chunks:
        if expected_before != frontier:
            raise ProjectionCheckpointError(
                "chunk chain does not end at the frontier; refusing restore"
            )
        if len({chunk.round_attempt_id for chunk in chunks}) != len(chunks):
            raise ProjectionCheckpointError("duplicate attempt in chunk chain")
        chunk_receipts = {chunk.round_attempt_id for chunk in chunks}
        missing = chunk_receipts - set(committed_attempts)
        if missing:
            raise ProjectionCheckpointError(
                f"chunks without a committed receipt: {sorted(missing)}"
            )
    elif frontier.block_sequence or committed_attempts:
        raise ProjectionCheckpointError(
            "non-empty frontier has no materialized chunks; refusing to "
            "restore a projection that claims coverage it cannot rebuild"
        )

    # CURRENT write fence (review B4/C2), validated BEFORE any state is
    # installed: FIELD PRESENCE distinguishes a legacy snapshot from a
    # corrupt new-format one.  Missing → the declared compatibility
    # fallback (highest committed SOURCE fence); present but malformed →
    # the snapshot is corrupt, fail closed here — raising after the install
    # block would leave a half-restored target behind the exception.
    legacy_fence = max(
        (receipt.attempt.owner_fence.fence for receipt in committed_attempts.values()),
        default=0,
    )
    if "owner_fence" in section:
        persisted_fence = section["owner_fence"]
        if (
            not isinstance(persisted_fence, int)
            or isinstance(persisted_fence, bool)
            or persisted_fence < 0
        ):
            raise ProjectionCheckpointError(
                f"projection owner_fence is present but invalid: "
                f"{persisted_fence!r}"
            )
        restored_owner_fence = max(persisted_fence, legacy_fence)
    else:
        # Pre-B4 snapshot: the field did not exist; the most faithful
        # reconstruction is still the highest committed source fence.
        restored_owner_fence = legacy_fence

    # Install atomically: everything validated above; these assignments are
    # the only visible restore boundary.
    capabilities_raw = section.get("capabilities")
    # Pre-B1 snapshots lack the field: empty capabilities is the only
    # faithful reconstruction for them (prototype snapshots only).
    if capabilities_raw is None:
        capabilities_raw = {}
    if not isinstance(capabilities_raw, Mapping):
        raise ProjectionCheckpointError("projection capabilities section is invalid")
    session.bind(binding, capabilities=capabilities_raw)
    session.identity = ProjectionIdentity(
        session=session.session_id, binding=binding, projection_generation=generation
    )
    session.frontier = frontier
    session.chunks = tuple(chunks)
    session._prefix_items = prefix_items
    session._frontier_item_count = len(prefix_items)
    session._pending_wire_tail = pending_wire_tail
    session._committed_head_system = committed_head_system
    session.native_by_attempt = {
        record["attempt_id"]: {
            "payload_json": record["payload_json"],
            "call_ids": list(record["call_ids"]),
            "endpoint_id": binding.endpoint_id,
            "model_id": binding.model_id,
        }
        for record in native_records
    }
    # Receipt ledger survives restart with its SOURCE owner fence intact:
    # idempotent replays of pre-restart receipts stay no-ops and conflicting
    # ones stay refused (PLAN §8.2, review R6).  The new worker's write
    # permission is a separate, higher fence it brings itself.
    session._committed_attempts = committed_attempts
    # Pre-validated above the install boundary (review C2).
    session._owner_fence = restored_owner_fence
    return True
