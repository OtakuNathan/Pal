from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _snapshot_from_row
from pal.bunshin.v2.storage.serialization import _decode_json_columns
import json
from dataclasses import dataclass
from typing import Any, Mapping
from pal.foundation import utc_now
from pal.bunshin.v2.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.v2.role_protocol import RoleAssignmentState
from pal.bunshin.v2.storage.connection_contracts import DatabasePort
from pal.bunshin.v2.storage.projections import ProjectionsStore


@dataclass
class QueriesStore:
    database: DatabasePort
    projections: ProjectionsStore

    def list_workflow_snapshots(self, workflow_id: str) -> tuple[AggregateSnapshot, ...]:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bunshin_v2_aggregate_snapshots
                WHERE workflow_id = ?
                ORDER BY created_at, aggregate_type, aggregate_id
                """,
                (str(workflow_id),),
            ).fetchall()
            return tuple(_snapshot_from_row(row) for row in rows)

    def workflow_ids(self) -> tuple[str, ...]:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                "SELECT aggregate_id FROM bunshin_v2_aggregate_snapshots WHERE aggregate_type = ? ORDER BY created_at, aggregate_id",
                (AggregateType.WORKFLOW.value,),
            ).fetchall()
            return tuple(str(row["aggregate_id"]) for row in rows)

    def read_workflow_projection(self, workflow_id: str) -> dict[str, Any] | None:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            existing = connection.execute(
                "SELECT last_progress_event_id FROM bunshin_v2_workflow_projection WHERE workflow_id = ?",
                (str(workflow_id),),
            ).fetchone()
            self.projections.rebuild_workflow_projection_locked(
                connection,
                str(workflow_id),
                str(existing["last_progress_event_id"] or "") if existing is not None else "",
            )
            row = connection.execute(
                "SELECT * FROM bunshin_v2_workflow_projection WHERE workflow_id = ?",
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
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bunshin_v2_node_projection
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
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT invocation_id, workflow_id, aggregate_type, aggregate_id,
                       role, mode, status, last_completed_turn,
                       total_input_tokens, total_output_tokens,
                       total_latency_ms, total_tool_latency_ms,
                       total_wall_latency_ms, created_at, updated_at
                FROM bunshin_v2_role_invocations
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
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT assignment.role, assignment.mode,
                       assignment.state AS assignment_state,
                       attempt.status AS attempt_state,
                       draft.version AS checklist_version,
                       draft.status AS checklist_status,
                       draft.payload_json AS checklist_payload_json,
                       draft.updated_at AS checklist_updated_at
                FROM bunshin_v2_role_assignments AS assignment
                LEFT JOIN bunshin_v2_role_attempts AS attempt
                  ON attempt.attempt_id = assignment.active_attempt_id
                LEFT JOIN bunshin_v2_submission_drafts AS draft
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

    def read_latest_workflow_event(self, workflow_id: str) -> dict[str, Any] | None:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT event_type, aggregate_type, created_at
                FROM bunshin_v2_domain_events
                WHERE workflow_id = ?
                ORDER BY created_at DESC, event_id DESC
                LIMIT 1
                """,
                (str(workflow_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def read_domain_event_aggregate_version(self, event_id: str) -> int | None:
        """Return the snapshot version that produced a durable outbox effect."""
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT aggregate_version FROM bunshin_v2_domain_events WHERE event_id = ?",
                (str(event_id or "").strip(),),
            ).fetchone()
        return int(row["aggregate_version"]) if row is not None else None

    def read_domain_event_effect_context(self, event_id: str) -> dict[str, Any]:
        """Recover causal state for outbox rows created before context embedding."""

        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT aggregate_version, payload_json "
                "FROM bunshin_v2_domain_events WHERE event_id = ?",
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

    def read_effect_pending_verification_ref(self, event_id: str) -> dict[str, Any]:
        """Bind legacy snapshot effects to their causal submission, never latest."""

        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT json_extract(submit.payload_json, '$.action_payload.pending_verification_ref') AS pending_ref
                FROM bunshin_v2_domain_events AS cause
                JOIN bunshin_v2_domain_events AS submit
                  ON submit.aggregate_type = cause.aggregate_type
                 AND submit.aggregate_id = cause.aggregate_id
                 AND submit.aggregate_version <= cause.aggregate_version
                WHERE cause.event_id = ?
                  AND json_extract(submit.payload_json, '$.action_payload.pending_verification_ref.sha256') IS NOT NULL
                ORDER BY submit.aggregate_version DESC LIMIT 1
                """, (str(event_id),),
            ).fetchone()
        return dict(json.loads(str(row["pending_ref"]))) if row is not None else {}

    def read_verification_settlement_ref(self, aggregate_id: str, pending_sha256: str) -> dict[str, Any]:
        """Return only a committed verdict receipt for this exact pending input."""

        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT json_extract(payload_json, '$.action_payload.verification_artifact_ref') AS report_ref
                FROM bunshin_v2_domain_events
                WHERE aggregate_type = ? AND aggregate_id = ?
                  AND json_extract(payload_json, '$.action_payload.source_pending_verification_ref.sha256') = ?
                  AND json_extract(payload_json, '$.action_payload.verification_artifact_ref.sha256') IS NOT NULL
                ORDER BY aggregate_version DESC LIMIT 1
                """, (AggregateType.DAG_NODE_RUN.value, str(aggregate_id), str(pending_sha256)),
            ).fetchone()
        return dict(json.loads(str(row["report_ref"]))) if row is not None else {}

    def has_dependency_repair_receipt(self, aggregate_id: str, repair_sha256: str, action_type: str) -> bool:
        """A later repair must not erase an earlier propagation's replay receipt."""

        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            return connection.execute(
                """
                SELECT 1 FROM bunshin_v2_domain_events
                WHERE aggregate_type = ? AND aggregate_id = ? AND event_type = ?
                  AND json_extract(payload_json, '$.action_payload.source_repair_packet_ref.sha256') = ?
                LIMIT 1
                """, (AggregateType.DAG_NODE_RUN.value, str(aggregate_id),
                      f"dag_node_run.{action_type.lower()}", str(repair_sha256)),
            ).fetchone() is not None

    def has_nonterminal_workflows_for_task(self, task_id: str) -> bool:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM bunshin_v2_aggregate_snapshots
                WHERE aggregate_type = ?
                  AND json_extract(payload_json, '$.task_id') = ?
                  AND state NOT IN ('COMPLETED', 'REJECTED', 'CANCELLED')
                LIMIT 1
                """,
                (AggregateType.WORKFLOW.value, str(task_id)),
            ).fetchone()
            return row is not None

    def orphaned_workflow_ids(self) -> tuple[str, ...]:
        self.projections.rebuild_workflow_projections()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                "SELECT workflow_id FROM bunshin_v2_workflow_projection WHERE liveness = 'orphaned' ORDER BY workflow_id"
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

        self.database.ensure_schema()
        sources: list[str] = []
        now = utc_now()
        with self.database.read_connection() as connection:
            if lease_resource_key:
                lease = connection.execute(
                    """
                    SELECT 1 FROM bunshin_v2_leases
                    WHERE resource_key = ? AND owner_id != '' AND expires_at > ?
                    LIMIT 1
                    """,
                    (str(lease_resource_key), now),
                ).fetchone()
                if lease is not None:
                    sources.append("live_lease")
            pending_effect = connection.execute(
                """
                SELECT 1 FROM bunshin_v2_outbox
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
                SELECT 1 FROM bunshin_v2_role_assignments
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
