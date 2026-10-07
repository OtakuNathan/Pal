from __future__ import annotations
from pal.bunshin.storage.serialization import _normalize_delivery_binding
from pal.bunshin.storage.serialization import _delivery_binding_row
from pal.bunshin.storage.serialization import _json
import json
from dataclasses import dataclass
from typing import Any, Mapping
from pal.foundation import utc_now
from pal.bunshin.contracts import AggregateType
from pal.bunshin.storage.connection_contracts import DatabasePort


@dataclass
class DeliveryBindingsStore:
    database: DatabasePort

    def bind_task_delivery(
        self,
        *,
        task_id: str,
        binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Capture the immutable origin and initial reply target for one Task."""

        self.database.ensure_schema()
        normalized_task_id = str(task_id or "").strip()
        normalized = _normalize_delivery_binding(binding)
        if not normalized_task_id:
            raise ValueError("task delivery binding requires task_id")
        now = utc_now()
        with self.database.write_connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM bunshin_v2_task_projection WHERE task_id = ?",
                (normalized_task_id,),
            ).fetchone()
            if row is None:
                raise ValueError("task delivery binding requires an existing Task")
            existing = connection.execute(
                "SELECT * FROM bunshin_v2_task_delivery_bindings WHERE task_id = ?",
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
                INSERT INTO bunshin_v2_task_delivery_bindings(
                    task_id, origin_binding_json, current_binding_json,
                    binding_version, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                (normalized_task_id, _json(normalized), _json(normalized), now, now),
            )
        return self.read_task_delivery(normalized_task_id) or {}

    def read_task_delivery(self, task_id: str) -> dict[str, Any] | None:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_task_delivery_bindings WHERE task_id = ?",
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

        self.database.ensure_schema()
        normalized_task_id = str(task_id or "").strip()
        normalized = _normalize_delivery_binding(binding)
        with self.database.write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_task_delivery_bindings WHERE task_id = ?",
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
                UPDATE bunshin_v2_task_delivery_bindings
                SET current_binding_json = ?, binding_version = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (_json(normalized), version, now, normalized_task_id),
            )
            updated = connection.execute(
                "SELECT * FROM bunshin_v2_task_delivery_bindings WHERE task_id = ?",
                (normalized_task_id,),
            ).fetchone()
        return {**_delivery_binding_row(updated), "changed": True}

    def pending_human_review_workflows(self, task_id: str) -> tuple[str, ...]:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT h.workflow_id
                FROM bunshin_v2_human_decisions AS h
                JOIN bunshin_v2_aggregate_snapshots AS w
                  ON w.aggregate_type = ? AND w.aggregate_id = h.workflow_id
                WHERE h.status = 'issued'
                  AND json_extract(w.payload_json, '$.task_id') = ?
                ORDER BY h.workflow_id
                """,
                (AggregateType.WORKFLOW.value, str(task_id)),
            ).fetchall()
        return tuple(str(row["workflow_id"]) for row in rows)
