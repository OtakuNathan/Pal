from __future__ import annotations
from pal.bunshin.event_contracts import same_scheduled_terminal_intent, terminal_has_user_actions, terminal_is_control_only
from pal.bunshin.storage.serialization import _delivery_outbox_row
from pal.bunshin.storage.serialization import _json
from pal.bunshin.storage.serialization import _utc_datetime
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping
from uuid import uuid4
from pal.foundation import utc_now
from pal.bunshin.storage.connection_contracts import DatabasePort


_ACTIONABLE_DELIVERY = """
    NOT EXISTS (
        SELECT 1 FROM bunshin_interaction_waits AS w
        JOIN bunshin_role_assignments AS a ON a.assignment_id = w.assignment_id
        JOIN bunshin_aggregate_snapshots AS workflow
          ON workflow.aggregate_type = 'workflow' AND workflow.aggregate_id = a.workflow_id
        WHERE w.wait_id = CASE d.event_kind
            WHEN 'approval_requested' THEN json_extract(d.payload_json, '$.payload.approval_id')
            WHEN 'clarification_requested' THEN json_extract(d.payload_json, '$.payload.clarification_id')
        END AND (w.state <> 'open' OR workflow.state IN (
            'CANCEL_REQUESTED', 'CANCELLED', 'RESTARTING', 'COMPLETED', 'REJECTED'
        ))
    )
"""


