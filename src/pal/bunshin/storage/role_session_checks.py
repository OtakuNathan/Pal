from __future__ import annotations
from pal.bunshin.storage.serialization import _snapshot_from_row
import sqlite3
from dataclasses import dataclass
from pal.bunshin.contracts import AggregateType
from pal.bunshin.role_protocol import RoleAssignmentRequest
from pal.bunshin.storage.snapshots import SnapshotsStore


@dataclass
class RoleSessionChecksStore:
    snapshots: SnapshotsStore

    def assert_assignment_in_role_session_scope_locked(
        self,
        connection: sqlite3.Connection,
        session: sqlite3.Row,
        request: RoleAssignmentRequest,
    ) -> None:
        scope_kind = str(session["scope_kind"] or "")
        subject_key = str(session["subject_key"] or "")
        snapshot = self.snapshots.read_snapshot_locked(
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

    def assert_role_session_scope_terminal_locked(
        self,
        connection: sqlite3.Connection,
        session: sqlite3.Row,
        *,
        cancelled: bool,
    ) -> None:
        workflow_id = str(session["workflow_id"])
        workflow = self.snapshots.read_snapshot_locked(
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
            if cancelled and self.module_absent_from_latest_epoch_locked(
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
                SELECT * FROM bunshin_v2_aggregate_snapshots
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
        snapshot = self.snapshots.read_snapshot_locked(
            connection,
            AggregateType(str(session["aggregate_type"])),
            str(session["aggregate_id"]),
        )
        allowed = {"ACCEPTED", "REJECTED", "CANCELLED"}
        if snapshot is None or snapshot.state not in allowed:
            raise ValueError(
                "aggregate-bound role session cannot complete before its aggregate is terminal"
            )

    def module_absent_from_latest_epoch_locked(
        self,
        connection: sqlite3.Connection,
        *,
        workflow_id: str,
        module_name: str,
    ) -> bool:
        epoch_row = connection.execute(
            """
            SELECT * FROM bunshin_v2_aggregate_snapshots
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
            SELECT * FROM bunshin_v2_aggregate_snapshots
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
