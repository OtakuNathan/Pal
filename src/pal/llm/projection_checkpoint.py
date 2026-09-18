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


def snapshot_projection(session: EndpointProjectionSession) -> dict[str, Any]:
    """Serialize the session's durable continuation state (derived chunks excluded)."""

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
        "frontier": {
            "history_epoch": session.frontier.history_epoch,
            "block_sequence": session.frontier.block_sequence,
            "prefix_digest": session.frontier.prefix_digest,
        },
        "native_records": [
            {
                "attempt_id": attempt_id,
                "payload_json": record["payload_json"],
                "call_ids": list(record["call_ids"]),
            }
            for attempt_id, record in sorted(session.native_by_attempt.items())
        ],
        "committed_attempts": [
            {
                "attempt_id": receipt.attempt.attempt_id,
                "before": {
                    "history_epoch": receipt.append.before.history_epoch,
                    "block_sequence": receipt.append.before.block_sequence,
                    "prefix_digest": receipt.append.before.prefix_digest,
                },
                "after": {
                    "history_epoch": receipt.append.after.history_epoch,
                    "block_sequence": receipt.append.after.block_sequence,
                    "prefix_digest": receipt.append.after.prefix_digest,
                },
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
    """

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

    # Joint-consistency gate: the projection cannot cover more history than
    # the L1 snapshot restored in the same transaction.  A frontier ahead of
    # the semantic cursor means native and IR come from different
    # generations — refuse instead of stitching (PLAN §8.2).
    if (
        frontier.history_epoch > l1_history_cursor.history_epoch
        or (
            frontier.history_epoch == l1_history_cursor.history_epoch
            and frontier.block_sequence > l1_history_cursor.block_sequence
        )
    ):
        raise ProjectionCheckpointError(
            "projection frontier is ahead of the L1 history cursor; "
            "refusing to combine mismatched checkpoint generations"
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
    for item in committed_raw:
        if not isinstance(item, Mapping):
            raise ProjectionCheckpointError("committed attempt entry is not an object")
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
                    owner_fence=OwnerFence(0),
                    attempt_id=str(item.get("attempt_id") or ""),
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
        committed_attempts[receipt.attempt.attempt_id] = receipt

    # Install atomically: everything validated above; these assignments are
    # the only visible restore boundary.
    session.bind(binding)
    session.identity = ProjectionIdentity(
        session=session.session_id, binding=binding, projection_generation=generation
    )
    session.frontier = frontier
    session.native_by_attempt = {
        record["attempt_id"]: {
            "payload_json": record["payload_json"],
            "call_ids": list(record["call_ids"]),
            "endpoint_id": binding.endpoint_id,
            "model_id": binding.model_id,
        }
        for record in native_records
    }
    # Receipt ledger survives restart: idempotent replays of pre-restart
    # receipts stay no-ops and conflicting ones stay refused (PLAN 8.2).
    session._committed_attempts = committed_attempts
    _ = OwnerFence(0)  # fence bump happens in the runtime when it reowns
    return True
