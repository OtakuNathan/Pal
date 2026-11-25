from __future__ import annotations
from pal.bunshin.storage.serialization import _decode_role_assignment
import sqlite3
from dataclasses import dataclass
from typing import Any
from pal.foundation import utc_now
from pal.bunshin.contracts import AggregateType
from pal.bunshin.role_protocol import RoleAssignmentAction, RoleAssignmentState, RoleAttemptState, RoleSessionAction, role_assignment_target
from pal.bunshin.storage.connection_contracts import DatabasePort
from pal.bunshin.storage.role_sessions import RoleSessionsStore
from pal.bunshin.storage.role_submissions import RoleSubmissionsStore


@dataclass
class RoleCancellationStore:
    database: DatabasePort
    role_sessions: RoleSessionsStore
    role_submissions: RoleSubmissionsStore

    def cancel_role_assignments(
        self,
        *,
        workflow_id: str,
        aggregate_type: AggregateType | str,
        aggregate_id: str,
        reason: str,
        exclude_assignment_id: str = "",
        preserve_interactions: bool = False,
        assignment_ids: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Terminate nonterminal invocations bound to one aggregate.

        Pause and triage retain parked interactions and retire attempts that
        already claimed an answer. Their outcome remains available for checkpoint
        reconciliation. A definitive cancellation closes all matching waits.
        """

        if assignment_ids is not None and not assignment_ids:
            return ()
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
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            scope = (str(workflow_id), aggregate_type_value, str(aggregate_id), str(exclude_assignment_id))
            rows = connection.execute(
                """
                SELECT * FROM bunshin_role_assignments
                WHERE workflow_id = ? AND aggregate_type = ? AND aggregate_id = ?
                  AND assignment_id != ?
                  AND state IN (?, ?, ?, ?, ?)
                ORDER BY created_at, assignment_id
                """,
                (*scope, *cancellable_states),
            ).fetchall()
            interactions = connection.execute(
                """SELECT w.wait_id, w.assignment_id, w.state FROM bunshin_interaction_waits w
                   JOIN bunshin_role_assignments a ON a.assignment_id=w.assignment_id
                   WHERE a.workflow_id=? AND a.aggregate_type=? AND a.aggregate_id=?
                     AND a.assignment_id<>? AND w.state IN ('open','answered','resuming')""",
                scope,
            ).fetchall()
            resuming = {row["assignment_id"] for row in interactions if row["state"] == "resuming"}
            if preserve_interactions:
                parked = {row["assignment_id"] for row in interactions if row["state"] in {"open", "answered"}}
                rows = [row for row in rows if row["assignment_id"] not in parked]
            exact_ids = None if assignment_ids is None else frozenset(str(item) for item in assignment_ids)
            if exact_ids is not None:
                rows = [row for row in rows if str(row["assignment_id"]) in exact_ids]
            if not preserve_interactions:
                # A previously quiesced assignment may already be cancelled
                # while its interrupted interaction still awaits reconciliation.
                connection.executemany(
                    "UPDATE bunshin_interaction_waits SET state='cancelled' WHERE wait_id=?",
                    ((wait["wait_id"],) for wait in interactions
                     if exact_ids is None or str(wait["assignment_id"]) in exact_ids),
                )
            now = utc_now()
            for assignment in rows:
                if str(assignment["state"]) == RoleAssignmentState.RESULT_RECORDED.value:
                    self.role_submissions.settle_role_assignment_locked(
                        connection,
                        settlement={
                            "role_assignment_id": str(assignment["assignment_id"]),
                            "role_submission_payload_hash": str(assignment["submission_payload_hash"]),
                        },
                    )
                    connection.execute(
                        """
                        UPDATE bunshin_role_assignments
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
                    self._cancel_attempt_locked(
                        connection, attempt_id_value, reason=reason, now=now,
                        preserve_process_record=assignment["assignment_id"] in resuming,
                    )
                connection.execute(
                    """
                    UPDATE bunshin_role_assignments
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
                self.role_sessions.transition_role_session_locked(
                    connection,
                    str(assignment["session_id"]),
                    RoleSessionAction.PARK,
                    now=now,
                )
            if not rows:
                return ()
            identifiers = tuple(str(row["assignment_id"]) for row in rows)
            placeholders = ",".join("?" for _ in identifiers)
            updated = connection.execute(
                "SELECT * FROM bunshin_role_assignments "
                f"WHERE assignment_id IN ({placeholders}) ORDER BY created_at, assignment_id",
                identifiers,
            ).fetchall()
            return tuple(_decode_role_assignment(row) for row in updated)

    @staticmethod
    def _cancel_attempt_locked(
        connection: sqlite3.Connection, attempt_id: str, *, reason: str, now: str,
        preserve_process_record: bool,
    ) -> None:
        attempt = connection.execute(
            "SELECT * FROM bunshin_role_attempts WHERE attempt_id = ?", (attempt_id,),
        ).fetchone()
        if attempt is None:
            return
        connection.execute(
            """UPDATE bunshin_role_attempts
               SET status = ?, access_token_hash = '', error_kind = ?,
                   error_text = ?, finished_at = ?, updated_at = ?
               WHERE attempt_id = ?""",
            (RoleAttemptState.CANCELLED.value, "aggregate_control", str(reason), now, now, attempt_id),
        )
        lease_resource = str(attempt["lease_resource_key"] or "")
        fencing_token = int(attempt["fencing_token"] or 0)
        if not lease_resource or not fencing_token:
            return
        if preserve_process_record:
            # Reconciliation still needs the original process-group identity.
            connection.execute(
                """UPDATE bunshin_leases SET owner_id='', renewed_at=?, expires_at=?
                   WHERE resource_key=? AND owner_id=? AND fencing_token=?""",
                (now, now, lease_resource, attempt_id, fencing_token),
            )
        else:
            connection.execute(
                """DELETE FROM bunshin_leases
                   WHERE resource_key=? AND owner_id=? AND fencing_token=?""",
                (lease_resource, attempt_id, fencing_token),
            )
