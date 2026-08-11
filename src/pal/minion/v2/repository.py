from __future__ import annotations

import hashlib
import json
import sqlite3
import secrets
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

from pal.foundation import utc_now
from pal.minion.checkpoint import LogicalCoroutineCheckpointStore
from pal.minion.config import minion_db_path
from pal.minion.v2.artifacts import ArtifactRef
from pal.minion.v2.contracts import (
    ActionEnvelope,
    AggregateSnapshot,
    AggregateType,
    AggregateVersionConflict,
    DispatchResult,
    DomainEvent,
    LeaseConflict,
    LeaseGrant,
    StaleFencingToken,
)
from pal.minion.v2.engine import TransitionEngine
from pal.minion.v2.machines import build_default_transition_engine
from pal.minion.v2.schema import ensure_minion_v2_schema
from pal.minion.v2.role_protocol import (
    RoleAssignmentAction,
    RoleAssignmentRequest,
    RoleAssignmentState,
    RoleAttemptState,
    RoleSessionAction,
    RoleSessionState,
    RoleSubmissionReceipt,
    attempt_id,
    role_assignment_target,
    role_session_target,
)
from pal.minion.v2.role_contracts import RoleActivation
from pal.minion.v2.cycle_protocol import (
    NodeCycle,
    PlanCycle,
    node_cycle_from_mapping,
    plan_cycle_from_mapping,
)
from pal.minion.v2.graph_executor import GraphExecution
from pal.minion.v2.graph_protocol import GraphIR, graph_ir_from_mapping
from pal.shared.text_search import compile_jieba_fts_queries, jieba_fts_text


