from __future__ import annotations
from dataclasses import dataclass
from pal.bunshin.checkpoint import LogicalCoroutineCheckpointStore
from pal.bunshin.contracts import AggregateType
from pal.bunshin.paths import reconcile_role_runtime_spool
from pal.bunshin.role_protocol import RoleSessionState
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
                SELECT session_id FROM bunshin_v2_role_sessions
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
                SELECT session_id FROM bunshin_v2_role_sessions
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
                FROM bunshin_v2_aggregate_snapshots
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
                SELECT session_id FROM bunshin_v2_role_sessions
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
