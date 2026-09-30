from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _active_lineage_has_triage
from pal.bunshin.v2.storage.serialization import _utc_datetime
from pal.bunshin.v2.storage.serialization import _parse_datetime
from pal.bunshin.v2.storage.serialization import _QUEUED_STATES
from pal.bunshin.v2.storage.serialization import _TERMINAL_WORKFLOW_STATES
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from pal.foundation import utc_now
from pal.bunshin.v2.contracts import AggregateSnapshot


@dataclass
class MetricsStore:


    def liveness_locked(
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
            SELECT 1 FROM bunshin_v2_leases
            WHERE owner_id != '' AND expires_at > ?
              AND json_extract(metadata_json, '$.workflow_id') = ?
            LIMIT 1
            """,
            (now, workflow.workflow_id),
        ).fetchone()
        if live_lease is not None:
            return "live_lease"
        pending_effect = connection.execute(
            "SELECT 1 FROM bunshin_v2_outbox WHERE workflow_id = ? AND status IN ('pending', 'inflight') LIMIT 1",
            (workflow.workflow_id,),
        ).fetchone()
        if pending_effect is not None:
            return "outbox"
        durable_assignment = connection.execute(
            """
            SELECT 1 FROM bunshin_v2_role_assignments
            WHERE workflow_id = ?
              AND state IN ('queued', 'claimed', 'running', 'retry_queued', 'result_recorded')
            LIMIT 1
            """,
            (workflow.workflow_id,),
        ).fetchone()
        if durable_assignment is not None:
            return "role_assignment"
        return "orphaned"

    def workflow_metrics_locked(self, connection: sqlite3.Connection, workflow_id: str) -> dict[str, Any]:
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
            FROM bunshin_v2_role_invocations
            WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        outbox = connection.execute(
            """
            SELECT COUNT(*) AS effect_count, COALESCE(SUM(attempt_count), 0) AS effect_attempts
            FROM bunshin_v2_outbox WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        review = connection.execute(
            """
            SELECT COALESCE(SUM(total_latency_ms), 0) AS review_time_ms
            FROM bunshin_v2_role_invocations
            WHERE workflow_id = ? AND role LIKE '%review%'
            """,
            (workflow_id,),
        ).fetchone()
        return {
            "queue_time_ms": self.workflow_queue_time_locked(connection, workflow_id),
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

    def workflow_queue_time_locked(self, connection: sqlite3.Connection, workflow_id: str) -> int:
        rows = connection.execute(
            """
            SELECT aggregate_type, aggregate_id, payload_json, created_at
            FROM bunshin_v2_domain_events
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
