from __future__ import annotations
from pal.bunshin.storage.serialization import _snapshot_from_row
from pal.bunshin.storage.serialization import _active_projection_snapshot
from pal.bunshin.storage.serialization import _current_phase
from pal.bunshin.storage.serialization import _json
from pal.bunshin.storage.serialization import _HUMAN_WAIT_STATES
import sqlite3
from dataclasses import dataclass
from pal.foundation import utc_now
from pal.bunshin.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.engine import TransitionEngine
from pal.bunshin.storage.connection_contracts import DatabasePort
from pal.bunshin.storage.metrics import MetricsStore


@dataclass
class ProjectionsStore:
    database: DatabasePort
    metrics: MetricsStore
    engine: TransitionEngine

    def rebuild_workflow_projections(self) -> int:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            rows = connection.execute(
                "SELECT aggregate_id FROM bunshin_v2_aggregate_snapshots WHERE aggregate_type = ?",
                (AggregateType.WORKFLOW.value,),
            ).fetchall()
            for row in rows:
                self.rebuild_workflow_projection_locked(connection, str(row["aggregate_id"]), "")
            return len(rows)

    def update_node_projection_locked(self, connection: sqlite3.Connection, snapshot: AggregateSnapshot) -> None:
        if snapshot.aggregate_type != AggregateType.DAG_NODE_RUN:
            return
        payload = dict(snapshot.payload)
        connection.execute(
            """
            INSERT INTO bunshin_v2_node_projection(
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

    def update_task_projection_locked(self, connection: sqlite3.Connection, snapshot: AggregateSnapshot) -> None:
        if snapshot.aggregate_type != AggregateType.TASK:
            return
        payload = dict(snapshot.payload)
        revision_ref = dict(payload.get("task_revision_ref") or {})
        connection.execute(
            """
            INSERT INTO bunshin_v2_task_projection(
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
        self.database.sync_task_fts_locked(connection, snapshot.aggregate_id)

    def rebuild_workflow_projection_locked(
        self,
        connection: sqlite3.Connection,
        workflow_id: str,
        last_event_id: str,
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM bunshin_v2_aggregate_snapshots WHERE workflow_id = ? ORDER BY updated_at DESC",
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
        liveness = self.metrics.liveness_locked(
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
            INSERT INTO bunshin_v2_workflow_projection(
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
                _json(self.metrics.workflow_metrics_locked(connection, workflow_id)),
                last_event_id,
                utc_now(),
            ),
        )