_QUEUED_STATES = {
    "ARCHITECT_QUEUED",
    "REVIEW_QUEUED",
    "QUEUED",
    "REPAIR_QUEUED",
    "STARTING",
}
_HUMAN_WAIT_STATES = {"HUMAN_REVIEW"}
_TERMINAL_WORKFLOW_STATES = {"COMPLETED", "REJECTED", "CANCELLED"}
_TASK_FTS_INDEX_VERSION = "jieba-v1"
@dataclass
class MinionV2Repository:
    runtime_root: Path
    engine: TransitionEngine = field(default_factory=build_default_transition_engine)
    _schema_ready: bool = field(default=False, init=False, repr=False)

    @property
    def db_path(self) -> Path:
        return minion_db_path(Path(self.runtime_root))

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path), timeout=30.0) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            ensure_minion_v2_schema(connection)
            self._ensure_task_fts_index_locked(connection)
        self._schema_ready = True

    def dispatch(
        self,
        action: ActionEnvelope,
        *,
        role_assignment_id: str = "",
        role_submission_payload_hash: str = "",
        _connection: sqlite3.Connection | None = None,
    ) -> DispatchResult:
        if bool(str(role_assignment_id or "")) != bool(
            str(role_submission_payload_hash or "")
        ):
            raise ValueError(
                "role submission settlement requires assignment id and payload hash"
            )
        if _connection is None:
            self.ensure_schema()
        request_hash = _stable_hash(_action_request_payload(action))
        transaction = self._transaction() if _connection is None else nullcontext(_connection)
        with transaction as connection:
            duplicate = connection.execute(
                """
                SELECT request_hash, result_json
                FROM minion_v2_action_dedup
                WHERE aggregate_type = ? AND aggregate_id = ? AND idempotency_key = ?
                """,
                (action.aggregate_type.value, action.aggregate_id, action.dedup_key),
            ).fetchone()
            if duplicate is not None:
                if str(duplicate["request_hash"]) != request_hash:
                    raise ValueError("idempotency key was reused with a different action request")
                if role_assignment_id:
                    self._settle_role_assignment_locked(
                        connection,
                        assignment_id=str(role_assignment_id),
                        submission_payload_hash=str(role_submission_payload_hash),
                        workflow_id=action.workflow_id,
                        aggregate_type=action.aggregate_type.value,
                        aggregate_id=action.aggregate_id,
                    )
                return _decode_dispatch_result(json.loads(str(duplicate["result_json"])), duplicate=True)

            self._consume_human_decision_locked(connection, action)
            self._assert_artifact_refs_durable(connection, action.payload)
            current = self._read_snapshot_locked(connection, action.aggregate_type, action.aggregate_id)
            outcome = self.engine.transition(current, action)
            self._write_snapshot_locked(connection, current, outcome.snapshot)

            events: list[DomainEvent] = []
            for draft in outcome.events:
                event = DomainEvent(
                    event_id=f"evt_{uuid4().hex}",
                    workflow_id=action.workflow_id,
                    aggregate_type=action.aggregate_type,
                    aggregate_id=action.aggregate_id,
                    aggregate_version=outcome.snapshot.version,
                    event_type=draft.event_type,
                    payload=dict(draft.payload),
                    action_id=action.action_id,
                    correlation_id=action.correlation_id or action.action_id,
                    causation_id=action.causation_id,
                    created_at=action.created_at,
                )
                connection.execute(
                    """
                    INSERT INTO minion_v2_domain_events(
                        event_id, workflow_id, aggregate_type, aggregate_id, aggregate_version,
                        event_type, payload_json, action_id, correlation_id, causation_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.workflow_id,
                        event.aggregate_type.value,
                        event.aggregate_id,
                        event.aggregate_version,
                        event.event_type,
                        _json(event.payload),
                        event.action_id,
                        event.correlation_id,
                        event.causation_id,
                        event.created_at,
                    ),
                )
                events.append(event)

            if outcome.effects and not events:
                raise RuntimeError("outbox effects require a causative domain event")
            effect_ids: list[str] = []
            event_id = events[0].event_id if events else ""
            for index, effect in enumerate(outcome.effects):
                effect_id = f"eff_{uuid4().hex}"
                effect_key = f"{event_id}:{index}"
                payload = dict(effect.payload)
                payload["_causal_context"] = {
                    "aggregate_version": outcome.snapshot.version,
                    "target_state": outcome.snapshot.state,
                    "active_worker_id": str(
                        outcome.snapshot.payload.get("active_worker_id") or ""
                    ),
                    "lease_resource_key": str(
                        outcome.snapshot.payload.get("lease_resource_key") or ""
                    ),
                    "fencing_token": int(
                        outcome.snapshot.payload.get("fencing_token") or 0
                    ),
                }
                effect_request_hash = _stable_hash({"effect_type": effect.effect_type, "payload": payload})
                connection.execute(
                    """
                    INSERT INTO minion_v2_outbox(
                        effect_id, effect_key, workflow_id, aggregate_type, aggregate_id,
                        event_id, effect_index, effect_type, request_hash, payload_json,
                        status, attempt_count, max_attempts, next_retry_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                    """,
                    (
                        effect_id,
                        effect_key,
                        action.workflow_id,
                        action.aggregate_type.value,
                        action.aggregate_id,
                        event_id,
                        index,
                        effect.effect_type,
                        effect_request_hash,
                        _json(payload),
                        max(1, int(effect.max_attempts)),
                        action.created_at,
                        action.created_at,
                        action.created_at,
                    ),
                )
                effect_ids.append(effect_id)

            result = DispatchResult(
                snapshot=outcome.snapshot,
                events=tuple(events),
                outbox_effect_ids=tuple(effect_ids),
            )
            encoded_result = _encode_dispatch_result(result)
            connection.execute(
                """
                INSERT INTO minion_v2_action_dedup(
                    aggregate_type, aggregate_id, idempotency_key, action_id,
                    request_hash, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.aggregate_type.value,
                    action.aggregate_id,
                    action.dedup_key,
                    action.action_id,
                    request_hash,
                    _json(encoded_result),
                    action.created_at,
                ),
            )
            if role_assignment_id:
                self._settle_role_assignment_locked(
                    connection,
                    assignment_id=str(role_assignment_id),
                    submission_payload_hash=str(role_submission_payload_hash),
                    workflow_id=action.workflow_id,
                    aggregate_type=action.aggregate_type.value,
                    aggregate_id=action.aggregate_id,
                )
            self._update_task_projection_locked(connection, outcome.snapshot)
            self._update_node_projection_locked(connection, outcome.snapshot)
            if action.aggregate_type != AggregateType.TASK:
                self._rebuild_workflow_projection_locked(connection, action.workflow_id, events[-1].event_id if events else "")
            return result

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open one repository transaction for an aggregate plus its projections.

        Callers may pass the yielded connection back to ``dispatch`` and the
        cycle projection methods.  Nothing becomes visible to the outbox
        processor until the complete business transition commits.
        """

        self.ensure_schema()
        with self._transaction() as connection:
            yield connection

    def read_snapshot(
        self,
        aggregate_type: AggregateType,
        aggregate_id: str,
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> AggregateSnapshot | None:
        if _connection is None:
            self.ensure_schema()
        connection_scope = self._connect() if _connection is None else nullcontext(_connection)
        with connection_scope as connection:
            return self._read_snapshot_locked(connection, aggregate_type, aggregate_id)

    def dispatch_task_with_delivery(
        self,
        action: ActionEnvelope,
        *,
        binding: Mapping[str, Any],
    ) -> DispatchResult:
        """Create one Task and its delivery binding in the same DB commit."""

        if action.aggregate_type != AggregateType.TASK or action.action_type != "CREATE_TASK":
            raise ValueError("atomic Task delivery binding requires CREATE_TASK")
        self.ensure_schema()
        normalized = _normalize_delivery_binding(binding)
        with self._transaction() as connection:
            result = self.dispatch(action, _connection=connection)
            existing = connection.execute(
                "SELECT * FROM minion_v2_task_delivery_bindings WHERE task_id = ?",
                (action.aggregate_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO minion_v2_task_delivery_bindings(
                        task_id, origin_binding_json, current_binding_json,
                        binding_version, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (
                        action.aggregate_id,
                        _json(normalized),
                        _json(normalized),
                        action.created_at,
                        action.created_at,
                    ),
                )
            else:
                origin = json.loads(str(existing["origin_binding_json"]))
                if origin != normalized:
                    raise ValueError(
                        "Task delivery origin differs from the idempotent creation request"
                    )
        return result

    def list_workflow_snapshots(self, workflow_id: str) -> tuple[AggregateSnapshot, ...]:
        self.ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM minion_v2_aggregate_snapshots
                WHERE workflow_id = ?
                ORDER BY created_at, aggregate_type, aggregate_id
                """,
                (str(workflow_id),),
            ).fetchall()
            return tuple(_snapshot_from_row(row) for row in rows)

    def workflow_ids(self) -> tuple[str, ...]:
        self.ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT aggregate_id FROM minion_v2_aggregate_snapshots WHERE aggregate_type = ? ORDER BY created_at, aggregate_id",
                (AggregateType.WORKFLOW.value,),
            ).fetchall()
            return tuple(str(row["aggregate_id"]) for row in rows)

    def read_workflow_projection(self, workflow_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT last_progress_event_id FROM minion_v2_workflow_projection WHERE workflow_id = ?",
                (str(workflow_id),),
            ).fetchone()
            self._rebuild_workflow_projection_locked(
                connection,
                str(workflow_id),
                str(existing["last_progress_event_id"] or "") if existing is not None else "",
            )
            row = connection.execute(
                "SELECT * FROM minion_v2_workflow_projection WHERE workflow_id = ?",
                (str(workflow_id),),
            ).fetchone()
            if row is None:
                return None
            return _decode_json_columns(
                row,
                {
                    "blocker_json": "blocker",
                    "next_legal_actions_json": "next_legal_actions",
                    "metrics_json": "metrics",
                },
            )

    def list_workflow_node_projections(
        self,
        workflow_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Return the Manager-owned semantic node projection for one workflow."""
        self.ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM minion_v2_node_projection
                WHERE workflow_id = ?
                ORDER BY updated_at, node_run_id
                """,
                (str(workflow_id),),
            ).fetchall()
        return tuple(
            _decode_json_columns(
                row,
                {
                    "dependency_node_ids_json": "dependency_node_ids",
                    "blocker_json": "blocker",
                },
            )
            for row in rows
        )

    def list_workflow_role_invocations(
        self,
        workflow_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Return role lifecycle rows used to build a public semantic status."""
        self.ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT invocation_id, workflow_id, aggregate_type, aggregate_id,
                       role, mode, status, last_completed_turn,
                       total_input_tokens, total_output_tokens,
                       total_latency_ms, total_tool_latency_ms,
                       total_wall_latency_ms, created_at, updated_at
                FROM minion_v2_role_invocations
                WHERE workflow_id = ?
                ORDER BY created_at, invocation_id
                """,
                (str(workflow_id),),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def read_role_checklist_progress(self, session_id: str) -> dict[str, Any] | None:
        """Read the current attempt's durable work cursor without adding events."""

        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return None
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT assignment.role, assignment.mode,
                       assignment.state AS assignment_state,
                       attempt.status AS attempt_state,
                       draft.version AS checklist_version,
                       draft.status AS checklist_status,
                       draft.payload_json AS checklist_payload_json,
                       draft.updated_at AS checklist_updated_at
                FROM minion_v2_role_assignments AS assignment
                LEFT JOIN minion_v2_role_attempts AS attempt
                  ON attempt.attempt_id = assignment.active_attempt_id
                LEFT JOIN minion_v2_submission_drafts AS draft
                  ON draft.invocation_id = attempt.attempt_id
                 AND draft.workflow_id = assignment.workflow_id
                 AND draft.draft_kind = 'work_items'
                WHERE assignment.session_id = ?
                ORDER BY assignment.created_at DESC,
                         assignment.assignment_id DESC,
                         draft.updated_at DESC
                LIMIT 1
                """,
                (normalized_session_id,),
            ).fetchone()
        if row is None:
            return None
        payload_value = json.loads(str(row["checklist_payload_json"] or "{}"))
        payload = dict(payload_value) if isinstance(payload_value, Mapping) else {}
        items = [
            {
                "kind": str(item.get("kind") or "phase"),
                "summary": str(item.get("summary") or ""),
                "status": str(item.get("status") or "pending"),
            }
            for item in list(payload.get("items") or [])
            if isinstance(item, Mapping) and str(item.get("summary") or "").strip()
        ]
        completed = sum(1 for item in items if item["status"] == "completed")
        current = next(
            (item["summary"] for item in items if item["status"] != "completed"),
            "",
        )
        version = int(row["checklist_version"] or 0)
        return {
            "role": str(row["role"] or ""),
            "mode": str(row["mode"] or ""),
            "assignment_state": str(row["assignment_state"] or ""),
            "attempt_state": str(row["attempt_state"] or ""),
            "activity_observed": version > 0,
            "checklist": {
                "status": str(row["checklist_status"] or ""),
                "version": version,
                "completed": completed,
                "total": len(items),
                "current": current,
                "items": items,
                "updated_at": str(row["checklist_updated_at"] or ""),
            },
        }

    def bind_task_delivery(
        self,
        *,
        task_id: str,
        binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Capture the immutable origin and initial reply target for one Task."""

        self.ensure_schema()
        normalized_task_id = str(task_id or "").strip()
        normalized = _normalize_delivery_binding(binding)
        if not normalized_task_id:
            raise ValueError("task delivery binding requires task_id")
        now = utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT 1 FROM minion_v2_task_projection WHERE task_id = ?",
                (normalized_task_id,),
            ).fetchone()
            if row is None:
                raise ValueError("task delivery binding requires an existing Task")
            existing = connection.execute(
                "SELECT * FROM minion_v2_task_delivery_bindings WHERE task_id = ?",
                (normalized_task_id,),
            ).fetchone()
            if existing is not None:
                current = json.loads(str(existing["current_binding_json"]))
                if current != normalized:
                    raise ValueError(
                        "Task delivery is already bound; use the explicit rebind operation"
                    )
                return _delivery_binding_row(existing)
            connection.execute(
                """
                INSERT INTO minion_v2_task_delivery_bindings(
                    task_id, origin_binding_json, current_binding_json,
                    binding_version, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                (normalized_task_id, _json(normalized), _json(normalized), now, now),
            )
        return self.read_task_delivery(normalized_task_id) or {}

    def read_task_delivery(self, task_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_task_delivery_bindings WHERE task_id = ?",
                (str(task_id or "").strip(),),
            ).fetchone()
        return _delivery_binding_row(row) if row is not None else None

    def rebind_task_delivery(
        self,
        *,
        task_id: str,
        binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Replace only the Task reply target; workflow state is untouched."""

        self.ensure_schema()
        normalized_task_id = str(task_id or "").strip()
        normalized = _normalize_delivery_binding(binding)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_task_delivery_bindings WHERE task_id = ?",
                (normalized_task_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Task has no delivery binding")
            current = json.loads(str(row["current_binding_json"]))
            if current == normalized:
                return {**_delivery_binding_row(row), "changed": False}
            version = int(row["binding_version"]) + 1
            now = utc_now()
            connection.execute(
                """
                UPDATE minion_v2_task_delivery_bindings
                SET current_binding_json = ?, binding_version = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (_json(normalized), version, now, normalized_task_id),
            )
            updated = connection.execute(
                "SELECT * FROM minion_v2_task_delivery_bindings WHERE task_id = ?",
                (normalized_task_id,),
            ).fetchone()
        return {**_delivery_binding_row(updated), "changed": True}

    def enqueue_task_delivery(
        self,
        *,
        task_id: str,
        workflow_id: str,
        event_kind: str,
        payload: Mapping[str, Any],
        dedup_key: str,
    ) -> dict[str, Any]:
        """Persist one user-visible Task event until Core accepts its delivery."""

        self.ensure_schema()
        normalized_task_id = str(task_id or "").strip()
        normalized_kind = str(event_kind or "").strip()
        normalized_workflow_id = str(workflow_id or "").strip()
        normalized_key = str(dedup_key or "").strip()
        if not normalized_task_id or not normalized_kind or not normalized_key:
            raise ValueError("task delivery requires task_id, event_kind, and dedup_key")
        now = utc_now()
        delivery_id = f"delivery_{uuid4().hex}"
        with self._transaction() as connection:
            binding = connection.execute(
                "SELECT 1 FROM minion_v2_task_delivery_bindings WHERE task_id = ?",
                (normalized_task_id,),
            ).fetchone()
            if binding is None:
                raise ValueError("task delivery requires an existing delivery binding")
            connection.execute(
                """
                INSERT INTO minion_v2_delivery_outbox(
                    delivery_id, dedup_key, task_id, workflow_id, event_kind,
                    payload_json, status, attempt_count, next_attempt_at,
                    last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, '', ?, ?)
                ON CONFLICT(dedup_key) DO NOTHING
                """,
                (
                    delivery_id,
                    normalized_key,
                    normalized_task_id,
                    normalized_workflow_id,
                    normalized_kind,
                    _json(dict(payload or {})),
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM minion_v2_delivery_outbox WHERE dedup_key = ?",
                (normalized_key,),
            ).fetchone()
            if row is not None:
                same_request = (
                    str(row["task_id"]) == normalized_task_id
                    and str(row["workflow_id"]) == normalized_workflow_id
                    and str(row["event_kind"]) == normalized_kind
                    and json.loads(str(row["payload_json"])) == dict(payload or {})
                )
                if not same_request:
                    raise ValueError(
                        f"delivery dedup key collision with different request: {normalized_key}"
                    )
        return _delivery_outbox_row(row)

    def list_pending_task_deliveries(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
        self.ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*, b.current_binding_json, b.binding_version
                FROM minion_v2_delivery_outbox AS d
                JOIN minion_v2_task_delivery_bindings AS b ON b.task_id = d.task_id
                WHERE d.status = 'pending' AND d.next_attempt_at <= ?
                ORDER BY d.created_at, d.delivery_id
                LIMIT ?
                """,
                (utc_now(), max(1, min(int(limit), 500))),
            ).fetchall()
        return tuple(_delivery_outbox_row(row) for row in rows)

    def acknowledge_task_delivery(self, delivery_id: str) -> bool:
        self.ensure_schema()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE minion_v2_delivery_outbox
                SET status = 'delivered', updated_at = ?
                WHERE delivery_id = ? AND status = 'pending'
                """,
                (utc_now(), str(delivery_id or "").strip()),
            )
        return cursor.rowcount == 1

    def delivered_task_delivery_parts(self, delivery_id: str) -> tuple[str, ...]:
        """Return durable sub-deliveries already accepted by the channel."""

        self.ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT part_key FROM minion_v2_delivery_parts
                WHERE delivery_id = ?
                ORDER BY part_key
                """,
                (str(delivery_id or "").strip(),),
            ).fetchall()
        return tuple(str(row["part_key"]) for row in rows)

    def acknowledge_task_delivery_part(
        self,
        delivery_id: str,
        part_key: str,
    ) -> bool:
        """Durably mark one stable part of a composite delivery as accepted."""

        self.ensure_schema()
        normalized_delivery_id = str(delivery_id or "").strip()
        normalized_part_key = str(part_key or "").strip()
        if not normalized_delivery_id or not normalized_part_key:
            raise ValueError("delivery_id and part_key are required")
        with self._transaction() as connection:
            pending = connection.execute(
                """
                SELECT 1 FROM minion_v2_delivery_outbox
                WHERE delivery_id = ? AND status = 'pending'
                """,
                (normalized_delivery_id,),
            ).fetchone()
            if pending is None:
                return False
            connection.execute(
                """
                INSERT INTO minion_v2_delivery_parts(
                    delivery_id, part_key, delivered_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(delivery_id, part_key) DO NOTHING
                """,
                (normalized_delivery_id, normalized_part_key, utc_now()),
            )
        return True

    def defer_task_delivery(self, delivery_id: str, *, error: str = "") -> bool:
        self.ensure_schema()
        now = _utc_datetime()
        retry_at = (now + timedelta(seconds=1)).isoformat()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE minion_v2_delivery_outbox
                SET attempt_count = attempt_count + 1,
                    next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'pending'
                """,
                (
                    retry_at,
                    str(error or "")[:1000],
                    now.isoformat(),
                    str(delivery_id or "").strip(),
                ),
            )
        return cursor.rowcount == 1

    def latest_task_delivery(
        self,
        *,
        task_id: str,
        workflow_id: str,
        event_kind: str,
    ) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM minion_v2_delivery_outbox
                WHERE task_id = ? AND workflow_id = ? AND event_kind = ?
                ORDER BY created_at DESC, delivery_id DESC
                LIMIT 1
                """,
                (str(task_id), str(workflow_id), str(event_kind)),
            ).fetchone()
        return _delivery_outbox_row(row) if row is not None else None

    def replay_task_delivery(
        self,
        *,
        delivery_id: str,
        dedup_key: str,
    ) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_delivery_outbox WHERE delivery_id = ?",
                (str(delivery_id),),
            ).fetchone()
        if row is None:
            raise ValueError("delivery replay source does not exist")
        source = _delivery_outbox_row(row)
        return self.enqueue_task_delivery(
            task_id=source["task_id"],
            workflow_id=source["workflow_id"],
            event_kind=source["event_kind"],
            payload=source["payload"],
            dedup_key=dedup_key,
        )

    def pending_human_review_workflows(self, task_id: str) -> tuple[str, ...]:
        self.ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT h.workflow_id
                FROM minion_v2_human_decisions AS h
                JOIN minion_v2_aggregate_snapshots AS w
                  ON w.aggregate_type = ? AND w.aggregate_id = h.workflow_id
                WHERE h.status = 'issued'
                  AND json_extract(w.payload_json, '$.task_id') = ?
                ORDER BY h.workflow_id
                """,
                (AggregateType.WORKFLOW.value, str(task_id)),
            ).fetchall()
        return tuple(str(row["workflow_id"]) for row in rows)

    def search_workflows(
        self,
        *,
        actor_id: str,
        task_id: str = "",
        query: str = "",
        include_terminal: bool = False,
        limit: int = 20,
    ) -> tuple[dict[str, Any], ...]:
        self.ensure_schema()
        clauses = ["s.aggregate_type = ?", "json_extract(s.payload_json, '$.owner') = ?"]
        parameters: list[Any] = [AggregateType.WORKFLOW.value, str(actor_id or "").strip()]
        if task_id:
            clauses.append("json_extract(s.payload_json, '$.task_id') = ?")
            parameters.append(str(task_id).strip())
        if not include_terminal:
            clauses.append("s.state NOT IN ('COMPLETED', 'REJECTED', 'CANCELLED')")
        text = str(query or "").strip().lower()
        if text:
            clauses.append(
                "(lower(coalesce(json_extract(s.payload_json, '$.workflow_name'), t.title, '')) LIKE ? "
                "OR lower(coalesce(t.objective, '')) LIKE ?)"
            )
            pattern = f"%{text}%"
            parameters.extend((pattern, pattern))
        parameters.append(max(1, min(int(limit), 100)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.aggregate_id AS workflow_id, s.state AS workflow_state,
                       s.updated_at, coalesce(t.title, '') AS task_title,
                       coalesce(json_extract(s.payload_json, '$.workflow_name'), t.title, '') AS workflow_name,
                       coalesce(t.objective, '') AS task_objective
                FROM minion_v2_aggregate_snapshots AS s
                LEFT JOIN minion_v2_task_projection AS t
                  ON t.task_id = json_extract(s.payload_json, '$.task_id')
                WHERE """
                + " AND ".join(clauses)
                + " ORDER BY s.updated_at DESC, s.aggregate_id LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def bind_artifact_alias(
        self,
        *,
        actor_id: str,
        alias: str,
        artifact_sha256: str,
    ) -> None:
        self.ensure_schema()
        actor = str(actor_id or "").strip()
        name = str(alias or "").strip()
        digest = str(artifact_sha256 or "").removeprefix("sha256:")
        if not actor or not name or not digest:
            raise ValueError("artifact alias requires actor, name, and artifact")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT durable FROM minion_v2_artifacts WHERE sha256 = ?",
                (digest,),
            ).fetchone()
            if row is None or int(row["durable"]) != 1:
                raise ValueError("artifact alias requires a durable artifact")
            connection.execute(
                """
                INSERT INTO minion_v2_artifact_aliases(actor_id, alias, artifact_sha256, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(actor_id, alias) DO UPDATE SET
                    artifact_sha256 = excluded.artifact_sha256,
                    updated_at = excluded.updated_at
                """,
                (actor, name, digest, utc_now()),
            )

    def resolve_artifact_alias(self, *, actor_id: str, alias: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT a.* FROM minion_v2_artifacts AS a
                JOIN minion_v2_artifact_aliases AS x ON x.artifact_sha256 = a.sha256
                WHERE x.actor_id = ? AND x.alias = ?
                """,
                (str(actor_id or "").strip(), str(alias or "").strip()),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["durable"] = bool(result["durable"])
        result["provenance"] = json.loads(str(result.pop("provenance_json")))
        result["metadata"] = json.loads(str(result.pop("metadata_json")))
        return result

    def read_latest_workflow_event(self, workflow_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT event_type, aggregate_type, created_at
                FROM minion_v2_domain_events
                WHERE workflow_id = ?
                ORDER BY created_at DESC, event_id DESC
                LIMIT 1
                """,
                (str(workflow_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def read_domain_event_aggregate_version(self, event_id: str) -> int | None:
        """Return the snapshot version that produced a durable outbox effect."""
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT aggregate_version FROM minion_v2_domain_events WHERE event_id = ?",
                (str(event_id or "").strip(),),
            ).fetchone()
        return int(row["aggregate_version"]) if row is not None else None

    def read_domain_event_effect_context(self, event_id: str) -> dict[str, Any]:
        """Recover causal state for outbox rows created before context embedding."""

        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT aggregate_version, payload_json "
                "FROM minion_v2_domain_events WHERE event_id = ?",
                (str(event_id or "").strip(),),
            ).fetchone()
        if row is None:
            return {}
        event_payload = json.loads(str(row["payload_json"] or "{}"))
        action_payload = dict(event_payload.get("action_payload") or {})
        return {
            "aggregate_version": int(row["aggregate_version"]),
            "target_state": str(event_payload.get("target_state") or ""),
            "active_worker_id": str(action_payload.get("active_worker_id") or ""),
            "lease_resource_key": str(action_payload.get("lease_resource_key") or ""),
            "fencing_token": int(action_payload.get("fencing_token") or 0),
        }

    def search_tasks(
        self,
        *,
        query: str = "",
        task_id: str = "",
        family_id: str = "",
        owner: str = "",
        include_archived: bool = False,
        limit: int = 20,
    ) -> tuple[dict[str, Any], ...]:
        self.ensure_schema()
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(str(task_id))
        if family_id:
            clauses.append("family_id = ?")
            parameters.append(str(family_id))
        if owner:
            clauses.append("owner = ?")
            parameters.append(str(owner))
        if not include_archived:
            clauses.append("state != 'ARCHIVED'")
        text = str(query or "").strip().lower()
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        resolved_limit = max(1, min(int(limit), 100))
        with self._connect() as connection:
            if text:
                scored: dict[str, tuple[float, dict[str, Any]]] = {}
                fts_filters = [f"t.{clause}" for clause in clauses]
                fts_where = " AND " + " AND ".join(fts_filters) if fts_filters else ""
                for fts_query, query_weight in compile_jieba_fts_queries(text):
                    try:
                        rows = connection.execute(
                            """
                            SELECT t.*, -bm25(minion_v2_tasks_fts) AS fts_score
                            FROM minion_v2_tasks_fts
                            JOIN minion_v2_task_projection AS t
                              ON t.task_id = minion_v2_tasks_fts.task_id
                            WHERE minion_v2_tasks_fts MATCH ?
                            """
                            + fts_where
                            + " ORDER BY bm25(minion_v2_tasks_fts), t.updated_at DESC LIMIT ?",
                            (fts_query, *parameters, resolved_limit),
                        ).fetchall()
                    except sqlite3.OperationalError:
                        continue
                    for row in rows:
                        item = dict(row)
                        score = float(item.pop("fts_score")) * float(query_weight)
                        current = scored.get(str(item["task_id"]))
                        if current is None or score > current[0]:
                            scored[str(item["task_id"])] = (score, item)
                    if rows:
                        break
                if scored:
                    ordered = sorted(
                        scored.values(),
                        key=lambda item: (-item[0], str(item[1]["task_id"])),
                    )[:resolved_limit]
                    return tuple({**item, "score": score} for score, item in ordered)

                like_clauses = [
                    *clauses,
                    "(lower(title) LIKE ? OR lower(objective) LIKE ? OR lower(workspace_key) LIKE ?)",
                ]
                like_where = " WHERE " + " AND ".join(like_clauses)
                pattern = f"%{text}%"
                rows = connection.execute(
                    "SELECT *, 1.0 AS score FROM minion_v2_task_projection"
                    + like_where
                    + " ORDER BY updated_at DESC, task_id LIMIT ?",
                    (*parameters, pattern, pattern, pattern, resolved_limit),
                ).fetchall()
                return tuple(dict(row) for row in rows)

            rows = connection.execute(
                "SELECT * FROM minion_v2_task_projection"
                + where
                + " ORDER BY updated_at DESC, task_id LIMIT ?",
                (*parameters, resolved_limit),
            ).fetchall()
            return tuple(dict(row) for row in rows)

    def _ensure_task_fts_index_locked(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT schema_value FROM minion_v2_schema_meta WHERE schema_key = 'task_fts_index_version'"
        ).fetchone()
        if row is not None and str(row[0]) == _TASK_FTS_INDEX_VERSION:
            return
        connection.execute("DELETE FROM minion_v2_tasks_fts")
        for task in connection.execute("SELECT * FROM minion_v2_task_projection").fetchall():
            self._sync_task_fts_locked(connection, str(task["task_id"]), row=task)
        connection.execute(
            """
            INSERT INTO minion_v2_schema_meta(schema_key, schema_value)
            VALUES ('task_fts_index_version', ?)
            ON CONFLICT(schema_key) DO UPDATE SET schema_value = excluded.schema_value
            """,
            (_TASK_FTS_INDEX_VERSION,),
        )

    def _sync_task_fts_locked(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        *,
        row: sqlite3.Row | None = None,
    ) -> None:
        connection.execute("DELETE FROM minion_v2_tasks_fts WHERE task_id = ?", (str(task_id),))
        task = row or connection.execute(
            "SELECT * FROM minion_v2_task_projection WHERE task_id = ?", (str(task_id),)
        ).fetchone()
        if task is None:
            return
        connection.execute(
            """
            INSERT INTO minion_v2_tasks_fts(task_id, title, objective, workspace)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(task["task_id"]),
                jieba_fts_text(task["title"]),
                jieba_fts_text(task["objective"]),
                jieba_fts_text(task["workspace_key"]),
            ),
        )

    def has_nonterminal_workflows_for_task(self, task_id: str) -> bool:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM minion_v2_aggregate_snapshots
                WHERE aggregate_type = ?
                  AND json_extract(payload_json, '$.task_id') = ?
                  AND state NOT IN ('COMPLETED', 'REJECTED', 'CANCELLED')
                LIMIT 1
                """,
                (AggregateType.WORKFLOW.value, str(task_id)),
            ).fetchone()
            return row is not None

    def claim_outbox(self, worker_id: str, *, limit: int = 20, lease_seconds: int = 60) -> tuple[dict[str, Any], ...]:
        self.ensure_schema()
        now = _utc_datetime()
        now_text = now.isoformat()
        locked_until = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM minion_v2_outbox
                WHERE next_retry_at <= ?
                  AND (
                    (status = 'pending' AND attempt_count < max_attempts)
                    OR (status = 'inflight' AND locked_until <= ?)
                  )
                ORDER BY created_at, effect_id
                LIMIT ?
                """,
                (now_text, now_text, max(1, int(limit))),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                cursor = connection.execute(
                    """
                    UPDATE minion_v2_outbox
                    SET status = 'inflight',
                        attempt_count = CASE
                            WHEN status = 'pending' THEN attempt_count + 1
                            ELSE attempt_count
                        END,
                        locked_by = ?, locked_until = ?, updated_at = ?
                    WHERE effect_id = ?
                      AND (
                        (status = 'pending' AND attempt_count < max_attempts)
                        OR (status = 'inflight' AND locked_until <= ?)
                      )
                    """,
                    (worker_id, locked_until, now_text, str(row["effect_id"]), now_text),
                )
                if cursor.rowcount == 1:
                    if str(row["status"]) == "inflight":
                        connection.execute(
                            """
                            UPDATE minion_v2_effect_attempts
                            SET status = 'lost', error_kind = 'lease_expired',
                                error_text = 'outbox claim expired before settlement',
                                finished_at = ?
                            WHERE effect_id = ? AND status = 'running'
                            """,
                            (now_text, str(row["effect_id"])),
                        )
                    attempt_row = connection.execute(
                        """
                        SELECT COALESCE(MAX(attempt_index), 0) + 1 AS next_attempt_index
                        FROM minion_v2_effect_attempts
                        WHERE effect_id = ?
                        """,
                        (str(row["effect_id"]),),
                    ).fetchone()
                    effect_attempt_index = int(attempt_row["next_attempt_index"])
                    connection.execute(
                        """
                        INSERT INTO minion_v2_effect_attempts(
                            effect_id, attempt_index, worker_id, status, started_at
                        ) VALUES (?, ?, ?, 'running', ?)
                        """,
                        (
                            str(row["effect_id"]),
                            effect_attempt_index,
                            str(worker_id),
                            now_text,
                        ),
                    )
                    attempt_count = int(row["attempt_count"])
                    if str(row["status"]) == "pending":
                        attempt_count += 1
                    item = dict(row)
                    item.update(
                        {
                            "status": "inflight",
                            "attempt_count": attempt_count,
                            "effect_attempt_index": effect_attempt_index,
                            "claim_incremented_attempt": str(row["status"]) == "pending",
                            "locked_by": worker_id,
                            "locked_until": locked_until,
                            "payload": json.loads(str(row["payload_json"])),
                        }
                    )
                    claimed.append(item)
            return tuple(claimed)

    def complete_outbox_effect(
        self,
        effect_id: str,
        *,
        worker_id: str,
        provider_request_id: str = "",
        result_artifact_ref: Mapping[str, Any] | None = None,
    ) -> bool:
        self.ensure_schema()
        result_ref = dict(result_artifact_ref or {})
        now = utc_now()
        with self._transaction() as connection:
            self._assert_artifact_refs_durable(connection, result_ref)
            row = connection.execute(
                "SELECT * FROM minion_v2_outbox WHERE effect_id = ?",
                (str(effect_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown outbox effect: {effect_id}")
            receipt = connection.execute(
                "SELECT request_hash FROM minion_v2_effect_receipts WHERE effect_key = ?",
                (str(row["effect_key"]),),
            ).fetchone()
            if receipt is not None:
                if str(receipt["request_hash"]) != str(row["request_hash"]):
                    raise ValueError("effect receipt request hash mismatch")
                return False
            if str(row["status"]) != "inflight" or str(row["locked_by"]) != str(worker_id):
                raise LeaseConflict("outbox effect is not claimed by this worker")
            connection.execute(
                """
                INSERT INTO minion_v2_effect_receipts(
                    effect_key, effect_id, request_hash, provider_request_id,
                    result_artifact_ref_json, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row["effect_key"]),
                    str(effect_id),
                    str(row["request_hash"]),
                    str(provider_request_id),
                    _json(result_ref),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE minion_v2_outbox
                SET status = 'completed', provider_request_id = ?, result_artifact_ref_json = ?,
                    locked_by = '', locked_until = '', last_error = '', updated_at = ?
                WHERE effect_id = ?
                """,
                (str(provider_request_id), _json(result_ref), now, str(effect_id)),
            )
            self._finish_effect_attempt_locked(
                connection,
                effect_id=str(effect_id),
                worker_id=str(worker_id),
                status="completed",
                provider_request_id=str(provider_request_id),
                result_artifact_ref=result_ref,
                finished_at=now,
            )
            return True

    def renew_outbox_claim(self, effect_id: str, *, worker_id: str, lease_seconds: int = 60) -> None:
        self.ensure_schema()
        now = _utc_datetime()
        locked_until = now + timedelta(seconds=max(1, lease_seconds))
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE minion_v2_outbox
                SET locked_until = ?, updated_at = ?
                WHERE effect_id = ? AND status = 'inflight' AND locked_by = ?
                """,
                (locked_until.isoformat(), now.isoformat(), str(effect_id), str(worker_id)),
            )
            if cursor.rowcount != 1:
                raise LeaseConflict("outbox effect is not claimed by this worker")

    def retry_outbox_effect(
        self,
        effect_id: str,
        *,
        worker_id: str,
        error: str,
        retry_after_seconds: int = 5,
        triage_action: ActionEnvelope | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> str:
        if _connection is None:
            self.ensure_schema()
        now = _utc_datetime()
        transaction = self._transaction() if _connection is None else nullcontext(_connection)
        with transaction as connection:
            row = connection.execute(
                "SELECT status, locked_by, attempt_count, max_attempts FROM minion_v2_outbox WHERE effect_id = ?",
                (str(effect_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown outbox effect: {effect_id}")
            if str(row["status"]) != "inflight" or str(row["locked_by"]) != str(worker_id):
                raise LeaseConflict("outbox effect is not claimed by this worker")
            exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
            status = "failed" if exhausted else "pending"
            next_retry_at = (now + timedelta(seconds=max(0, retry_after_seconds))).isoformat()
            connection.execute(
                """
                UPDATE minion_v2_outbox
                SET status = ?, next_retry_at = ?, locked_by = '', locked_until = '',
                    last_error = ?, updated_at = ?
                WHERE effect_id = ?
                """,
                (status, next_retry_at, str(error), now.isoformat(), str(effect_id)),
            )
            self._finish_effect_attempt_locked(
                connection,
                effect_id=str(effect_id),
                worker_id=str(worker_id),
                status="failed" if exhausted else "retryable",
                error_kind="effect_failed" if exhausted else "effect_retry",
                error_text=str(error),
                finished_at=now.isoformat(),
            )
            if exhausted and triage_action is not None:
                self.dispatch(triage_action, _connection=connection)
            return status

    def defer_outbox_effect(
        self,
        effect_id: str,
        *,
        worker_id: str,
        reason: str,
        attempt_was_incremented: bool = True,
    ) -> None:
        """Return a claimed effect to the queue without spending a retry attempt."""

        self.ensure_schema()
        now = utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status, locked_by, attempt_count FROM minion_v2_outbox WHERE effect_id = ?",
                (str(effect_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown outbox effect: {effect_id}")
            if str(row["status"]) != "inflight" or str(row["locked_by"]) != str(worker_id):
                raise LeaseConflict("outbox effect is not claimed by this worker")
            connection.execute(
                """
                UPDATE minion_v2_outbox
                SET status = 'pending', attempt_count = ?, next_retry_at = ?,
                    locked_by = '', locked_until = '', last_error = ?, updated_at = ?
                WHERE effect_id = ?
                """,
                (
                    max(
                        0,
                        int(row["attempt_count"]) - (1 if attempt_was_incremented else 0),
                    ),
                    now,
                    str(reason),
                    now,
                    str(effect_id),
                ),
            )
            self._finish_effect_attempt_locked(
                connection,
                effect_id=str(effect_id),
                worker_id=str(worker_id),
                status="deferred",
                error_kind="manager_deferred",
                error_text=str(reason),
                finished_at=now,
            )

    def fail_outbox_effect(
        self,
        effect_id: str,
        *,
        worker_id: str,
        error: str,
        triage_action: ActionEnvelope | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> DispatchResult | None:
        if _connection is None:
            self.ensure_schema()
        now = utc_now()
        transaction = self._transaction() if _connection is None else nullcontext(_connection)
        with transaction as connection:
            row = connection.execute(
                "SELECT status, locked_by FROM minion_v2_outbox WHERE effect_id = ?",
                (str(effect_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown outbox effect: {effect_id}")
            if str(row["status"]) != "inflight" or str(row["locked_by"]) != str(worker_id):
                raise LeaseConflict("outbox effect is not claimed by this worker")
            connection.execute(
                """
                UPDATE minion_v2_outbox
                SET status = 'failed', next_retry_at = ?, locked_by = '', locked_until = '',
                    last_error = ?, updated_at = ?
                WHERE effect_id = ?
                """,
                (now, str(error), now, str(effect_id)),
            )
            self._finish_effect_attempt_locked(
                connection,
                effect_id=str(effect_id),
                worker_id=str(worker_id),
                status="failed",
                error_kind="effect_failed",
                error_text=str(error),
                finished_at=now,
            )
            if triage_action is not None:
                return self.dispatch(triage_action, _connection=connection)
            return None

    def list_effect_attempts(self, effect_id: str) -> tuple[dict[str, Any], ...]:
        self.ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM minion_v2_effect_attempts
                WHERE effect_id = ?
                ORDER BY attempt_index
                """,
                (str(effect_id),),
            ).fetchall()
            return tuple(
                {
                    **dict(row),
                    "result_artifact_ref": json.loads(str(row["result_artifact_ref_json"])),
                }
                for row in rows
            )

    @staticmethod
    def _finish_effect_attempt_locked(
        connection: sqlite3.Connection,
        *,
        effect_id: str,
        worker_id: str,
        status: str,
        error_kind: str = "",
        error_text: str = "",
        provider_request_id: str = "",
        result_artifact_ref: Mapping[str, Any] | None = None,
        finished_at: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT attempt_index FROM minion_v2_effect_attempts
            WHERE effect_id = ? AND worker_id = ? AND status = 'running'
            ORDER BY attempt_index DESC
            LIMIT 1
            """,
            (str(effect_id), str(worker_id)),
        ).fetchone()
        if row is None:
            raise LeaseConflict("outbox effect has no active attempt for this worker")
        connection.execute(
            """
            UPDATE minion_v2_effect_attempts
            SET status = ?, error_kind = ?, error_text = ?,
                provider_request_id = ?, result_artifact_ref_json = ?, finished_at = ?
            WHERE effect_id = ? AND attempt_index = ? AND status = 'running'
            """,
            (
                str(status),
                str(error_kind),
                str(error_text),
                str(provider_request_id),
                _json(dict(result_artifact_ref or {})),
                str(finished_at),
                str(effect_id),
                int(row["attempt_index"]),
            ),
        )

    def claim_lease(
        self,
        resource_key: str,
        owner_id: str,
        *,
        ttl_seconds: int = 60,
        metadata: Mapping[str, Any] | None = None,
    ) -> LeaseGrant:
        if not resource_key.strip() or not owner_id.strip():
            raise ValueError("resource_key and owner_id are required")
        self.ensure_schema()
        now = _utc_datetime()
        expires_at = now + timedelta(seconds=max(1, ttl_seconds))
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            if row is not None and str(row["owner_id"]) and _parse_datetime(str(row["expires_at"])) > now:
                raise LeaseConflict(f"resource already leased by {row['owner_id']}")
            fencing_token = (int(row["fencing_token"]) if row is not None else 0) + 1
            connection.execute(
                """
                INSERT INTO minion_v2_leases(
                    resource_key, owner_id, fencing_token, acquired_at, renewed_at, expires_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resource_key) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    fencing_token = excluded.fencing_token,
                    acquired_at = excluded.acquired_at,
                    renewed_at = excluded.renewed_at,
                    expires_at = excluded.expires_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    resource_key,
                    owner_id,
                    fencing_token,
                    now.isoformat(),
                    now.isoformat(),
                    expires_at.isoformat(),
                    _json(dict(metadata or {})),
                ),
            )
            return LeaseGrant(resource_key, owner_id, fencing_token, now.isoformat(), expires_at.isoformat())

    def renew_lease(
        self,
        resource_key: str,
        owner_id: str,
        fencing_token: int,
        *,
        ttl_seconds: int = 60,
    ) -> LeaseGrant:
        self.ensure_schema()
        now = _utc_datetime()
        expires_at = now + timedelta(seconds=max(1, ttl_seconds))
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            self._assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=now)
            connection.execute(
                "UPDATE minion_v2_leases SET renewed_at = ?, expires_at = ? WHERE resource_key = ?",
                (now.isoformat(), expires_at.isoformat(), resource_key),
            )
            return LeaseGrant(resource_key, owner_id, fencing_token, str(row["acquired_at"]), expires_at.isoformat())

    def assert_fencing_token(self, resource_key: str, owner_id: str, fencing_token: int) -> None:
        self.ensure_schema()
        now = _utc_datetime()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            self._assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=now)

    def release_lease(self, resource_key: str, owner_id: str, fencing_token: int) -> None:
        self.ensure_schema()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            self._assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=None)
            connection.execute(
                "UPDATE minion_v2_leases SET owner_id = '', renewed_at = ?, expires_at = ? WHERE resource_key = ?",
                (utc_now(), utc_now(), resource_key),
            )

    def expired_leases(self) -> tuple[dict[str, Any], ...]:
        self.ensure_schema()
        now = utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM minion_v2_leases WHERE owner_id != '' AND expires_at <= ? ORDER BY expires_at",
                (now,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["metadata"] = json.loads(str(item.pop("metadata_json")))
                result.append(item)
            return tuple(result)

    def read_lease(self, resource_key: str) -> dict[str, Any] | None:
        """Read lease ownership and process metadata without changing its lifetime."""
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_leases WHERE resource_key = ?",
                (str(resource_key),),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["metadata"] = json.loads(str(item.pop("metadata_json")))
            return item

    def update_lease_metadata(
        self,
        resource_key: str,
        owner_id: str,
        fencing_token: int,
        metadata: Mapping[str, Any],
    ) -> None:
        self.ensure_schema()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            self._assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=_utc_datetime())
            connection.execute(
                "UPDATE minion_v2_leases SET metadata_json = ?, renewed_at = ? WHERE resource_key = ?",
                (_json(dict(metadata)), utc_now(), resource_key),
            )

    def clear_expired_lease(self, resource_key: str, fencing_token: int) -> bool:
        self.ensure_schema()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE minion_v2_leases
                SET owner_id = '', renewed_at = ?, expires_at = ?
                WHERE resource_key = ? AND fencing_token = ? AND expires_at <= ?
                """,
                (utc_now(), utc_now(), resource_key, int(fencing_token), utc_now()),
            )
            return cursor.rowcount == 1

    def rebuild_workflow_projections(self) -> int:
        self.ensure_schema()
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT aggregate_id FROM minion_v2_aggregate_snapshots WHERE aggregate_type = ?",
                (AggregateType.WORKFLOW.value,),
            ).fetchall()
            for row in rows:
                self._rebuild_workflow_projection_locked(connection, str(row["aggregate_id"]), "")
            return len(rows)

    def orphaned_workflow_ids(self) -> tuple[str, ...]:
        self.rebuild_workflow_projections()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT workflow_id FROM minion_v2_workflow_projection WHERE liveness = 'orphaned' ORDER BY workflow_id"
            ).fetchall()
            return tuple(str(row["workflow_id"]) for row in rows)

    def aggregate_liveness_sources(
        self,
        *,
        workflow_id: str,
        aggregate_type: AggregateType,
        aggregate_id: str,
        lease_resource_key: str = "",
    ) -> tuple[str, ...]:
        """Return durable execution sources for one worker-owned aggregate."""

        self.ensure_schema()
        sources: list[str] = []
        now = utc_now()
        with self._connect() as connection:
            if lease_resource_key:
                lease = connection.execute(
                    """
                    SELECT 1 FROM minion_v2_leases
                    WHERE resource_key = ? AND owner_id != '' AND expires_at > ?
                    LIMIT 1
                    """,
                    (str(lease_resource_key), now),
                ).fetchone()
                if lease is not None:
                    sources.append("live_lease")
            pending_effect = connection.execute(
                """
                SELECT 1 FROM minion_v2_outbox
                WHERE workflow_id = ? AND aggregate_type = ? AND aggregate_id = ?
                  AND status IN ('pending', 'inflight')
                LIMIT 1
                """,
                (str(workflow_id), aggregate_type.value, str(aggregate_id)),
            ).fetchone()
            if pending_effect is not None:
                sources.append("outbox")
            durable_assignment = connection.execute(
                """
                SELECT 1 FROM minion_v2_role_assignments
                WHERE workflow_id = ? AND aggregate_type = ? AND aggregate_id = ?
                  AND state IN (?, ?, ?, ?, ?)
                LIMIT 1
                """,
                (
                    str(workflow_id),
                    aggregate_type.value,
                    str(aggregate_id),
                    RoleAssignmentState.QUEUED.value,
                    RoleAssignmentState.CLAIMED.value,
                    RoleAssignmentState.RUNNING.value,
                    RoleAssignmentState.RETRY_QUEUED.value,
                    RoleAssignmentState.RESULT_RECORDED.value,
                ),
            ).fetchone()
            if durable_assignment is not None:
                sources.append("role_assignment")
        return tuple(sources)

    def record_artifact(
        self,
        ref: ArtifactRef,
        *,
        storage_path: Path,
        provenance: Mapping[str, Any],
        metadata: Mapping[str, Any],
        child_refs: tuple[tuple[str, str], ...],
    ) -> None:
        self.ensure_schema()
        if not storage_path.is_file():
            raise FileNotFoundError(storage_path)
        if storage_path.stat().st_size != ref.byte_size:
            raise IOError("artifact size changed before metadata publication")
        now = utc_now()
        with self._transaction() as connection:
            for child_sha, _relation in child_refs:
                child = connection.execute(
                    "SELECT durable FROM minion_v2_artifacts WHERE sha256 = ?",
                    (str(child_sha),),
                ).fetchone()
                if child is None or int(child["durable"]) != 1:
                    raise ValueError(f"child artifact is not durable: {child_sha}")
            existing = connection.execute(
                "SELECT artifact_type, schema_version, media_type, byte_size, storage_path FROM minion_v2_artifacts WHERE sha256 = ?",
                (ref.sha256,),
            ).fetchone()
            if existing is not None:
                expected = (ref.artifact_type, ref.schema_version, ref.media_type, ref.byte_size, str(storage_path))
                actual = (
                    str(existing["artifact_type"]),
                    str(existing["schema_version"]),
                    str(existing["media_type"]),
                    int(existing["byte_size"]),
                    str(existing["storage_path"]),
                )
                if actual != expected:
                    raise ValueError("typed artifact metadata is immutable")
            connection.execute(
                """
                INSERT INTO minion_v2_artifacts(
                    sha256, artifact_type, schema_version, media_type, byte_size,
                    storage_path, durable, provenance_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(sha256) DO NOTHING
                """,
                (
                    ref.sha256,
                    ref.artifact_type,
                    ref.schema_version,
                    ref.media_type,
                    ref.byte_size,
                    str(storage_path),
                    _json(dict(provenance)),
                    _json(dict(metadata)),
                    now,
                ),
            )
            for child_sha, relation in child_refs:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO minion_v2_artifact_refs(parent_sha256, child_sha256, relation, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (ref.sha256, str(child_sha), str(relation), now),
                )

    def artifact_is_durable(self, sha256: str) -> bool:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT durable, storage_path FROM minion_v2_artifacts WHERE sha256 = ?",
                (str(sha256).removeprefix("sha256:"),),
            ).fetchone()
            return bool(row is not None and int(row["durable"]) == 1 and Path(str(row["storage_path"])).is_file())

    def read_artifact_record(self, sha256: str) -> dict[str, Any] | None:
        self.ensure_schema()
        digest = str(sha256).removeprefix("sha256:")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_artifacts WHERE sha256 = ?",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["durable"] = bool(result["durable"])
            result["provenance"] = json.loads(str(result.pop("provenance_json")))
            result["metadata"] = json.loads(str(result.pop("metadata_json")))
            return result

    def read_latest_effect_result_artifact(
        self,
        *,
        workflow_id: str,
        aggregate_type: AggregateType,
        aggregate_id: str,
        effect_type: str,
    ) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT result_artifact_ref_json
                FROM minion_v2_outbox
                WHERE workflow_id = ? AND aggregate_type = ? AND aggregate_id = ?
                  AND effect_type = ? AND status = 'completed'
                  AND result_artifact_ref_json != '{}'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (str(workflow_id), aggregate_type.value, str(aggregate_id), str(effect_type)),
            ).fetchone()
            if row is None:
                return None
            value = json.loads(str(row["result_artifact_ref_json"] or "{}"))
            return dict(value) if isinstance(value, dict) and value.get("sha256") else None

    def issue_human_decision_token(
        self,
        *,
        workflow_id: str,
        architecture_revision_id: str,
        manifest_sha: str,
        actor_id: str,
    ) -> str:
        required = {
            "workflow_id": workflow_id,
            "architecture_revision_id": architecture_revision_id,
            "manifest_sha": manifest_sha,
            "actor_id": actor_id,
        }
        missing = [key for key, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"human decision token missing fields: {', '.join(missing)}")
        self.ensure_schema()
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = _utc_datetime()
        with self._transaction() as connection:
            artifact = connection.execute(
                "SELECT durable FROM minion_v2_artifacts WHERE sha256 = ?",
                (str(manifest_sha).removeprefix("sha256:"),),
            ).fetchone()
            if artifact is None or int(artifact["durable"]) != 1:
                raise ValueError("human decision token requires a durable architecture manifest")
            connection.execute(
                """
                UPDATE minion_v2_human_decisions
                SET status = 'expired'
                WHERE workflow_id = ? AND architecture_revision_id = ? AND status = 'issued'
                """,
                (workflow_id, architecture_revision_id),
            )
            connection.execute(
                """
                INSERT INTO minion_v2_human_decisions(
                    token_hash, workflow_id, architecture_revision_id, manifest_sha,
                    actor_id, expires_at, issued_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    workflow_id,
                    architecture_revision_id,
                    str(manifest_sha).removeprefix("sha256:"),
                    actor_id,
                    "",
                    now.isoformat(),
                ),
            )
        return token

    def inspect_human_decision_token(self, token: str) -> dict[str, Any] | None:
        self.ensure_schema()
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_human_decisions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result.pop("token_hash", None)
            return result

    def expire_human_decisions_for_revision(
        self,
        *,
        workflow_id: str,
        architecture_revision_id: str,
    ) -> int:
        self.ensure_schema()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE minion_v2_human_decisions
                SET status = 'expired'
                WHERE workflow_id = ? AND architecture_revision_id = ? AND status = 'issued'
                """,
                (str(workflow_id), str(architecture_revision_id)),
            )
            return int(cursor.rowcount)

    def reissue_human_decision_token(
        self,
        *,
        workflow_id: str,
        actor_id: str,
    ) -> str:
        """Replace the unique pending card token for a semantic/manual decision path."""

        required = {
            "workflow_id": workflow_id,
            "actor_id": actor_id,
        }
        missing = [key for key, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"human decision binding missing fields: {', '.join(missing)}")
        self.ensure_schema()
        now = _utc_datetime()
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM minion_v2_human_decisions
                WHERE workflow_id = ? AND actor_id = ? AND status = 'issued'
                ORDER BY issued_at DESC
                """,
                (str(workflow_id), str(actor_id)),
            ).fetchall()
            bindings = {
                (
                    str(row["architecture_revision_id"]),
                    str(row["manifest_sha"]),
                )
                for row in rows
            }
            if not bindings:
                raise ValueError("current workflow has no pending human decision")
            if len(bindings) != 1:
                raise ValueError("current workflow has multiple pending human decisions")
            architecture_revision_id, manifest_sha = next(iter(bindings))
            artifact = connection.execute(
                "SELECT durable FROM minion_v2_artifacts WHERE sha256 = ?",
                (manifest_sha,),
            ).fetchone()
            if artifact is None or int(artifact["durable"]) != 1:
                raise ValueError("pending human decision references a non-durable artifact")
            connection.execute(
                """
                UPDATE minion_v2_human_decisions
                SET status = 'expired'
                WHERE workflow_id = ? AND architecture_revision_id = ? AND manifest_sha = ?
                  AND actor_id = ? AND status = 'issued'
                """,
                (
                    str(workflow_id),
                    architecture_revision_id,
                    manifest_sha,
                    str(actor_id),
                ),
            )
            connection.execute(
                """
                INSERT INTO minion_v2_human_decisions(
                    token_hash, workflow_id, architecture_revision_id, manifest_sha,
                    actor_id, expires_at, issued_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    str(workflow_id),
                    architecture_revision_id,
                    manifest_sha,
                    str(actor_id),
                    "",
                    now.isoformat(),
                ),
            )
        return token

    def ensure_role_session(
        self,
        *,
        session_id: str,
        workflow_id: str,
        aggregate_type: AggregateType,
        aggregate_id: str,
        role: str,
        mode: str,
        role_profile_id: str,
        family_binding_sha: str,
        preferred_harness_id: str = "pal",
        preferred_harness_generation: str = "",
        scope_kind: str = "",
        subject_key: str = "",
    ) -> dict[str, Any]:
        values = {
            "session_id": str(session_id or "").strip(),
            "workflow_id": str(workflow_id or "").strip(),
            "aggregate_id": str(aggregate_id or "").strip(),
            "role": str(role or "").strip(),
            "mode": str(mode or "").strip(),
            "role_profile_id": str(role_profile_id or "").strip(),
            "family_binding_sha": str(family_binding_sha or "").strip(),
            "preferred_harness_id": str(
                preferred_harness_id or "pal"
            ).strip(),
            "preferred_harness_generation": str(
                preferred_harness_generation or ""
            ).strip(),
            "scope_kind": str(scope_kind or "").strip(),
            "subject_key": str(subject_key or "").strip(),
        }
        missing = [
            name
            for name in (
                "session_id",
                "workflow_id",
                "aggregate_id",
                "role",
                "mode",
                "role_profile_id",
                "family_binding_sha",
                "scope_kind",
                "subject_key",
            )
            if not values[name]
        ]
        if missing:
            raise ValueError("role session missing fields: " + ", ".join(missing))
        RoleActivation.from_values(values["role"], values["mode"])
        if "." not in values["role_profile_id"]:
            raise ValueError("role session role_profile_id must be canonical")
        self.ensure_schema()
        now = utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM minion_v2_role_sessions WHERE session_id = ?",
                (values["session_id"],),
            ).fetchone()
            identity = (
                values["workflow_id"],
                values["role"],
                values["role_profile_id"],
                values["family_binding_sha"],
                values["scope_kind"],
                values["subject_key"],
            )
            if existing is not None:
                actual = (
                    str(existing["workflow_id"]),
                    str(existing["role"]),
                    str(existing["role_profile_id"]),
                    str(existing["family_binding_sha"]),
                    str(existing["scope_kind"] or ""),
                    str(existing["subject_key"] or ""),
                )
                if actual != identity:
                    raise ValueError("role session identity is immutable")
                if str(existing["mode"] or "") != values["mode"]:
                    connection.execute(
                        """
                        UPDATE minion_v2_role_sessions
                        SET mode = ?, updated_at = ?
                        WHERE session_id = ?
                        """,
                        (values["mode"], now, values["session_id"]),
                    )
                    existing = connection.execute(
                        "SELECT * FROM minion_v2_role_sessions WHERE session_id = ?",
                        (values["session_id"],),
                    ).fetchone()
                return _decode_role_session(existing)
            connection.execute(
                """
                INSERT INTO minion_v2_role_sessions(
                    session_id, workflow_id, aggregate_type, aggregate_id, role, mode,
                    role_profile_id, preferred_harness_id,
                    preferred_harness_generation, family_binding_sha,
                    scope_kind, subject_key, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["session_id"],
                    values["workflow_id"],
                    aggregate_type.value,
                    values["aggregate_id"],
                    values["role"],
                    values["mode"],
                    values["role_profile_id"],
                    values["preferred_harness_id"],
                    values["preferred_harness_generation"],
                    values["family_binding_sha"],
                    values["scope_kind"],
                    values["subject_key"],
                    RoleSessionState.ACTIVE.value,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM minion_v2_role_sessions WHERE session_id = ?",
                (values["session_id"],),
            ).fetchone()
            return _decode_role_session(row)

    def read_role_session(self, session_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_role_sessions WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
        return _decode_role_session(row) if row is not None else None

    def complete_workflow_role_sessions(
        self,
        workflow_id: str,
        *,
        status: str = "completed",
    ) -> tuple[str, ...]:
        """Close logical role sessions only after the workflow itself is terminal."""

        self.ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT session_id FROM minion_v2_role_sessions
                WHERE workflow_id = ? AND status IN (?, ?)
                ORDER BY created_at, session_id
                """,
                (
                    str(workflow_id),
                    RoleSessionState.ACTIVE.value,
                    RoleSessionState.SUSPENDED.value,
                ),
            ).fetchall()
        completed: list[str] = []
        for row in rows:
            session_id = str(row["session_id"])
            if self.complete_role_session(session_id, status=status):
                completed.append(session_id)
        return tuple(completed)

    def reconcile_role_session_checkpoints(self) -> tuple[str, ...]:
        """Delete derived checkpoints whose durable role session is terminal."""

        self.ensure_schema()
        store = LogicalCoroutineCheckpointStore(self.runtime_root)
        checkpoint_ids = store.list_logical_coroutine_ids()
        if not checkpoint_ids:
            return ()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT session_id FROM minion_v2_role_sessions
                WHERE status IN (?, ?)
                """,
                (
                    RoleSessionState.ACTIVE.value,
                    RoleSessionState.SUSPENDED.value,
                ),
            ).fetchall()
        resumable = {str(row["session_id"]) for row in rows}
        retired: list[str] = []
        for session_id in checkpoint_ids:
            if session_id in resumable:
                continue
            store.delete(session_id)
            retired.append(session_id)
        return tuple(retired)

    def list_role_sessions(
        self,
        *,
        workflow_id: str,
        aggregate_type: AggregateType | str,
        aggregate_id: str,
        role: str = "",
    ) -> tuple[dict[str, Any], ...]:
        self.ensure_schema()
        clauses = ["workflow_id = ?", "aggregate_type = ?", "aggregate_id = ?"]
        parameters: list[Any] = [
            str(workflow_id),
            AggregateType(str(aggregate_type)).value,
            str(aggregate_id),
        ]
        if str(role or "").strip():
            clauses.append("role = ?")
            parameters.append(str(role))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM minion_v2_role_sessions WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at, session_id",
                tuple(parameters),
            ).fetchall()
        return tuple(_decode_role_session(row) for row in rows)

    def create_role_assignment(self, request: RoleAssignmentRequest) -> dict[str, Any]:
        self.ensure_schema()
        now = utc_now()
        with self._transaction() as connection:
            session = connection.execute(
                "SELECT * FROM minion_v2_role_sessions WHERE session_id = ?",
                (request.session_id,),
            ).fetchone()
            if session is None:
                raise ValueError("role assignment requires an existing role session")
            if str(session["status"]) in {
                RoleSessionState.COMPLETED.value,
                RoleSessionState.CANCELLED.value,
            }:
                raise ValueError("role assignment cannot use a terminal role session")
            session_identity = (
                str(session["workflow_id"]),
                str(session["role"]),
                str(session["role_profile_id"]),
                str(session["family_binding_sha"]),
            )
            request_identity = (
                request.workflow_id,
                request.role,
                request.role_profile_id,
                request.family_binding_sha,
            )
            if session_identity != request_identity:
                raise ValueError("role assignment does not match its session identity")
            self._assert_assignment_in_role_session_scope_locked(
                connection,
                session,
                request,
            )
            self._assert_artifact_refs_durable(connection, request.input_refs)
            existing = connection.execute(
                "SELECT * FROM minion_v2_role_assignments WHERE assignment_key = ?",
                (request.assignment_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["request_hash"]) != request.request_hash:
                    raise ValueError("role assignment key was reused with different inputs")
                return _decode_role_assignment(existing)
            open_assignment = connection.execute(
                """
                SELECT assignment_id
                FROM minion_v2_role_assignments
                WHERE session_id = ?
                  AND state IN (
                      'queued', 'claimed', 'running', 'retry_queued',
                      'result_recorded'
                  )
                LIMIT 1
                """,
                (request.session_id,),
            ).fetchone()
            if open_assignment is not None:
                raise ValueError(
                    "role session already has an open assignment: "
                    + str(open_assignment["assignment_id"])
                )
            connection.execute(
                """
                INSERT INTO minion_v2_role_assignments(
                    assignment_id, assignment_key, request_hash, session_id,
                    workflow_id, aggregate_type, aggregate_id, role, mode,
                    role_profile_id, family_binding_sha,
                    input_fingerprint, required_inputs_json, input_refs_json,
                    execution_spec_json, submission_kind, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.assignment_id,
                    request.assignment_key,
                    request.request_hash,
                    request.session_id,
                    request.workflow_id,
                    request.aggregate_type,
                    request.aggregate_id,
                    request.role,
                    request.mode,
                    request.role_profile_id,
                    request.family_binding_sha,
                    request.input_fingerprint,
                    _json(sorted(request.required_inputs)),
                    _json({name: dict(ref) for name, ref in request.input_refs.items()}),
                    _json(dict(request.execution_spec)),
                    request.submission_kind,
                    RoleAssignmentState.QUEUED.value,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM minion_v2_role_assignments WHERE assignment_id = ?",
                (request.assignment_id,),
            ).fetchone()
            return _decode_role_assignment(row)

    def _assert_assignment_in_role_session_scope_locked(
        self,
        connection: sqlite3.Connection,
        session: sqlite3.Row,
        request: RoleAssignmentRequest,
    ) -> None:
        scope_kind = str(session["scope_kind"] or "")
        subject_key = str(session["subject_key"] or "")
        snapshot = self._read_snapshot_locked(
            connection,
            AggregateType(str(request.aggregate_type)),
            request.aggregate_id,
        )
        if snapshot is None or snapshot.workflow_id != request.workflow_id:
            raise ValueError("role assignment aggregate is outside its workflow")
        if scope_kind == "architecture_cycle":
            if snapshot.aggregate_type != AggregateType.ARCHITECTURE_REVISION:
                raise ValueError("architecture-cycle session may only review architecture revisions")
            cycle_id = str(
                snapshot.payload.get("architecture_cycle_id")
                or snapshot.payload.get("root_architecture_revision_id")
                or snapshot.aggregate_id
            )
            if cycle_id != subject_key:
                raise ValueError("role assignment is outside its architecture cycle")
            return
        if scope_kind == "module":
            if snapshot.aggregate_type != AggregateType.DAG_NODE_RUN:
                raise ValueError("module session may only activate on DAG node runs")
            if str(snapshot.payload.get("module_name") or "") != subject_key:
                raise ValueError("role assignment is outside its module")
            return
        if scope_kind != request.aggregate_type or subject_key != request.aggregate_id:
            raise ValueError("role assignment is outside its aggregate-bound session")

    def claim_role_assignment(
        self,
        assignment_id: str,
        *,
        harness_id: str = "pal",
        harness_generation: str = "",
    ) -> dict[str, Any]:
        self.ensure_schema()
        with self._transaction() as connection:
            assignment = connection.execute(
                "SELECT * FROM minion_v2_role_assignments WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
            if assignment is None:
                raise KeyError(f"unknown role assignment: {assignment_id}")
            if str(assignment["state"]) not in {
                RoleAssignmentState.QUEUED.value,
                RoleAssignmentState.RETRY_QUEUED.value,
            }:
                raise ValueError("role assignment is not claimable")
            target_state = role_assignment_target(
                str(assignment["state"]),
                RoleAssignmentAction.CLAIM,
            )
            attempt_index = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(attempt_index), 0) + 1
                    FROM minion_v2_role_attempts WHERE assignment_id = ?
                    """,
                    (str(assignment_id),),
                ).fetchone()[0]
            )
            identifier = attempt_id(str(assignment_id), attempt_index)
            now = utc_now()
            connection.execute(
                """
                INSERT INTO minion_v2_role_attempts(
                    attempt_id, assignment_id, attempt_index, lease_resource_key,
                    fencing_token, harness_id, harness_generation,
                    status, started_at, updated_at
                ) VALUES (?, ?, ?, '', 0, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    str(assignment_id),
                    attempt_index,
                    str(harness_id or "pal"),
                    str(harness_generation or ""),
                    RoleAttemptState.STARTING.value,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE minion_v2_role_assignments
                SET state = ?, active_attempt_id = ?, last_error = '', updated_at = ?
                WHERE assignment_id = ?
                """,
                (
                    target_state.value,
                    identifier,
                    now,
                    str(assignment_id),
                ),
            )
            # The session is the logical coroutine while an attempt is only
            # its current process shell.  Pin the shell that actually claimed
            # the assignment, not merely the harness that was preferred when
            # the session was first created.  A fallback may select Pal after
            # an optional harness fails; later registry refreshes must then
            # restore the Pal-authored checkpoint with that same generation.
            connection.execute(
                """
                UPDATE minion_v2_role_sessions
                SET preferred_harness_id = ?,
                    preferred_harness_generation = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (
                    str(harness_id or "pal"),
                    str(harness_generation or ""),
                    now,
                    str(assignment["session_id"]),
                ),
            )
            return _decode_role_attempt(
                connection.execute(
                    "SELECT * FROM minion_v2_role_attempts WHERE attempt_id = ?",
                    (identifier,),
                ).fetchone()
            )

    def start_role_attempt(
        self,
        *,
        assignment_id: str,
        attempt_id_value: str,
        lease_resource_key: str,
        fencing_token: int,
        prompt_pack_ref: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.ensure_schema()
        with self._transaction() as connection:
            assignment, _attempt = self._role_assignment_attempt_locked(
                connection,
                assignment_id=assignment_id,
                attempt_id_value=attempt_id_value,
            )
            if str(assignment["state"]) != RoleAssignmentState.CLAIMED.value:
                raise ValueError("role assignment is not claimed")
            target_state = role_assignment_target(
                str(assignment["state"]),
                RoleAssignmentAction.START,
            )
            self._assert_lease_locked(
                connection,
                str(lease_resource_key),
                str(attempt_id_value),
                int(fencing_token),
            )
            self._assert_artifact_refs_durable(connection, prompt_pack_ref)
            now = utc_now()
            connection.execute(
                """
                UPDATE minion_v2_role_attempts
                SET lease_resource_key = ?, fencing_token = ?, status = ?,
                    prompt_pack_ref_json = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    str(lease_resource_key),
                    int(fencing_token),
                    RoleAttemptState.RUNNING.value,
                    _json(dict(prompt_pack_ref)),
                    now,
                    str(attempt_id_value),
                ),
            )
            connection.execute(
                """
                UPDATE minion_v2_role_assignments
                SET state = ?, updated_at = ? WHERE assignment_id = ?
                """,
                (
                    target_state.value,
                    now,
                    str(assignment_id),
                ),
            )
            self._transition_role_session_locked(
                connection,
                str(assignment["session_id"]),
                RoleSessionAction.ACTIVATE,
                now=now,
            )
            row = connection.execute(
                "SELECT * FROM minion_v2_role_attempts WHERE attempt_id = ?",
                (str(attempt_id_value),),
            ).fetchone()
            return _decode_role_attempt(row)

    def issue_role_attempt_access_token(
        self,
        *,
        assignment_id: str,
        attempt_id_value: str,
        fencing_token: int,
    ) -> str:
        """Issue one opaque token for the active process attempt.

        Only the token hash is durable. A later attempt always replaces the
        authorization surface, while the cognitive session remains reusable.
        """

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.ensure_schema()
        with self._transaction() as connection:
            assignment, attempt = self._role_assignment_attempt_locked(
                connection,
                assignment_id=assignment_id,
                attempt_id_value=attempt_id_value,
            )
            if str(assignment["state"]) != RoleAssignmentState.RUNNING.value:
                raise ValueError("role assignment is not running")
            self._assert_lease_locked(
                connection,
                str(attempt["lease_resource_key"]),
                str(attempt_id_value),
                int(fencing_token),
            )
            connection.execute(
                """
                UPDATE minion_v2_role_attempts
                SET access_token_hash = ?, updated_at = ?
                WHERE attempt_id = ? AND status = ?
                """,
                (
                    token_hash,
                    utc_now(),
                    str(attempt_id_value),
                    RoleAttemptState.RUNNING.value,
                ),
            )
        return token

    def update_role_attempt_process_group(
        self,
        *,
        assignment_id: str,
        attempt_id_value: str,
        fencing_token: int,
        process_group_id: int,
    ) -> None:
        self.ensure_schema()
        with self._transaction() as connection:
            _assignment, attempt = self._role_assignment_attempt_locked(
                connection,
                assignment_id=assignment_id,
                attempt_id_value=attempt_id_value,
            )
            self._assert_lease_locked(
                connection,
                str(attempt["lease_resource_key"]),
                str(attempt_id_value),
                int(fencing_token),
            )
            connection.execute(
                """
                UPDATE minion_v2_role_attempts
                SET process_group_id = ?, updated_at = ?
                WHERE attempt_id = ? AND status = ?
                """,
                (
                    max(0, int(process_group_id)),
                    utc_now(),
                    str(attempt_id_value),
                    RoleAttemptState.RUNNING.value,
                ),
            )

    def authenticate_role_attempt(self, access_token: str) -> dict[str, Any]:
        token = str(access_token or "").strip()
        if not token:
            raise ValueError("role assignment access token is required")
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    a.*,
                    t.attempt_id AS authenticated_attempt_id,
                    t.attempt_index AS authenticated_attempt_index,
                    t.lease_resource_key AS authenticated_lease_resource_key,
                    t.fencing_token AS authenticated_fencing_token,
                    t.status AS authenticated_attempt_status
                FROM minion_v2_role_attempts AS t
                JOIN minion_v2_role_assignments AS a
                  ON a.assignment_id = t.assignment_id
                WHERE t.access_token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                raise ValueError("role assignment access token is invalid")
            if str(row["active_attempt_id"]) != str(row["authenticated_attempt_id"]):
                raise StaleFencingToken("role assignment token belongs to a stale attempt")
            if str(row["state"]) not in {
                RoleAssignmentState.RUNNING.value,
                RoleAssignmentState.RESULT_RECORDED.value,
            }:
                raise ValueError("role assignment is not active")
            if str(row["authenticated_attempt_status"]) not in {
                RoleAttemptState.RUNNING.value,
                RoleAttemptState.SUBMITTED.value,
            }:
                raise StaleFencingToken("role assignment attempt is no longer active")
            self._assert_lease_locked(
                connection,
                str(row["authenticated_lease_resource_key"]),
                str(row["authenticated_attempt_id"]),
                int(row["authenticated_fencing_token"]),
            )
            assignment = _decode_role_assignment(row)
            return {
                "assignment": assignment,
                "attempt_id": str(row["authenticated_attempt_id"]),
                "attempt_index": int(row["authenticated_attempt_index"]),
                "lease_resource_key": str(row["authenticated_lease_resource_key"]),
                "fencing_token": int(row["authenticated_fencing_token"]),
            }

    def record_role_submission(
        self,
        *,
        assignment_id: str,
        attempt_id_value: str,
        fencing_token: int,
        artifact_ref: Mapping[str, Any],
        payload_hash: str,
        settlement_action: Mapping[str, Any],
    ) -> RoleSubmissionReceipt:
        if not str(payload_hash or "").strip():
            raise ValueError("role submission requires a payload hash")
        if not dict(settlement_action or {}).get("action_type"):
            raise ValueError("role submission requires a settlement action")
        self.ensure_schema()
        with self._transaction() as connection:
            assignment, attempt = self._role_assignment_attempt_locked(
                connection,
                assignment_id=assignment_id,
                attempt_id_value=attempt_id_value,
            )
            existing_ref = json.loads(
                str(assignment["submission_artifact_ref_json"] or "{}")
            )
            if existing_ref:
                existing = RoleSubmissionReceipt(
                    assignment_id=str(assignment_id),
                    artifact_ref=existing_ref,
                    payload_hash=str(assignment["submission_payload_hash"]),
                    settlement_action=json.loads(
                        str(assignment["settlement_action_json"] or "{}")
                    ),
                )
                requested = RoleSubmissionReceipt(
                    assignment_id=str(assignment_id),
                    artifact_ref=dict(artifact_ref),
                    payload_hash=str(payload_hash),
                    settlement_action=dict(settlement_action),
                )
                if existing.to_dict() != requested.to_dict():
                    raise ValueError(
                        "role assignment already has a different submission receipt"
                    )
                return existing
            if str(assignment["state"]) != RoleAssignmentState.RUNNING.value:
                raise ValueError("role assignment is not accepting a submission")
            target_state = role_assignment_target(
                str(assignment["state"]),
                RoleAssignmentAction.RECORD_RESULT,
            )
            self._assert_lease_locked(
                connection,
                str(attempt["lease_resource_key"]),
                str(attempt_id_value),
                int(fencing_token),
            )
            self._assert_artifact_refs_durable(connection, artifact_ref)
            now = utc_now()
            connection.execute(
                """
                UPDATE minion_v2_role_assignments
                SET state = ?, submission_artifact_ref_json = ?,
                    submission_payload_hash = ?, settlement_action_json = ?,
                    updated_at = ?
                WHERE assignment_id = ?
                """,
                (
                    target_state.value,
                    _json(dict(artifact_ref)),
                    str(payload_hash),
                    _json(dict(settlement_action)),
                    now,
                    str(assignment_id),
                ),
            )
            connection.execute(
                """
                UPDATE minion_v2_role_attempts
                SET status = ?, response_artifact_ref_json = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    RoleAttemptState.SUBMITTED.value,
                    _json(dict(artifact_ref)),
                    now,
                    str(attempt_id_value),
                ),
            )
            return RoleSubmissionReceipt(
                assignment_id=str(assignment_id),
                artifact_ref=dict(artifact_ref),
                payload_hash=str(payload_hash),
                settlement_action=dict(settlement_action),
            )

    def settle_role_assignment(
        self,
        *,
        assignment_id: str,
        submission_payload_hash: str,
    ) -> dict[str, Any]:
        self.ensure_schema()
        with self._transaction() as connection:
            self._settle_role_assignment_locked(
                connection,
                assignment_id=str(assignment_id),
                submission_payload_hash=str(submission_payload_hash),
            )
            row = connection.execute(
                "SELECT * FROM minion_v2_role_assignments WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
            return _decode_role_assignment(row)

    def _settle_role_assignment_locked(
        self,
        connection: sqlite3.Connection,
        *,
        assignment_id: str,
        submission_payload_hash: str,
        workflow_id: str = "",
        aggregate_type: str = "",
        aggregate_id: str = "",
    ) -> None:
        assignment = connection.execute(
            "SELECT * FROM minion_v2_role_assignments WHERE assignment_id = ?",
            (str(assignment_id),),
        ).fetchone()
        if assignment is None:
            raise KeyError(f"unknown role assignment: {assignment_id}")
        if str(assignment["submission_payload_hash"]) != str(
            submission_payload_hash
        ):
            raise ValueError("role assignment settlement receipt does not match")
        expected_binding = (str(workflow_id), str(aggregate_type), str(aggregate_id))
        if any(expected_binding) and expected_binding != (
            str(assignment["workflow_id"]),
            str(assignment["aggregate_type"]),
            str(assignment["aggregate_id"]),
        ):
            raise ValueError("role assignment settlement targets a different aggregate")
        if str(assignment["state"]) == RoleAssignmentState.SETTLED.value:
            return
        if str(assignment["state"]) != RoleAssignmentState.RESULT_RECORDED.value:
            raise ValueError("role assignment has no recorded submission")
        target_state = role_assignment_target(
            str(assignment["state"]),
            RoleAssignmentAction.SETTLE,
        )
        now = utc_now()
        connection.execute(
            """
            UPDATE minion_v2_role_assignments
            SET state = ?, updated_at = ? WHERE assignment_id = ?
            """,
            (target_state.value, now, str(assignment_id)),
        )
        connection.execute(
            """
            UPDATE minion_v2_role_attempts
            SET status = CASE WHEN status = ? THEN ? ELSE status END,
                access_token_hash = '', finished_at = ?, updated_at = ?
            WHERE attempt_id = ?
            """,
            (
                RoleAttemptState.SUBMITTED.value,
                RoleAttemptState.COMPLETED.value,
                now,
                now,
                str(assignment["active_attempt_id"]),
            ),
        )
        attempt = connection.execute(
            "SELECT * FROM minion_v2_role_attempts WHERE attempt_id = ?",
            (str(assignment["active_attempt_id"]),),
        ).fetchone()
        if attempt is not None:
            lease_resource = str(attempt["lease_resource_key"] or "")
            fencing_token = int(attempt["fencing_token"] or 0)
            if lease_resource and fencing_token:
                connection.execute(
                    """
                    DELETE FROM minion_v2_leases
                    WHERE resource_key = ? AND owner_id = ? AND fencing_token = ?
                    """,
                    (
                        lease_resource,
                        str(assignment["active_attempt_id"]),
                        fencing_token,
                    ),
                )
        self._transition_role_session_locked(
            connection,
            str(assignment["session_id"]),
            RoleSessionAction.SUSPEND,
            now=now,
        )

    def queue_role_attempt_retry(
        self,
        *,
        assignment_id: str,
        attempt_id_value: str,
        error_kind: str,
        error_text: str,
    ) -> dict[str, Any]:
        self.ensure_schema()
        with self._transaction() as connection:
            assignment, attempt = self._role_assignment_attempt_locked(
                connection,
                assignment_id=assignment_id,
                attempt_id_value=attempt_id_value,
            )
            if str(assignment["state"]) in {
                RoleAssignmentState.SETTLED.value,
                RoleAssignmentState.CANCELLED.value,
                RoleAssignmentState.RESULT_RECORDED.value,
            }:
                return _decode_role_assignment(assignment)
            now = utc_now()
            target = role_assignment_target(
                str(assignment["state"]),
                RoleAssignmentAction.QUEUE_RETRY,
            )
            connection.execute(
                """
                UPDATE minion_v2_role_attempts
                SET status = ?, error_kind = ?, error_text = ?,
                    finished_at = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    RoleAttemptState.LOST.value,
                    str(error_kind),
                    str(error_text),
                    now,
                    now,
                    str(attempt_id_value),
                ),
            )
            connection.execute(
                """
                UPDATE minion_v2_role_assignments
                SET state = ?, last_error = ?, updated_at = ?
                WHERE assignment_id = ?
                """,
                (target.value, str(error_text), now, str(assignment_id)),
            )
            row = connection.execute(
                "SELECT * FROM minion_v2_role_assignments WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
            return _decode_role_assignment(row)

    def record_role_failure_result(
        self,
        *,
        assignment_id: str,
        attempt_id_value: str,
        error_kind: str,
        error_text: str,
        failure_artifact_ref: Mapping[str, Any],
        payload_hash: str,
        settlement_action: Mapping[str, Any],
    ) -> RoleSubmissionReceipt:
        """Record an exhausted activation failure as a normal durable result.

        The assignment owns only this receipt. The settlement action advances
        the parent aggregate to its explicit failure state in a separate,
        atomically acknowledged dispatch.
        """

        if not str(payload_hash or "").strip():
            raise ValueError("role failure result requires a payload hash")
        if str(dict(settlement_action or {}).get("action_type") or "") != "ROLE_FAILED":
            raise ValueError("role failure result must settle through ROLE_FAILED")
        self.ensure_schema()
        with self._transaction() as connection:
            assignment, attempt = self._role_assignment_attempt_locked(
                connection,
                assignment_id=assignment_id,
                attempt_id_value=attempt_id_value,
            )
            existing_ref = json.loads(
                str(assignment["submission_artifact_ref_json"] or "{}")
            )
            if existing_ref:
                receipt = RoleSubmissionReceipt(
                    assignment_id=str(assignment_id),
                    artifact_ref=existing_ref,
                    payload_hash=str(assignment["submission_payload_hash"]),
                    settlement_action=json.loads(
                        str(assignment["settlement_action_json"] or "{}")
                    ),
                )
                requested = RoleSubmissionReceipt(
                    assignment_id=str(assignment_id),
                    artifact_ref=dict(failure_artifact_ref),
                    payload_hash=str(payload_hash),
                    settlement_action=dict(settlement_action),
                )
                if receipt.to_dict() != requested.to_dict():
                    raise ValueError(
                        "role assignment already has a different result receipt"
                    )
                return receipt
            target_state = role_assignment_target(
                str(assignment["state"]),
                RoleAssignmentAction.RECORD_RESULT,
            )
            self._assert_artifact_refs_durable(connection, failure_artifact_ref)
            now = utc_now()
            connection.execute(
                """
                UPDATE minion_v2_role_attempts
                SET status = ?, error_kind = ?, error_text = ?,
                    response_artifact_ref_json = ?, access_token_hash = '',
                    finished_at = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    RoleAttemptState.FAILED.value,
                    str(error_kind),
                    str(error_text),
                    _json(dict(failure_artifact_ref)),
                    now,
                    now,
                    str(attempt_id_value),
                ),
            )
            connection.execute(
                """
                UPDATE minion_v2_role_assignments
                SET state = ?, submission_artifact_ref_json = ?,
                    submission_payload_hash = ?, settlement_action_json = ?,
                    last_error = ?, updated_at = ?
                WHERE assignment_id = ?
                """,
                (
                    target_state.value,
                    _json(dict(failure_artifact_ref)),
                    str(payload_hash),
                    _json(dict(settlement_action)),
                    str(error_text),
                    now,
                    str(assignment_id),
                ),
            )
            lease_resource = str(attempt["lease_resource_key"] or "")
            fencing_token = int(attempt["fencing_token"] or 0)
            if lease_resource and fencing_token:
                connection.execute(
                    """
                    DELETE FROM minion_v2_leases
                    WHERE resource_key = ? AND owner_id = ? AND fencing_token = ?
                    """,
                    (lease_resource, str(attempt_id_value), fencing_token),
                )
            self._transition_role_session_locked(
                connection,
                str(assignment["session_id"]),
                RoleSessionAction.SUSPEND,
                now=now,
            )
            return RoleSubmissionReceipt(
                assignment_id=str(assignment_id),
                artifact_ref=dict(failure_artifact_ref),
                payload_hash=str(payload_hash),
                settlement_action=dict(settlement_action),
            )

    def cancel_role_assignments(
        self,
        *,
        workflow_id: str,
        aggregate_type: AggregateType | str,
        aggregate_id: str,
        reason: str,
        exclude_assignment_id: str = "",
    ) -> tuple[dict[str, Any], ...]:
        """Terminate nonterminal invocations bound to one aggregate.

        Pause and cancel both end the current assignment. The durable worker
        session remains resumable unless the caller separately closes it.
        """

        aggregate_type_value = (
            aggregate_type.value
            if isinstance(aggregate_type, AggregateType)
            else str(aggregate_type)
        )
        cancellable_states = (
            RoleAssignmentState.QUEUED.value,
            RoleAssignmentState.CLAIMED.value,
            RoleAssignmentState.RUNNING.value,
            RoleAssignmentState.RETRY_QUEUED.value,
            RoleAssignmentState.RESULT_RECORDED.value,
        )
        self.ensure_schema()
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM minion_v2_role_assignments
                WHERE workflow_id = ? AND aggregate_type = ? AND aggregate_id = ?
                  AND assignment_id != ?
                  AND state IN (?, ?, ?, ?, ?)
                ORDER BY created_at, assignment_id
                """,
                (
                    str(workflow_id),
                    aggregate_type_value,
                    str(aggregate_id),
                    str(exclude_assignment_id),
                    *cancellable_states,
                ),
            ).fetchall()
            now = utc_now()
            for assignment in rows:
                if str(assignment["state"]) == RoleAssignmentState.RESULT_RECORDED.value:
                    self._settle_role_assignment_locked(
                        connection,
                        assignment_id=str(assignment["assignment_id"]),
                        submission_payload_hash=str(
                            assignment["submission_payload_hash"]
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE minion_v2_role_assignments
                        SET last_error = ?, updated_at = ? WHERE assignment_id = ?
                        """,
                        (str(reason), now, str(assignment["assignment_id"])),
                    )
                    continue
                target_state = role_assignment_target(
                    str(assignment["state"]),
                    RoleAssignmentAction.CANCEL,
                )
                attempt_id_value = str(assignment["active_attempt_id"] or "")
                if attempt_id_value:
                    attempt = connection.execute(
                        "SELECT * FROM minion_v2_role_attempts WHERE attempt_id = ?",
                        (attempt_id_value,),
                    ).fetchone()
                    if attempt is not None:
                        connection.execute(
                            """
                            UPDATE minion_v2_role_attempts
                            SET status = ?, access_token_hash = '', error_kind = ?,
                                error_text = ?, finished_at = ?, updated_at = ?
                            WHERE attempt_id = ?
                            """,
                            (
                                RoleAttemptState.CANCELLED.value,
                                "aggregate_control",
                                str(reason),
                                now,
                                now,
                                attempt_id_value,
                            ),
                        )
                        lease_resource = str(attempt["lease_resource_key"] or "")
                        fencing_token = int(attempt["fencing_token"] or 0)
                        if lease_resource and fencing_token:
                            connection.execute(
                                """
                                DELETE FROM minion_v2_leases
                                WHERE resource_key = ? AND owner_id = ? AND fencing_token = ?
                                """,
                                (lease_resource, attempt_id_value, fencing_token),
                            )
                connection.execute(
                    """
                    UPDATE minion_v2_role_assignments
                    SET state = ?, last_error = ?, updated_at = ?
                    WHERE assignment_id = ?
                    """,
                    (
                        target_state.value,
                        str(reason),
                        now,
                        str(assignment["assignment_id"]),
                    ),
                )
                self._transition_role_session_locked(
                    connection,
                    str(assignment["session_id"]),
                    RoleSessionAction.SUSPEND,
                    now=now,
                )
            if not rows:
                return ()
            identifiers = tuple(str(row["assignment_id"]) for row in rows)
            placeholders = ",".join("?" for _ in identifiers)
            updated = connection.execute(
                "SELECT * FROM minion_v2_role_assignments "
                f"WHERE assignment_id IN ({placeholders}) ORDER BY created_at, assignment_id",
                identifiers,
            ).fetchall()
            return tuple(_decode_role_assignment(row) for row in updated)

    def read_role_assignment(self, assignment_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_role_assignments WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
            return _decode_role_assignment(row) if row is not None else None

    def read_role_attempt(self, attempt_id_value: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_role_attempts WHERE attempt_id = ?",
                (str(attempt_id_value),),
            ).fetchone()
            return _decode_role_attempt(row) if row is not None else None

    def read_latest_completed_role_harness_attempt(
        self,
        *,
        session_id: str,
        harness_id: str,
    ) -> dict[str, Any] | None:
        """Return the process shell that authored the resumable session state."""

        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT attempt.*
                FROM minion_v2_role_attempts AS attempt
                JOIN minion_v2_role_assignments AS assignment
                  ON assignment.assignment_id = attempt.assignment_id
                WHERE assignment.session_id = ?
                  AND attempt.harness_id = ?
                  AND attempt.status = ?
                ORDER BY attempt.finished_at DESC, attempt.started_at DESC,
                         attempt.attempt_index DESC
                LIMIT 1
                """,
                (
                    str(session_id),
                    str(harness_id),
                    RoleAttemptState.COMPLETED.value,
                ),
            ).fetchone()
        return _decode_role_attempt(row) if row is not None else None

    def read_role_harness_continuation(
        self,
        *,
        session_id: str,
        harness_id: str,
        harness_generation: str,
    ) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT attempt.harness_state_json
                FROM minion_v2_role_attempts AS attempt
                JOIN minion_v2_role_assignments AS assignment
                  ON assignment.assignment_id = attempt.assignment_id
                WHERE assignment.session_id = ? AND attempt.harness_id = ?
                  AND attempt.harness_generation = ?
                  AND attempt.harness_state_json != '{}'
                ORDER BY attempt.started_at DESC, attempt.attempt_index DESC
                LIMIT 1
                """,
                (
                    str(session_id),
                    str(harness_id),
                    str(harness_generation),
                ),
            ).fetchone()
        if row is None:
            return {}
        value = json.loads(str(row["harness_state_json"] or "{}"))
        return dict(value) if isinstance(value, Mapping) else {}

    def write_role_attempt_harness_state(
        self,
        *,
        assignment_id: str,
        attempt_id_value: str,
        fencing_token: int,
        harness_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.ensure_schema()
        with self._transaction() as connection:
            _assignment, attempt = self._role_assignment_attempt_locked(
                connection,
                assignment_id=str(assignment_id),
                attempt_id_value=str(attempt_id_value),
            )
            self._assert_lease_locked(
                connection,
                str(attempt["lease_resource_key"]),
                str(attempt_id_value),
                int(fencing_token),
            )
            if str(attempt["status"]) != RoleAttemptState.RUNNING.value:
                raise ValueError("role attempt is not running")
            state = dict(harness_state or {})
            encoded = _json(state)
            if len(encoded.encode("utf-8")) > 64 * 1024:
                raise ValueError("harness continuation exceeds 64 KiB")
            connection.execute(
                """
                UPDATE minion_v2_role_attempts
                SET harness_state_json = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (encoded, utc_now(), str(attempt_id_value)),
            )
            return state

    def list_role_assignments(
        self,
        *,
        workflow_id: str = "",
        states: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], ...]:
        self.ensure_schema()
        clauses: list[str] = []
        parameters: list[Any] = []
        if workflow_id:
            clauses.append("workflow_id = ?")
            parameters.append(str(workflow_id))
        if states:
            clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
            parameters.extend(str(item) for item in states)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM minion_v2_role_assignments"
                + where
                + " ORDER BY created_at, assignment_id",
                tuple(parameters),
            ).fetchall()
        return tuple(_decode_role_assignment(row) for row in rows)

    def list_role_attempts(self, assignment_id: str) -> tuple[dict[str, Any], ...]:
        self.ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM minion_v2_role_attempts
                WHERE assignment_id = ?
                ORDER BY attempt_index
                """,
                (str(assignment_id),),
            ).fetchall()
        return tuple(_decode_role_attempt(row) for row in rows)

    @staticmethod
    def _role_assignment_attempt_locked(
        connection: sqlite3.Connection,
        *,
        assignment_id: str,
        attempt_id_value: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        assignment = connection.execute(
            "SELECT * FROM minion_v2_role_assignments WHERE assignment_id = ?",
            (str(assignment_id),),
        ).fetchone()
        if assignment is None:
            raise KeyError(f"unknown role assignment: {assignment_id}")
        attempt = connection.execute(
            """
            SELECT * FROM minion_v2_role_attempts
            WHERE attempt_id = ? AND assignment_id = ?
            """,
            (str(attempt_id_value), str(assignment_id)),
        ).fetchone()
        if attempt is None or str(assignment["active_attempt_id"]) != str(
            attempt_id_value
        ):
            raise ValueError("role attempt is not active for this assignment")
        return assignment, attempt

    def record_role_invocation(
        self,
        *,
        invocation_id: str,
        workflow_id: str,
        aggregate_type: AggregateType,
        aggregate_id: str,
        lease_resource_key: str,
        fencing_token: int,
        role: str,
        mode: str,
        role_profile_id: str,
        harness_id: str = "pal",
        harness_generation: str = "",
        family_binding_sha: str,
        authoring_contract_version: str,
        prompt_pack_ref: Mapping[str, Any],
    ) -> None:
        self.assert_fencing_token(lease_resource_key, invocation_id, fencing_token)
        self.ensure_schema()
        now = utc_now()
        with self._transaction() as connection:
            self._assert_artifact_refs_durable(connection, prompt_pack_ref)
            connection.execute(
                """
                INSERT INTO minion_v2_role_invocations(
                    invocation_id, workflow_id, aggregate_type, aggregate_id, lease_resource_key,
                    fencing_token, role, mode, role_profile_id, harness_id,
                    harness_generation, family_binding_sha,
                    authoring_contract_version, prompt_pack_ref_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                ON CONFLICT(invocation_id) DO UPDATE SET
                    workflow_id = excluded.workflow_id,
                    aggregate_type = excluded.aggregate_type,
                    aggregate_id = excluded.aggregate_id,
                    lease_resource_key = excluded.lease_resource_key,
                    fencing_token = excluded.fencing_token,
                    role = excluded.role,
                    mode = excluded.mode,
                    role_profile_id = excluded.role_profile_id,
                    harness_id = excluded.harness_id,
                    harness_generation = excluded.harness_generation,
                    family_binding_sha = excluded.family_binding_sha,
                    authoring_contract_version = excluded.authoring_contract_version,
                    prompt_pack_ref_json = excluded.prompt_pack_ref_json,
                    status = 'running',
                    updated_at = excluded.updated_at
                """,
                (
                    invocation_id,
                    workflow_id,
                    aggregate_type.value,
                    aggregate_id,
                    lease_resource_key,
                    fencing_token,
                    role,
                    mode,
                    role_profile_id,
                    str(harness_id or "pal"),
                    str(harness_generation or ""),
                    family_binding_sha,
                    str(authoring_contract_version),
                    _json(dict(prompt_pack_ref)),
                    now,
                    now,
                ),
            )

    def read_role_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_role_invocations WHERE invocation_id = ?",
                (str(invocation_id),),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        raw = str(value.pop("prompt_pack_ref_json", "{}") or "{}")
        value["prompt_pack_ref"] = json.loads(raw)
        return value

    def suspend_role_invocation(
        self,
        *,
        invocation_id: str,
        fencing_token: int,
        status: str = "suspended",
    ) -> None:
        normalized = str(status or "suspended").strip().lower()
        if normalized not in {"suspended", "interrupted"}:
            raise ValueError("worker suspension status must be suspended or interrupted")
        self.ensure_schema()
        with self._transaction() as connection:
            invocation = connection.execute(
                "SELECT * FROM minion_v2_role_invocations WHERE invocation_id = ?",
                (str(invocation_id),),
            ).fetchone()
            if invocation is None:
                raise KeyError(f"unknown role invocation: {invocation_id}")
            self._assert_lease_locked(
                connection,
                str(invocation["lease_resource_key"]),
                str(invocation_id),
                int(fencing_token),
            )
            now = utc_now()
            connection.execute(
                """
                UPDATE minion_v2_role_invocations
                SET status = ?, updated_at = ?
                WHERE invocation_id = ?
                """,
                (normalized, now, str(invocation_id)),
            )
            self._transition_role_session_locked(
                connection,
                str(invocation_id),
                RoleSessionAction.SUSPEND,
                now=now,
            )
            self._rebuild_workflow_projection_locked(connection, str(invocation["workflow_id"]), "")

    def complete_role_session(self, session_id: str, *, status: str = "completed") -> bool:
        normalized = str(status or "completed").strip().lower()
        if normalized not in {"completed", "cancelled"}:
            raise ValueError("role session completion status must be completed or cancelled")
        self.ensure_schema()
        completed = False
        with self._transaction() as connection:
            invocation = connection.execute(
                "SELECT * FROM minion_v2_role_invocations WHERE invocation_id = ?",
                (str(session_id),),
            ).fetchone()
            session = connection.execute(
                "SELECT * FROM minion_v2_role_sessions WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
            if invocation is None and session is None:
                completed = False
            elif (
                (invocation is None or str(invocation["status"]) == normalized)
                and (session is None or str(session["status"]) == normalized)
            ):
                completed = True
            else:
                owner = session if session is not None else invocation
                assignments = connection.execute(
                    "SELECT state FROM minion_v2_role_assignments WHERE session_id = ?",
                    (str(session_id),),
                ).fetchall()
                states = {str(row["state"]) for row in assignments}
                if states - {
                    RoleAssignmentState.SETTLED.value,
                    RoleAssignmentState.CANCELLED.value,
                }:
                    raise ValueError("role session cannot complete with a non-terminal assignment")
                if session is not None:
                    self._assert_role_session_scope_terminal_locked(
                        connection,
                        session,
                        cancelled=normalized == RoleSessionState.CANCELLED.value,
                    )
                else:
                    snapshot = self._read_snapshot_locked(
                        connection,
                        AggregateType(str(owner["aggregate_type"])),
                        str(owner["aggregate_id"]),
                    )
                    if snapshot is None or snapshot.state not in {
                        "ACCEPTED",
                        "REJECTED",
                        "CANCELLED",
                    }:
                        raise ValueError(
                            "legacy role invocation cannot complete before its aggregate is terminal"
                        )
                now = utc_now()
                if invocation is not None:
                    connection.execute(
                        "UPDATE minion_v2_role_invocations SET status = ?, updated_at = ? WHERE invocation_id = ?",
                        (normalized, now, str(session_id)),
                    )
                if session is not None:
                    self._transition_role_session_locked(
                        connection,
                        str(session_id),
                        (
                            RoleSessionAction.COMPLETE
                            if normalized == RoleSessionState.COMPLETED.value
                            else RoleSessionAction.CANCEL
                        ),
                        now=now,
                    )
                self._rebuild_workflow_projection_locked(connection, str(owner["workflow_id"]), "")
                completed = True
        # The database transition is the durable authority. Delete the
        # encrypted worker payload only after COMMIT succeeds, so a failed
        # transition cannot strand an otherwise resumable coroutine.
        if completed:
            LogicalCoroutineCheckpointStore(self.runtime_root).delete(
                str(session_id)
            )
        return completed

    def _assert_role_session_scope_terminal_locked(
        self,
        connection: sqlite3.Connection,
        session: sqlite3.Row,
        *,
        cancelled: bool,
    ) -> None:
        workflow_id = str(session["workflow_id"])
        workflow = self._read_snapshot_locked(
            connection,
            AggregateType.WORKFLOW,
            workflow_id,
        )
        workflow_terminal = workflow is not None and workflow.state in {
            "COMPLETED",
            "REJECTED",
            "CANCELLED",
        }
        role = str(session["role"] or "")
        scope_kind = str(session["scope_kind"] or "")
        subject_key = str(session["subject_key"] or "")
        if scope_kind == "module":
            if cancelled and self._module_absent_from_latest_epoch_locked(
                connection,
                workflow_id=workflow_id,
                module_name=subject_key,
            ):
                return
            if not workflow_terminal:
                raise ValueError(
                    "module role session lives for its Module identity and "
                    "cannot complete before that Module is deleted"
                )
            return
        if scope_kind == "architecture_cycle":
            rows = connection.execute(
                """
                SELECT * FROM minion_v2_aggregate_snapshots
                WHERE workflow_id = ? AND aggregate_type = ?
                ORDER BY created_at, aggregate_id
                """,
                (workflow_id, AggregateType.ARCHITECTURE_REVISION.value),
            ).fetchall()
            snapshots = [_snapshot_from_row(row) for row in rows]
            revisions = [
                revision
                for revision in snapshots
                if str(
                    revision.payload.get("architecture_cycle_id")
                    or revision.payload.get("root_architecture_revision_id")
                    or revision.aggregate_id
                )
                == subject_key
            ]
            terminal_states = {"ACCEPTED", "REJECTED", "CANCELLED"}
            if not revisions or revisions[-1].state not in terminal_states:
                raise ValueError(
                    "architecture role session cannot complete while its correction cycle is open"
                )
            return
        snapshot = self._read_snapshot_locked(
            connection,
            AggregateType(str(session["aggregate_type"])),
            str(session["aggregate_id"]),
        )
        allowed = {"ACCEPTED", "REJECTED", "CANCELLED"}
        if snapshot is None or snapshot.state not in allowed:
            raise ValueError(
                "aggregate-bound role session cannot complete before its aggregate is terminal"
            )

    def _module_absent_from_latest_epoch_locked(
        self,
        connection: sqlite3.Connection,
        *,
        workflow_id: str,
        module_name: str,
    ) -> bool:
        epoch_row = connection.execute(
            """
            SELECT * FROM minion_v2_aggregate_snapshots
            WHERE workflow_id = ? AND aggregate_type = ?
            ORDER BY created_at DESC, aggregate_id DESC
            LIMIT 1
            """,
            (str(workflow_id), AggregateType.EXECUTION_EPOCH.value),
        ).fetchone()
        if epoch_row is None:
            return False
        epoch = _snapshot_from_row(epoch_row)
        node_rows = connection.execute(
            """
            SELECT * FROM minion_v2_aggregate_snapshots
            WHERE workflow_id = ? AND aggregate_type = ?
            """,
            (str(workflow_id), AggregateType.DAG_NODE_RUN.value),
        ).fetchall()
        for row in node_rows:
            node = _snapshot_from_row(row)
            if str(node.payload.get("epoch_id") or "") != epoch.aggregate_id:
                continue
            subject = str(
                node.payload.get("module_name")
                or node.payload.get("unit_id")
                or ""
            )
            if subject == str(module_name):
                return False
        return True

    @staticmethod
    def _transition_role_session_locked(
        connection: sqlite3.Connection,
        session_id: str,
        action: RoleSessionAction,
        *,
        now: str,
    ) -> RoleSessionState:
        session = connection.execute(
            "SELECT status FROM minion_v2_role_sessions WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
        if session is None:
            raise KeyError(f"unknown role session: {session_id}")
        target = role_session_target(str(session["status"]), action)
        connection.execute(
            "UPDATE minion_v2_role_sessions SET status = ?, updated_at = ? WHERE session_id = ?",
            (target.value, str(now), str(session_id)),
        )
        return target

    def record_worker_event(self, event: Mapping[str, Any]) -> None:
        invocation_id = str(event.get("invocation_id") or event.get("minion_id") or "").strip()
        if not invocation_id:
            return
        event_kind = str(event.get("event_kind") or "progress")
        payload = dict(event.get("payload") or {})
        phase = str(payload.get("phase") or "")
        round_index = int(payload.get("round") or 0)
        tool_call_count = int(payload.get("tool_call_count") or 0)
        created_at = str(event.get("created_at") or utc_now())
        self.ensure_schema()
        with self._transaction() as connection:
            invocation = connection.execute(
                "SELECT status FROM minion_v2_role_invocations WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
            if invocation is None:
                return
            connection.execute(
                """
                INSERT INTO minion_v2_worker_events(
                    invocation_id, event_kind, phase, round_index, tool_call_count, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (invocation_id, event_kind, phase, round_index, tool_call_count, _json(payload), created_at),
            )
            if event_kind == "progress" and phase == "llm_round_completed":
                connection.execute(
                    """
                    UPDATE minion_v2_role_invocations
                    SET last_completed_turn = MAX(last_completed_turn, ?), updated_at = ?
                    WHERE invocation_id = ?
                    """,
                    (round_index, created_at, invocation_id),
                )

    def record_role_turn(
        self,
        *,
        invocation_id: str,
        fencing_token: int,
        turn_index: int,
        llm_request_ref: Mapping[str, Any],
        llm_response_ref: Mapping[str, Any],
        tool_summary_ref: Mapping[str, Any] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float = 0,
        latency_ms: int = 0,
        tool_latency_ms: int = 0,
        wall_latency_ms: int = 0,
    ) -> None:
        self.ensure_schema()
        with self._transaction() as connection:
            invocation = connection.execute(
                "SELECT * FROM minion_v2_role_invocations WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
            if invocation is None:
                raise KeyError(f"unknown role invocation: {invocation_id}")
            self._assert_lease_locked(
                connection,
                str(invocation["lease_resource_key"]),
                invocation_id,
                fencing_token,
            )
            refs = {
                "request": dict(llm_request_ref),
                "response": dict(llm_response_ref),
                "tools": dict(tool_summary_ref or {}),
            }
            self._assert_artifact_refs_durable(connection, refs)
            now = utc_now()
            connection.execute(
                """
                INSERT INTO minion_v2_role_turns(
                    invocation_id, turn_index, llm_request_ref_json, llm_response_ref_json,
                    tool_summary_ref_json, input_tokens, output_tokens, cost, latency_ms,
                    tool_latency_ms, wall_latency_ms, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(invocation_id, turn_index) DO NOTHING
                """,
                (
                    invocation_id,
                    int(turn_index),
                    _json(dict(llm_request_ref)),
                    _json(dict(llm_response_ref)),
                    _json(dict(tool_summary_ref or {})),
                    max(0, int(input_tokens)),
                    max(0, int(output_tokens)),
                    max(0.0, float(cost)),
                    max(0, int(latency_ms)),
                    max(0, int(tool_latency_ms)),
                    max(0, int(wall_latency_ms)),
                    now,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] == 1:
                connection.execute(
                    """
                    UPDATE minion_v2_role_invocations
                    SET last_completed_turn = MAX(last_completed_turn, ?),
                        total_input_tokens = total_input_tokens + ?,
                        total_output_tokens = total_output_tokens + ?,
                        total_cost = total_cost + ?, total_latency_ms = total_latency_ms + ?,
                        total_tool_latency_ms = total_tool_latency_ms + ?,
                        total_wall_latency_ms = total_wall_latency_ms + ?,
                        updated_at = ?
                    WHERE invocation_id = ?
                    """,
                    (
                        int(turn_index),
                        max(0, int(input_tokens)),
                        max(0, int(output_tokens)),
                        max(0.0, float(cost)),
                        max(0, int(latency_ms)),
                        max(0, int(tool_latency_ms)),
                        max(0, int(wall_latency_ms)),
                        now,
                        invocation_id,
                    ),
                )
            self._rebuild_workflow_projection_locked(
                connection,
                str(invocation["workflow_id"]),
                "",
            )

    def finish_role_invocation(self, *, invocation_id: str, fencing_token: int, status: str) -> None:
        normalized = str(status or "").strip().lower()
        if normalized not in {"completed", "failed", "cancelled"}:
            raise ValueError(f"invalid role invocation terminal status: {status}")
        self.ensure_schema()
        with self._transaction() as connection:
            invocation = connection.execute(
                "SELECT * FROM minion_v2_role_invocations WHERE invocation_id = ?",
                (str(invocation_id),),
            ).fetchone()
            if invocation is None:
                raise KeyError(f"unknown role invocation: {invocation_id}")
            self._assert_lease_locked(
                connection,
                str(invocation["lease_resource_key"]),
                str(invocation_id),
                int(fencing_token),
            )
            connection.execute(
                "UPDATE minion_v2_role_invocations SET status = ?, updated_at = ? WHERE invocation_id = ?",
                (normalized, utc_now(), str(invocation_id)),
            )
            self._rebuild_workflow_projection_locked(connection, str(invocation["workflow_id"]), "")

    def update_node_journal(
        self,
        *,
        node_run_id: str,
        workflow_id: str,
        lease_resource_key: str,
        owner_id: str,
        fencing_token: int,
        expected_generation: int,
        journal: Mapping[str, Any],
    ) -> int:
        self.ensure_schema()
        with self._transaction() as connection:
            self._assert_lease_locked(connection, lease_resource_key, owner_id, fencing_token)
            row = connection.execute(
                "SELECT generation FROM minion_v2_node_journals WHERE node_run_id = ?",
                (node_run_id,),
            ).fetchone()
            current_generation = int(row["generation"]) if row is not None else 0
            if current_generation != int(expected_generation):
                raise AggregateVersionConflict(
                    f"expected journal generation {expected_generation}, found {current_generation}"
                )
            next_generation = current_generation + 1
            connection.execute(
                """
                INSERT INTO minion_v2_node_journals(
                    node_run_id, workflow_id, lease_resource_key, fencing_token,
                    generation, journal_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_run_id) DO UPDATE SET
                    lease_resource_key = excluded.lease_resource_key,
                    fencing_token = excluded.fencing_token,
                    generation = excluded.generation,
                    journal_json = excluded.journal_json,
                    updated_at = excluded.updated_at
                """,
                (
                    node_run_id,
                    workflow_id,
                    lease_resource_key,
                    fencing_token,
                    next_generation,
                    _json(dict(journal)),
                    utc_now(),
                ),
            )
            return next_generation

    def read_node_journal(self, node_run_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_node_journals WHERE node_run_id = ?",
                (str(node_run_id),),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["journal"] = json.loads(str(result.pop("journal_json")))
            return result

    def _read_snapshot_locked(
        self,
        connection: sqlite3.Connection,
        aggregate_type: AggregateType,
        aggregate_id: str,
    ) -> AggregateSnapshot | None:
        row = connection.execute(
            "SELECT * FROM minion_v2_aggregate_snapshots WHERE aggregate_type = ? AND aggregate_id = ?",
            (aggregate_type.value, aggregate_id),
        ).fetchone()
        return _snapshot_from_row(row) if row is not None else None

    def _write_snapshot_locked(
        self,
        connection: sqlite3.Connection,
        current: AggregateSnapshot | None,
        snapshot: AggregateSnapshot,
    ) -> None:
        if current is None:
            try:
                connection.execute(
                    """
                    INSERT INTO minion_v2_aggregate_snapshots(
                        aggregate_type, aggregate_id, workflow_id, state, version,
                        payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.aggregate_type.value,
                        snapshot.aggregate_id,
                        snapshot.workflow_id,
                        snapshot.state,
                        snapshot.version,
                        _json(snapshot.payload),
                        snapshot.created_at,
                        snapshot.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AggregateVersionConflict("aggregate was concurrently created") from exc
            return
        cursor = connection.execute(
            """
            UPDATE minion_v2_aggregate_snapshots
            SET state = ?, version = ?, payload_json = ?, updated_at = ?
            WHERE aggregate_type = ? AND aggregate_id = ? AND version = ?
            """,
            (
                snapshot.state,
                snapshot.version,
                _json(snapshot.payload),
                snapshot.updated_at,
                snapshot.aggregate_type.value,
                snapshot.aggregate_id,
                current.version,
            ),
        )
        if cursor.rowcount != 1:
            raise AggregateVersionConflict("aggregate snapshot compare-and-swap failed")

    def _assert_artifact_refs_durable(self, connection: sqlite3.Connection, value: Any) -> None:
        for digest in _artifact_digests(value):
            row = connection.execute(
                "SELECT durable, storage_path FROM minion_v2_artifacts WHERE sha256 = ?",
                (digest,),
            ).fetchone()
            if row is None or int(row["durable"]) != 1:
                raise ValueError(f"action references a missing or non-durable artifact: {digest}")
            if not Path(str(row["storage_path"])).is_file():
                raise ValueError(f"action references an artifact with missing storage: {digest}")

    def _consume_human_decision_locked(self, connection: sqlite3.Connection, action: ActionEnvelope) -> None:
        if action.action_type not in {"HUMAN_ACCEPT", "HUMAN_EDIT", "HUMAN_REJECT"}:
            return
        token = str(action.payload.get("decision_token") or "")
        if not token:
            raise ValueError("human decision action requires decision_token")
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        row = connection.execute(
            "SELECT * FROM minion_v2_human_decisions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown human decision token")
        if str(row["status"]) != "issued":
            raise ValueError("human decision token is stale or already consumed")
        manifest_sha = _manifest_sha_from_action(action)
        mismatches = []
        if str(row["workflow_id"]) != action.workflow_id:
            mismatches.append("workflow_id")
        if str(row["architecture_revision_id"]) != action.aggregate_id:
            mismatches.append("architecture_revision_id")
        if manifest_sha != str(row["manifest_sha"]):
            mismatches.append("manifest_sha")
        if str(row["actor_id"]) != action.actor:
            mismatches.append("actor_id")
        if mismatches:
            raise ValueError(f"stale human decision binding: {', '.join(mismatches)}")
        cursor = connection.execute(
            """
            UPDATE minion_v2_human_decisions
            SET status = 'consumed', decision = ?, action_id = ?, consumed_at = ?
            WHERE token_hash = ? AND status = 'issued'
            """,
            (action.action_type, action.action_id, action.created_at, token_hash),
        )
        if cursor.rowcount != 1:
            raise ValueError("human decision token was consumed concurrently")

    def _update_node_projection_locked(self, connection: sqlite3.Connection, snapshot: AggregateSnapshot) -> None:
        if snapshot.aggregate_type != AggregateType.DAG_NODE_RUN:
            return
        payload = dict(snapshot.payload)
        connection.execute(
            """
            INSERT INTO minion_v2_node_projection(
                node_run_id, workflow_id, epoch_id, unit_id, node_kind, state,
                dependency_node_ids_json, active_worker_id, candidate_digest, blocker_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_run_id) DO UPDATE SET
                state = excluded.state,
                dependency_node_ids_json = excluded.dependency_node_ids_json,
                active_worker_id = excluded.active_worker_id,
                candidate_digest = excluded.candidate_digest,
                blocker_json = excluded.blocker_json,
                updated_at = excluded.updated_at
            """,
            (
                snapshot.aggregate_id,
                snapshot.workflow_id,
                str(payload.get("epoch_id") or ""),
                str(payload.get("unit_id") or ""),
                str(payload.get("node_kind") or "unit"),
                snapshot.state,
                _json(list(payload.get("dependency_node_ids") or [])),
                str(payload.get("active_worker_id") or ""),
                str(payload.get("candidate_digest") or ""),
                _json(dict(payload.get("blocker") or {})),
                snapshot.updated_at,
            ),
        )

    def _update_task_projection_locked(self, connection: sqlite3.Connection, snapshot: AggregateSnapshot) -> None:
        if snapshot.aggregate_type != AggregateType.TASK:
            return
        payload = dict(snapshot.payload)
        revision_ref = dict(payload.get("task_revision_ref") or {})
        connection.execute(
            """
            INSERT INTO minion_v2_task_projection(
                task_id, state, title, objective, profile_id, family_id, workspace_key,
                task_revision_sha, owner, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                state = excluded.state,
                title = excluded.title,
                objective = excluded.objective,
                profile_id = excluded.profile_id,
                workspace_key = excluded.workspace_key,
                task_revision_sha = excluded.task_revision_sha,
                owner = excluded.owner,
                updated_at = excluded.updated_at
            """,
            (
                snapshot.aggregate_id,
                snapshot.state,
                str(payload.get("title") or ""),
                str(payload.get("objective") or ""),
                str(payload.get("primary_profile_id") or ""),
                str(payload.get("family_id") or ""),
                str(payload.get("workspace_key") or ""),
                str(revision_ref.get("sha256") or ""),
                str(payload.get("owner") or ""),
                snapshot.updated_at,
            ),
        )
        self._sync_task_fts_locked(connection, snapshot.aggregate_id)

    def _rebuild_workflow_projection_locked(
        self,
        connection: sqlite3.Connection,
        workflow_id: str,
        last_event_id: str,
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM minion_v2_aggregate_snapshots WHERE workflow_id = ? ORDER BY updated_at DESC",
            (workflow_id,),
        ).fetchall()
        snapshots = [_snapshot_from_row(row) for row in rows]
        workflow = next((item for item in snapshots if item.aggregate_type == AggregateType.WORKFLOW), None)
        if workflow is None:
            return
        active = _active_projection_snapshot(snapshots, workflow)
        phase = _current_phase(workflow, active)
        next_actions = self.engine.legal_actions(active.aggregate_type, active.state) if active is not None else ()
        waiting_for_user = bool(active is not None and active.state in _HUMAN_WAIT_STATES)
        liveness = self._liveness_locked(
            connection,
            workflow,
            snapshots,
            waiting_for_user,
            active,
        )
        blocker = dict((active.payload if active is not None else workflow.payload).get("blocker") or {})
        active_worker_id = (
            ""
            if waiting_for_user
            else str((active.payload if active is not None else {}).get("active_worker_id") or "")
        )
        if (
            not active_worker_id
            and active is not None
            and active.aggregate_type == AggregateType.EXECUTION_EPOCH
            and active.state == "REPLAN_COLLECTING"
        ):
            draining = sorted(
                (
                    item
                    for item in snapshots
                    if item.aggregate_type == AggregateType.DAG_NODE_RUN
                    and str(item.payload.get("epoch_id") or "") == active.aggregate_id
                    and item.state in {
                        "REVIEWING",
                        "REVIEW_QUIESCING",
                        "REVIEW_SNAPSHOTTING",
                    }
                    and str(item.payload.get("active_worker_id") or "")
                ),
                key=lambda item: item.aggregate_id,
            )
            if draining:
                active_worker_id = str(draining[0].payload.get("active_worker_id") or "")
        connection.execute(
            """
            INSERT INTO minion_v2_workflow_projection(
                workflow_id, current_phase, workflow_state, active_aggregate_type,
                active_aggregate_id, active_worker_id, blocker_json, next_legal_actions_json,
                waiting_for_user, liveness, metrics_json, last_progress_event_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workflow_id) DO UPDATE SET
                current_phase = excluded.current_phase,
                workflow_state = excluded.workflow_state,
                active_aggregate_type = excluded.active_aggregate_type,
                active_aggregate_id = excluded.active_aggregate_id,
                active_worker_id = excluded.active_worker_id,
                blocker_json = excluded.blocker_json,
                next_legal_actions_json = excluded.next_legal_actions_json,
                waiting_for_user = excluded.waiting_for_user,
                liveness = excluded.liveness,
                metrics_json = excluded.metrics_json,
                last_progress_event_id = excluded.last_progress_event_id,
                updated_at = excluded.updated_at
            """,
            (
                workflow_id,
                phase,
                workflow.state,
                active.aggregate_type.value if active is not None else "",
                active.aggregate_id if active is not None else "",
                active_worker_id,
                _json(blocker),
                _json(list(next_actions)),
                int(waiting_for_user),
                liveness,
                _json(self._workflow_metrics_locked(connection, workflow_id)),
                last_event_id,
                utc_now(),
            ),
        )

    def _liveness_locked(
        self,
        connection: sqlite3.Connection,
        workflow: AggregateSnapshot,
        snapshots: list[AggregateSnapshot],
        waiting_for_user: bool,
        active: AggregateSnapshot | None,
    ) -> str:
        if workflow.state in _TERMINAL_WORKFLOW_STATES:
            return "terminal"
        if workflow.state == "PAUSED":
            return "paused"
        if workflow.state == "TRIAGE_REQUIRED" or _active_lineage_has_triage(
            workflow,
            snapshots,
            active,
        ):
            return "operator_wait"
        if waiting_for_user:
            return "human_wait"
        now = utc_now()
        live_lease = connection.execute(
            """
            SELECT 1 FROM minion_v2_leases
            WHERE owner_id != '' AND expires_at > ?
              AND json_extract(metadata_json, '$.workflow_id') = ?
            LIMIT 1
            """,
            (now, workflow.workflow_id),
        ).fetchone()
        if live_lease is not None:
            return "live_lease"
        pending_effect = connection.execute(
            "SELECT 1 FROM minion_v2_outbox WHERE workflow_id = ? AND status IN ('pending', 'inflight') LIMIT 1",
            (workflow.workflow_id,),
        ).fetchone()
        if pending_effect is not None:
            return "outbox"
        durable_assignment = connection.execute(
            """
            SELECT 1 FROM minion_v2_role_assignments
            WHERE workflow_id = ?
              AND state IN ('queued', 'claimed', 'running', 'retry_queued', 'result_recorded')
            LIMIT 1
            """,
            (workflow.workflow_id,),
        ).fetchone()
        if durable_assignment is not None:
            return "role_assignment"
        return "orphaned"

    def _workflow_metrics_locked(self, connection: sqlite3.Connection, workflow_id: str) -> dict[str, Any]:
        worker = connection.execute(
            """
            SELECT
                COALESCE(SUM(total_input_tokens), 0) AS input_tokens,
                COALESCE(SUM(total_output_tokens), 0) AS output_tokens,
                COALESCE(SUM(total_cost), 0) AS cost,
                COALESCE(SUM(total_latency_ms), 0) AS llm_time_ms,
                COALESCE(SUM(total_tool_latency_ms), 0) AS tool_time_ms,
                COALESCE(SUM(total_wall_latency_ms), 0) AS worker_time_ms,
                COUNT(*) AS role_invocations
            FROM minion_v2_role_invocations
            WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        outbox = connection.execute(
            """
            SELECT COUNT(*) AS effect_count, COALESCE(SUM(attempt_count), 0) AS effect_attempts
            FROM minion_v2_outbox WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        review = connection.execute(
            """
            SELECT COALESCE(SUM(total_latency_ms), 0) AS review_time_ms
            FROM minion_v2_role_invocations
            WHERE workflow_id = ? AND role LIKE '%review%'
            """,
            (workflow_id,),
        ).fetchone()
        return {
            "queue_time_ms": self._workflow_queue_time_locked(connection, workflow_id),
            "llm_time_ms": int(worker["llm_time_ms"]),
            "tool_time_ms": int(worker["tool_time_ms"]),
            "worker_time_ms": int(worker["worker_time_ms"]),
            "review_time_ms": int(review["review_time_ms"]),
            "input_tokens": int(worker["input_tokens"]),
            "output_tokens": int(worker["output_tokens"]),
            "cost": float(worker["cost"]),
            "role_invocations": int(worker["role_invocations"]),
            "effect_count": int(outbox["effect_count"]),
            "effect_attempts": int(outbox["effect_attempts"]),
        }

    def _workflow_queue_time_locked(self, connection: sqlite3.Connection, workflow_id: str) -> int:
        rows = connection.execute(
            """
            SELECT aggregate_type, aggregate_id, payload_json, created_at
            FROM minion_v2_domain_events
            WHERE workflow_id = ?
            ORDER BY created_at, event_id
            """,
            (workflow_id,),
        ).fetchall()
        queued_since: dict[tuple[str, str], datetime] = {}
        total_seconds = 0.0
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            state = str(payload.get("target_state") or "")
            key = (str(row["aggregate_type"]), str(row["aggregate_id"]))
            created_at = _parse_datetime(str(row["created_at"]))
            if state in _QUEUED_STATES:
                queued_since.setdefault(key, created_at)
                continue
            started = queued_since.pop(key, None)
            if started is not None:
                total_seconds += max(0.0, (created_at - started).total_seconds())
        now = _utc_datetime()
        total_seconds += sum(max(0.0, (now - started).total_seconds()) for started in queued_since.values())
        return int(total_seconds * 1000)

    def _assert_lease_locked(
        self,
        connection: sqlite3.Connection,
        resource_key: str,
        owner_id: str,
        fencing_token: int,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM minion_v2_leases WHERE resource_key = ?",
            (resource_key,),
        ).fetchone()
        self._assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=_utc_datetime())

    @staticmethod
    def _assert_lease_row(
        row: sqlite3.Row | None,
        *,
        owner_id: str,
        fencing_token: int,
        now: datetime | None,
    ) -> None:
        if row is None:
            raise StaleFencingToken("lease does not exist")
        if str(row["owner_id"]) != str(owner_id) or int(row["fencing_token"]) != int(fencing_token):
            raise StaleFencingToken("worker fencing token is stale")
        if now is not None and _parse_datetime(str(row["expires_at"])) <= now:
            raise StaleFencingToken("worker lease has expired")

    def store_graph_generation(
        self,
        *,
        workflow_id: str,
        graph: GraphIR,
        status: str = "compiled",
        _connection: sqlite3.Connection | None = None,
    ) -> GraphIR:
        """Persist one immutable compiled GraphIR generation idempotently."""

        if _connection is None:
            self.ensure_schema()
        now = utc_now()
        payload = graph.to_dict()
        transaction = self._transaction() if _connection is None else nullcontext(_connection)
        with transaction as connection:
            existing = connection.execute(
                "SELECT graph_ir_json, workflow_id FROM minion_v2_graph_generations "
                "WHERE graph_id = ? AND generation = ?",
                (graph.graph_id, graph.generation),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["workflow_id"]) != workflow_id
                    or json.loads(str(existing["graph_ir_json"])) != payload
                ):
                    raise ValueError(
                        "GraphIR generation identity is already bound to other content"
                    )
                return graph_ir_from_mapping(
                    json.loads(str(existing["graph_ir_json"]))
                )
            latest = connection.execute(
                "SELECT MAX(generation) AS generation "
                "FROM minion_v2_graph_generations WHERE graph_id = ?",
                (graph.graph_id,),
            ).fetchone()
            latest_generation = int((latest or {})["generation"] or 0)
            if graph.generation != latest_generation + 1:
                raise ValueError(
                    "GraphIR generations must be appended without gaps"
                )
            connection.execute(
                """
                INSERT INTO minion_v2_graph_generations(
                    graph_id, generation, workflow_id, generation_hash,
                    graph_ir_json, source_ref, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    graph.graph_id,
                    graph.generation,
                    workflow_id,
                    graph.generation_hash,
                    _json(payload),
                    graph.source_ref,
                    str(status),
                    now,
                ),
            )
        return graph

    def read_graph_generation(
        self,
        *,
        graph_id: str,
        generation: int | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> GraphIR | None:
        if _connection is None:
            self.ensure_schema()
        connection_scope = self._connect() if _connection is None else nullcontext(_connection)
        with connection_scope as connection:
            if generation is None:
                row = connection.execute(
                    "SELECT graph_ir_json FROM minion_v2_graph_generations "
                    "WHERE graph_id = ? ORDER BY generation DESC LIMIT 1",
                    (graph_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT graph_ir_json FROM minion_v2_graph_generations "
                    "WHERE graph_id = ? AND generation = ?",
                    (graph_id, int(generation)),
                ).fetchone()
        if row is None:
            return None
        return graph_ir_from_mapping(json.loads(str(row["graph_ir_json"])))

    def store_plan_cycle(
        self,
        *,
        workflow_id: str,
        cycle: PlanCycle,
        _connection: sqlite3.Connection | None = None,
    ) -> None:
        if _connection is None:
            self.ensure_schema()
        now = utc_now()
        payload = _cycle_payload(cycle)
        transaction = self._transaction() if _connection is None else nullcontext(_connection)
        with transaction as connection:
            connection.execute(
                """
                INSERT INTO minion_v2_plan_cycles(
                    cycle_id, workflow_id, generation, state, payload_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id) DO UPDATE SET
                    generation = excluded.generation,
                    state = excluded.state,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    cycle.cycle_id,
                    workflow_id,
                    cycle.generation,
                    cycle.state.value,
                    _json(payload),
                    now,
                    now,
                ),
            )

    def read_plan_cycle(
        self,
        *,
        workflow_id: str,
        _connection: sqlite3.Connection | None = None,
    ) -> PlanCycle | None:
        if _connection is None:
            self.ensure_schema()
        connection_scope = self._connect() if _connection is None else nullcontext(_connection)
        with connection_scope as connection:
            row = connection.execute(
                "SELECT payload_json FROM minion_v2_plan_cycles "
                "WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        if row is None:
            return None
        return plan_cycle_from_mapping(
            json.loads(str(row["payload_json"]))
        )

    def store_node_cycle(
        self,
        *,
        workflow_id: str,
        graph: GraphIR,
        cycle: NodeCycle,
    ) -> None:
        if cycle.node_name not in graph.nodes:
            raise ValueError("node cycle does not belong to GraphIR")
        self.ensure_schema()
        now = utc_now()
        payload = _cycle_payload(cycle)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO minion_v2_node_cycles(
                    cycle_id, workflow_id, graph_id, graph_generation,
                    node_name, state, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id) DO UPDATE SET
                    graph_generation = excluded.graph_generation,
                    state = excluded.state,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    cycle.cycle_id,
                    workflow_id,
                    graph.graph_id,
                    graph.generation,
                    cycle.node_name,
                    cycle.state.value,
                    _json(payload),
                    now,
                    now,
                ),
            )

    def read_node_cycles(
        self,
        *,
        workflow_id: str,
        graph_generation: int | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> dict[str, NodeCycle]:
        if _connection is None:
            self.ensure_schema()
        connection_scope = self._connect() if _connection is None else nullcontext(_connection)
        with connection_scope as connection:
            if graph_generation is None:
                graph = connection.execute(
                    "SELECT MAX(graph_generation) AS generation "
                    "FROM minion_v2_node_cycles WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                graph_generation = int((graph or {})["generation"] or 0)
            rows = connection.execute(
                "SELECT payload_json FROM minion_v2_node_cycles "
                "WHERE workflow_id = ? AND graph_generation = ? "
                "ORDER BY node_name",
                (workflow_id, int(graph_generation)),
            ).fetchall()
        cycles = [
            node_cycle_from_mapping(json.loads(str(row["payload_json"])))
            for row in rows
        ]
        return {cycle.node_name: cycle for cycle in cycles}

    def store_graph_execution(
        self,
        *,
        workflow_id: str,
        execution: GraphExecution,
        _connection: sqlite3.Connection | None = None,
    ) -> None:
        """Atomically replace every cycle projection for one graph generation."""

        if _connection is None:
            self.ensure_schema()
        now = utc_now()
        transaction = self._transaction() if _connection is None else nullcontext(_connection)
        with transaction as connection:
            graph_row = connection.execute(
                "SELECT workflow_id, generation_hash "
                "FROM minion_v2_graph_generations "
                "WHERE graph_id = ? AND generation = ?",
                (execution.graph.graph_id, execution.graph.generation),
            ).fetchone()
            if graph_row is None:
                raise ValueError("GraphExecution requires a stored GraphIR generation")
            if (
                str(graph_row["workflow_id"]) != workflow_id
                or str(graph_row["generation_hash"])
                != execution.graph.generation_hash
            ):
                raise ValueError("GraphExecution is bound to another workflow generation")
            for cycle in execution.cycles.values():
                payload = _cycle_payload(cycle)
                connection.execute(
                    """
                    INSERT INTO minion_v2_node_cycles(
                        cycle_id, workflow_id, graph_id, graph_generation,
                        node_name, state, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cycle_id) DO UPDATE SET
                        graph_generation = excluded.graph_generation,
                        state = excluded.state,
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        cycle.cycle_id,
                        workflow_id,
                        execution.graph.graph_id,
                        execution.graph.generation,
                        cycle.node_name,
                        cycle.state.value,
                        _json(payload),
                        now,
                        now,
                    ),
                )
            connection.execute(
                "UPDATE minion_v2_graph_generations "
                "SET status = ?, execution_json = ? "
                "WHERE graph_id = ? AND generation = ?",
                (
                    execution.state.value.lower(),
                    _json(
                        {
                            "state": execution.state.value,
                            "published_sink_ref": execution.published_sink_ref,
                            "repair_barriers": {
                                name: list(providers)
                                for name, providers in execution.repair_barriers.items()
                            },
                        }
                    ),
                    execution.graph.graph_id,
                    execution.graph.generation,
                ),
            )

    def read_graph_execution(
        self,
        *,
        workflow_id: str,
        generation: int | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> GraphExecution | None:
        if _connection is None:
            self.ensure_schema()
        connection_scope = self._connect() if _connection is None else nullcontext(_connection)
        with connection_scope as connection:
            if generation is None:
                row = connection.execute(
                    "SELECT graph_ir_json, status, execution_json "
                    "FROM minion_v2_graph_generations "
                    "WHERE workflow_id = ? ORDER BY generation DESC LIMIT 1",
                    (workflow_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT graph_ir_json, status, execution_json "
                    "FROM minion_v2_graph_generations "
                    "WHERE workflow_id = ? AND generation = ?",
                    (workflow_id, int(generation)),
                ).fetchone()
        if row is None:
            return None
        graph = graph_ir_from_mapping(json.loads(str(row["graph_ir_json"])))
        cycles = self.read_node_cycles(
            workflow_id=workflow_id,
            graph_generation=graph.generation,
            _connection=_connection,
        )
        if not cycles:
            return None
        from pal.minion.v2.graph_executor import GraphExecutionState

        runtime = json.loads(str(row["execution_json"] or "{}"))
        raw_status = str(
            runtime.get("state") or row["status"] or ""
        ).upper()
        state = (
            GraphExecutionState.COMPLETED
            if cycles[graph.sink].state.value == "ACCEPTED"
            and all(cycle.state.value == "ACCEPTED" for cycle in cycles.values())
            else GraphExecutionState(raw_status)
            if raw_status in GraphExecutionState._value2member_map_
            else GraphExecutionState.RUNNING
        )
        return GraphExecution(
            graph=graph,
            state=state,
            cycles=cycles,
            published_sink_ref=(
                str(runtime.get("published_sink_ref") or "")
                or (
                    cycles[graph.sink].accepted_product_ref
                    if state == GraphExecutionState.COMPLETED
                    else ""
                )
            ),
            repair_barriers={
                str(name): tuple(str(item) for item in list(providers or []))
                for name, providers in dict(
                    runtime.get("repair_barriers") or {}
                ).items()
            },
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


def _snapshot_from_row(row: sqlite3.Row) -> AggregateSnapshot:
    return AggregateSnapshot(
        aggregate_type=AggregateType(str(row["aggregate_type"])),
        aggregate_id=str(row["aggregate_id"]),
        workflow_id=str(row["workflow_id"]),
        state=str(row["state"]),
        version=int(row["version"]),
        payload=json.loads(str(row["payload_json"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _action_request_payload(action: ActionEnvelope) -> dict[str, Any]:
    return {
        "action_type": action.action_type,
        "workflow_id": action.workflow_id,
        "aggregate_type": action.aggregate_type.value,
        "aggregate_id": action.aggregate_id,
        "actor": action.actor,
        "expected_version": action.expected_version,
        "payload": dict(action.payload),
    }


def _decode_role_session(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _decode_role_assignment(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for key, output, default in (
        ("required_inputs_json", "required_inputs", "[]"),
        ("input_refs_json", "input_refs", "{}"),
        ("execution_spec_json", "execution_spec", "{}"),
        ("submission_artifact_ref_json", "submission_artifact_ref", "{}"),
        ("settlement_action_json", "settlement_action", "{}"),
    ):
        value[output] = json.loads(str(value.pop(key, default) or default))
    return value


def _decode_role_attempt(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for key, output in (
        ("prompt_pack_ref_json", "prompt_pack_ref"),
        ("response_artifact_ref_json", "response_artifact_ref"),
        ("harness_state_json", "harness_state"),
    ):
        value[output] = json.loads(str(value.pop(key, "{}") or "{}"))
    return value


def _encode_dispatch_result(result: DispatchResult) -> dict[str, Any]:
    return {
        "snapshot": {
            **asdict(result.snapshot),
            "aggregate_type": result.snapshot.aggregate_type.value,
            "payload": dict(result.snapshot.payload),
        },
        "events": [
            {
                **asdict(event),
                "aggregate_type": event.aggregate_type.value,
                "payload": dict(event.payload),
            }
            for event in result.events
        ],
        "outbox_effect_ids": list(result.outbox_effect_ids),
    }


def _decode_dispatch_result(value: Mapping[str, Any], *, duplicate: bool) -> DispatchResult:
    snapshot_data = dict(value.get("snapshot") or {})
    snapshot = AggregateSnapshot(
        aggregate_type=AggregateType(str(snapshot_data["aggregate_type"])),
        aggregate_id=str(snapshot_data["aggregate_id"]),
        workflow_id=str(snapshot_data["workflow_id"]),
        state=str(snapshot_data["state"]),
        version=int(snapshot_data["version"]),
        payload=dict(snapshot_data.get("payload") or {}),
        created_at=str(snapshot_data["created_at"]),
        updated_at=str(snapshot_data["updated_at"]),
    )
    events = tuple(
        DomainEvent(
            event_id=str(item["event_id"]),
            workflow_id=str(item["workflow_id"]),
            aggregate_type=AggregateType(str(item["aggregate_type"])),
            aggregate_id=str(item["aggregate_id"]),
            aggregate_version=int(item["aggregate_version"]),
            event_type=str(item["event_type"]),
            payload=dict(item.get("payload") or {}),
            action_id=str(item["action_id"]),
            correlation_id=str(item["correlation_id"]),
            causation_id=str(item["causation_id"]),
            created_at=str(item["created_at"]),
        )
        for item in list(value.get("events") or [])
    )
    return DispatchResult(
        snapshot=snapshot,
        events=events,
        outbox_effect_ids=tuple(str(item) for item in list(value.get("outbox_effect_ids") or [])),
        duplicate=duplicate,
    )


def _artifact_digests(value: Any) -> set[str]:
    digests: set[str] = set()
    if isinstance(value, Mapping):
        raw_digest = value.get("sha256")
        if isinstance(raw_digest, str) and len(raw_digest.removeprefix("sha256:")) == 64:
            digests.add(raw_digest.removeprefix("sha256:"))
        for item in value.values():
            digests.update(_artifact_digests(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            digests.update(_artifact_digests(item))
    elif isinstance(value, str) and value.startswith("sha256:") and len(value) == 71:
        digests.add(value.removeprefix("sha256:"))
    return digests


def _manifest_sha_from_action(action: ActionEnvelope) -> str:
    value = action.payload.get("architecture_manifest_ref") or action.payload.get("clarification_ref")
    if isinstance(value, Mapping):
        return str(value.get("sha256") or "").removeprefix("sha256:")
    return str(value or action.payload.get("manifest_sha") or "").removeprefix("sha256:")


def _active_projection_snapshot(
    snapshots: list[AggregateSnapshot],
    workflow: AggregateSnapshot,
) -> AggregateSnapshot | None:
    if workflow.state in _TERMINAL_WORKFLOW_STATES | {"PAUSED", "PAUSE_REQUESTED", "CANCEL_REQUESTED", "TRIAGE_REQUIRED"}:
        return workflow
    execution_id = str(workflow.payload.get("execution_epoch_id") or "")
    architecture_id = str(workflow.payload.get("architecture_revision_id") or "")
    if execution_id:
        match = next((item for item in snapshots if item.aggregate_id == execution_id), None)
        if match is not None:
            if match.state == "REPLAN_REQUIRED":
                epoch_revision_id = str(
                    match.payload.get("active_replan_revision_id") or ""
                )
                workflow_revision_id = str(
                    workflow.payload.get("architecture_revision_id") or ""
                )
                for revision_id in dict.fromkeys(
                    (workflow_revision_id, epoch_revision_id)
                ):
                    revision = next(
                        (
                            item
                            for item in snapshots
                            if item.aggregate_type
                            == AggregateType.ARCHITECTURE_REVISION
                            and item.aggregate_id == revision_id
                            and (
                                str(
                                    item.payload.get("source_execution_epoch_id")
                                    or ""
                                )
                                == match.aggregate_id
                                or item.aggregate_id == epoch_revision_id
                                or str(
                                    item.payload.get("architecture_cycle_id") or ""
                                )
                                == epoch_revision_id
                            )
                        ),
                        None,
                    )
                    if revision is not None:
                        return revision
            return match
    if architecture_id:
        match = next((item for item in snapshots if item.aggregate_id == architecture_id), None)
        if match is not None:
            return match
    children = [item for item in snapshots if item.aggregate_type != AggregateType.WORKFLOW]
    return children[0] if children else workflow


def _current_phase(workflow: AggregateSnapshot, active: AggregateSnapshot | None) -> str:
    if workflow.state in _TERMINAL_WORKFLOW_STATES | {"PAUSED", "PAUSE_REQUESTED", "CANCEL_REQUESTED", "TRIAGE_REQUIRED"}:
        return workflow.state.lower()
    if active is None or active.aggregate_type == AggregateType.WORKFLOW:
        return "created" if workflow.state == "CREATED" else "routing"
    if active.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
        state = active.state
        if state.startswith("ARCHITECT"):
            return "architecture"
        if state in {"REVIEW_QUEUED", "REVIEWING"}:
            return "architecture_review"
        if state == "HUMAN_REVIEW":
            return "human_review"
        return f"architecture_{state.lower()}"
    if active.aggregate_type == AggregateType.EXECUTION_EPOCH:
        if active.state == "REPLAN_COLLECTING":
            return "replan_collecting"
        if active.state == "REPLAN_REQUIRED":
            return "replan_required"
        return "finalizing" if active.state == "FINALIZING" else "executing"
    if active.aggregate_type == AggregateType.DAG_NODE_RUN:
        return "executing"
    return "standalone_review"


def _active_lineage_has_triage(
    workflow: AggregateSnapshot,
    snapshots: list[AggregateSnapshot],
    active: AggregateSnapshot | None,
) -> bool:
    if active is not None and active.state == "TRIAGE_REQUIRED":
        return True
    execution_id = str(workflow.payload.get("execution_epoch_id") or "")
    execution = next(
        (
            item
            for item in snapshots
            if item.aggregate_type == AggregateType.EXECUTION_EPOCH
            and item.aggregate_id == execution_id
        ),
        None,
    )
    if execution is None:
        return False
    if execution.state != "REPLAN_REQUIRED":
        return any(
            item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == execution.aggregate_id
            and item.state == "TRIAGE_REQUIRED"
            for item in snapshots
        )
    revision_id = str(execution.payload.get("active_replan_revision_id") or "")
    return any(
        item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
        and item.aggregate_id == revision_id
        and item.state == "TRIAGE_REQUIRED"
        for item in snapshots
    )


def _decode_json_columns(row: sqlite3.Row, columns: Mapping[str, str]) -> dict[str, Any]:
    result = dict(row)
    for source, target in columns.items():
        result[target] = json.loads(str(result.pop(source)))
    result["waiting_for_user"] = bool(result.get("waiting_for_user"))
    return result


def _normalize_delivery_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    binding = dict(value or {})
    channel_id = str(binding.get("channel_id") or "").strip()
    channel_kind = str(binding.get("channel_kind") or "").strip()
    reply_target = binding.get("reply_target")
    if not channel_id or not channel_kind or not isinstance(reply_target, Mapping):
        raise ValueError(
            "delivery binding requires channel_id, channel_kind, and reply_target"
        )
    normalized_target = {
        str(key): item
        for key, item in dict(reply_target).items()
        if str(key).strip()
    }
    if not normalized_target:
        raise ValueError("delivery binding requires a non-empty reply_target")
    return {
        "channel_id": channel_id,
        "channel_kind": channel_kind,
        "reply_target": normalized_target,
        "control_scope_key": str(
            binding.get("control_scope_key")
            or normalized_target.get("control_scope_key")
            or f"{channel_kind}:{channel_id}"
        ),
    }


def _delivery_binding_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "task_id": str(row["task_id"]),
        "origin": json.loads(str(row["origin_binding_json"])),
        "current": json.loads(str(row["current_binding_json"])),
        "binding_version": int(row["binding_version"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _delivery_outbox_row(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "delivery_id": str(row["delivery_id"]),
        "dedup_key": str(row["dedup_key"]),
        "task_id": str(row["task_id"]),
        "workflow_id": str(row["workflow_id"]),
        "event_kind": str(row["event_kind"]),
        "payload": json.loads(str(row["payload_json"])),
        "status": str(row["status"]),
        "attempt_count": int(row["attempt_count"]),
        "next_attempt_at": str(row["next_attempt_at"]),
        "last_error": str(row["last_error"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
    if "current_binding_json" in row.keys():
        result["binding"] = json.loads(str(row["current_binding_json"]))
        result["binding_version"] = int(row["binding_version"])
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cycle_payload(cycle: PlanCycle | NodeCycle) -> dict[str, Any]:
    assignment = cycle.active_assignment
    verdict = cycle.last_verdict
    return {
        "cycle_id": cycle.cycle_id,
        "kind": cycle.kind.value,
        "generation": cycle.generation,
        "state": cycle.state.value,
        **(
            {"node_name": cycle.node_name}
            if isinstance(cycle, NodeCycle)
            else {}
        ),
        "active_assignment": (
            {
                "slot": assignment.slot.value,
                "kind": assignment.kind.value,
                "generation": assignment.generation,
                "input_fingerprint": assignment.input_fingerprint,
            }
            if assignment is not None
            else None
        ),
        "product_ref": cycle.product_ref,
        "accepted_product_ref": cycle.accepted_product_ref,
        "last_verdict": (
            {
                "accepted": verdict.accepted,
                "generation": verdict.generation,
                "finding_refs": list(verdict.finding_refs),
            }
            if verdict is not None
            else None
        ),
        "resume_state": (
            cycle.resume_state.value
            if cycle.resume_state is not None
            else None
        ),
    }


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _utc_datetime() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