@dataclass
class DeliveriesStore:
    database: DatabasePort

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

        self.database.ensure_schema()
        normalized_task_id = str(task_id or "").strip()
        normalized_kind = str(event_kind or "").strip()
        normalized_workflow_id = str(workflow_id or "").strip()
        normalized_key = str(dedup_key or "").strip()
        if not normalized_task_id or not normalized_kind or not normalized_key:
            raise ValueError("task delivery requires task_id, event_kind, and dedup_key")
        now = utc_now()
        delivery_id = f"delivery_{uuid4().hex}"
        with self.database.write_connection() as connection:
            binding = connection.execute(
                "SELECT 1 FROM bunshin_task_delivery_bindings WHERE task_id = ?",
                (normalized_task_id,),
            ).fetchone()
            if binding is None:
                raise ValueError("task delivery requires an existing delivery binding")
            connection.execute(
                """
                INSERT INTO bunshin_delivery_outbox(
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
                "SELECT * FROM bunshin_delivery_outbox WHERE dedup_key = ?",
                (normalized_key,),
            ).fetchone()
            if row is not None:
                stored_payload = json.loads(str(row["payload_json"]))
                same_request = (
                    str(row["task_id"]) == normalized_task_id
                    and str(row["workflow_id"]) == normalized_workflow_id
                    and str(row["event_kind"]) == normalized_kind
                    and (
                        stored_payload == dict(payload or {})
                        or (normalized_kind == "workflow_terminal" and same_scheduled_terminal_intent(stored_payload, payload))
                    )
                )
                if not same_request:
                    raise ValueError(
                        f"delivery dedup key collision with different request: {normalized_key}"
                    )
        return _delivery_outbox_row(row)

    def pending_interaction_deliveries(self, task_id: str) -> tuple[dict[str, Any], ...]:
        """Find the original notification for each still-actionable tool call."""
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                f"""SELECT d.* FROM bunshin_delivery_outbox d
                   JOIN bunshin_interaction_waits w ON d.dedup_key = 'role-interaction:' || w.wait_id
                   JOIN bunshin_role_assignments a ON w.assignment_id = a.assignment_id
                   WHERE d.task_id = ? AND w.state = 'open'
                     AND a.state IN ('queued','claimed','running','retry_queued')
                     AND {_ACTIONABLE_DELIVERY}
                   ORDER BY d.created_at, d.delivery_id""", (task_id,),
            ).fetchall()
        return tuple(_delivery_outbox_row(row) for row in rows)

    def list_pending_task_deliveries(self, *, limit: int = 50) -> tuple[dict[str, Any], ...]:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT d.*, b.current_binding_json, b.binding_version
                FROM bunshin_delivery_outbox AS d
                JOIN bunshin_task_delivery_bindings AS b ON b.task_id = d.task_id
                WHERE d.status = 'pending' AND d.next_attempt_at <= ?
                  AND {_ACTIONABLE_DELIVERY}
                ORDER BY d.created_at, d.delivery_id
                LIMIT ?
                """,
                (utc_now(), max(1, min(int(limit), 500))),
            ).fetchall()
        return tuple(_delivery_outbox_row(row) for row in rows)

    def may_send_interaction_delivery(self, event: Mapping[str, Any]) -> bool:
        delivery_id = str(event.get("delivery_id") or "")
        if not delivery_id or event.get("event_kind") not in {"approval_requested", "clarification_requested"}:
            return True
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            return connection.execute(
                f"SELECT 1 FROM bunshin_delivery_outbox AS d WHERE d.delivery_id = ? "
                f"AND d.status = 'pending' AND {_ACTIONABLE_DELIVERY}",
                (delivery_id,),
            ).fetchone() is not None

    def acknowledge_task_delivery(self, delivery_id: str) -> bool:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE bunshin_delivery_outbox
                SET status = 'delivered', updated_at = ?
                WHERE delivery_id = ? AND status = 'pending'
                """,
                (utc_now(), str(delivery_id or "").strip()),
            )
        return cursor.rowcount == 1

    def suppress_task_delivery(self, delivery_id: str) -> bool:
        """Retire a legacy control-free terminal notice without claiming a send."""
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            row = connection.execute(
                "SELECT event_kind,payload_json,status FROM bunshin_delivery_outbox WHERE delivery_id=?",
                (str(delivery_id),),
            ).fetchone()
            if row is None:
                return False
            payload = dict(json.loads(str(row["payload_json"])).get("payload") or {})
            if not terminal_is_control_only(row["event_kind"], payload) or terminal_has_user_actions(payload):
                raise ValueError("delivery is not a suppressible terminal notice")
            if row["status"] == "suppressed":
                return True
            return connection.execute(
                "UPDATE bunshin_delivery_outbox SET status='suppressed', updated_at=? WHERE delivery_id=? AND status='pending'",
                (utc_now(), str(delivery_id)),
            ).rowcount == 1

    def delivered_task_delivery_parts(self, delivery_id: str) -> tuple[str, ...]:
        """Return durable sub-deliveries already accepted by the channel."""

        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT part_key FROM bunshin_delivery_parts
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

        self.database.ensure_schema()
        normalized_delivery_id = str(delivery_id or "").strip()
        normalized_part_key = str(part_key or "").strip()
        if not normalized_delivery_id or not normalized_part_key:
            raise ValueError("delivery_id and part_key are required")
        with self.database.write_connection() as connection:
            pending = connection.execute(
                """
                SELECT 1 FROM bunshin_delivery_outbox
                WHERE delivery_id = ? AND status = 'pending'
                """,
                (normalized_delivery_id,),
            ).fetchone()
            if pending is None:
                return False
            connection.execute(
                """
                INSERT INTO bunshin_delivery_parts(
                    delivery_id, part_key, delivered_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(delivery_id, part_key) DO NOTHING
                """,
                (normalized_delivery_id, normalized_part_key, utc_now()),
            )
        return True

    def mark_task_delivery_uncertain(self, delivery_id: str) -> bool:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            cursor = connection.execute(
                "UPDATE bunshin_delivery_outbox SET status='uncertain', last_error=?, updated_at=? WHERE delivery_id=? AND status='pending'",
                ("external channel acceptance is unknown", utc_now(), delivery_id),
            )
        return cursor.rowcount == 1

    def defer_task_delivery(self, delivery_id: str, *, error: str = "") -> bool:
        self.database.ensure_schema()
        now = _utc_datetime()
        retry_at = (now + timedelta(seconds=1)).isoformat()
        with self.database.write_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE bunshin_delivery_outbox
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
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM bunshin_delivery_outbox
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
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_delivery_outbox WHERE delivery_id = ?",
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
