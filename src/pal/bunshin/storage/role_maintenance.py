from __future__ import annotations
import json
from dataclasses import dataclass
from pal.bunshin.checkpoint import (
    AgentSessionCheckpointError, LogicalCoroutineCheckpointStore,
    normalize_agent_session_checkpoint, open_agent_session_checkpoint,
)
from pal.bunshin.contracts import AggregateType
from pal.bunshin.paths import invocation_root, reconcile_role_runtime_spool
from pal.bunshin.role_protocol import RoleSessionState
from pal.bunshin.storage.serialization import _parse_datetime, _utc_datetime
from pal.bunshin.workspace_resources import inherited_process_group_live
from pal.bunshin.storage.connection_contracts import DatabasePort
from pal.bunshin.storage.role_sessions import RoleSessionsStore


@dataclass
class RoleMaintenanceStore:
    database: DatabasePort
    role_sessions: RoleSessionsStore

    def complete_workflow_role_sessions(
        self,
        workflow_id: str,
        *,
        status: str = "completed",
    ) -> tuple[str, ...]:
        """Close logical role sessions only after the workflow itself is terminal."""

        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT session_id FROM bunshin_role_sessions
                WHERE workflow_id = ? AND status IN (?, ?, ?)
                ORDER BY created_at, session_id
                """,
                (
                    str(workflow_id),
                    RoleSessionState.UNINITIALIZED.value,
                    RoleSessionState.ACTIVE.value,
                    RoleSessionState.SUSPENDED.value,
                ),
            ).fetchall()
        completed: list[str] = []
        for row in rows:
            session_id = str(row["session_id"])
            if self.role_sessions.complete_role_session(session_id, status=status):
                completed.append(session_id)
        return tuple(completed)

    def reconcile_retired_role_checkpoint(self, session_id: str) -> bool:
        """Select the latest authenticated closed point of this retired attempt.

        The attempt spool is only a candidate. An older assignment, a live
        process, or any unresolved interaction cannot replace the selected
        continuation. Selection precedes receipt replay and a replacement claim.
        """
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            row = connection.execute(
                """SELECT a.assignment_id, p.attempt_id, p.fencing_token,
                          l.owner_id, l.expires_at, l.metadata_json, s.status
                   FROM bunshin_role_attempts p
                   JOIN bunshin_role_assignments a ON a.assignment_id=p.assignment_id
                   JOIN bunshin_role_sessions s ON s.session_id=a.session_id
                   LEFT JOIN bunshin_leases l ON l.resource_key=p.lease_resource_key
                       AND l.fencing_token=p.fencing_token
                   WHERE a.session_id=? AND a.active_attempt_id=p.attempt_id
                     AND p.harness_id='pal' AND p.fencing_token>0
                     AND NOT EXISTS (
                         SELECT 1 FROM bunshin_role_attempts q JOIN bunshin_role_assignments b
                             ON b.assignment_id=q.assignment_id
                         WHERE b.session_id=a.session_id AND q.rowid>p.rowid
                     )""",
                (session_id,),
            ).fetchone()
            if row is None or row["status"] in {"completed", "cancelled"}:
                return False
            attempt_id = str(row["attempt_id"])
            if row["owner_id"] == attempt_id and _parse_datetime(row["expires_at"]) > _utc_datetime():
                return False
            metadata = json.loads(row["metadata_json"] or "{}")
            group = metadata.get("process_group_id")
            if type(group) is int and inherited_process_group_live(group, str(metadata.get("process_group_birth") or "")):
                return False
            root = self.database.runtime_root
            path = invocation_root(root) / session_id / "session-attempts" / attempt_id / "continuation-output.json"
            if not path.is_file():
                return False
            store = LogicalCoroutineCheckpointStore(root)
            current = store.read_locked(connection, session_id)
            if current is None:
                # Never use a retirement candidate to disguise the loss of
                # an already acknowledged continuation.
                return False
            try:
                candidate = normalize_agent_session_checkpoint(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, OSError) as exc:
                raise AgentSessionCheckpointError("retired attempt checkpoint is unreadable") from exc
            if int(candidate["sequence"]) <= int(current["sequence"]):
                return False
            private = open_agent_session_checkpoint(root, candidate)["coroutine_state"]
            selected = open_agent_session_checkpoint(root, current)["coroutine_state"]
            if private.get("parked_batch") or selected.get("parked_batch"):
                return False
            if connection.execute(
                """SELECT 1 FROM bunshin_interaction_waits w JOIN bunshin_role_assignments a
                    ON a.assignment_id=w.assignment_id WHERE a.session_id=?
                    AND w.state IN ('open','answered','resuming')""", (session_id,),
            ).fetchone():
                return False
            if private.get("active_response_key") not in {None, "", str(row["assignment_id"])}:
                raise AgentSessionCheckpointError("retired checkpoint has the wrong assignment")
            self.role_sessions.publish_role_session_checkpoint_locked(
                connection, session_id=session_id, fencing_token=int(row["fencing_token"]),
                checkpoint=candidate, recovering=True,
            )
            return True

    def reconcile_role_session_checkpoints(self) -> tuple[str, ...]:
        """Delete derived checkpoints whose durable role session is terminal."""

        self.database.ensure_schema()
        store = LogicalCoroutineCheckpointStore(self.database.runtime_root)
        checkpoint_ids = store.list_logical_coroutine_ids()
        if not checkpoint_ids:
            return ()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT session_id FROM bunshin_role_sessions
                WHERE status IN (?, ?, ?)
                """,
                (
                    RoleSessionState.UNINITIALIZED.value,
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

    def reconcile_terminal_role_runtime(self) -> tuple[str, ...]:
        """Close terminal-workflow sessions and sweep disposable role state."""

        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            workflows = connection.execute(
                """
                SELECT aggregate_id AS workflow_id, state AS workflow_state
                FROM bunshin_aggregate_snapshots
                WHERE aggregate_type = ?
                  AND state IN ('COMPLETED', 'REJECTED', 'CANCELLED')
                ORDER BY aggregate_id
                """,
                (AggregateType.WORKFLOW.value,),
            ).fetchall()
        for workflow in workflows:
            state = str(workflow["workflow_state"])
            self.complete_workflow_role_sessions(
                str(workflow["workflow_id"]),
                status="cancelled" if state == "CANCELLED" else "completed",
            )
        return self.reconcile_role_runtime_spool()

    def reconcile_role_runtime_spool(self) -> tuple[str, ...]:
        """Sweep role runtime trees that have no resumable session owner."""

        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT session_id FROM bunshin_role_sessions
                WHERE status IN (?, ?, ?)
                """,
                (
                    RoleSessionState.UNINITIALIZED.value,
                    RoleSessionState.ACTIVE.value,
                    RoleSessionState.SUSPENDED.value,
                ),
            ).fetchall()
        resumable = {str(row["session_id"]) for row in rows}
        return reconcile_role_runtime_spool(
            self.database.runtime_root,
            resumable_session_ids=resumable,
        )
