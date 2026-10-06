from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _decode_role_assignment
from dataclasses import dataclass
from typing import Any
from pal.foundation import utc_now
from pal.bunshin.v2.contracts import AggregateType
from pal.bunshin.v2.role_protocol import RoleAssignmentAction, RoleAssignmentState, RoleAttemptState, RoleSessionAction, role_assignment_target
from pal.bunshin.v2.storage.connection_contracts import DatabasePort
from pal.bunshin.v2.storage.role_sessions import RoleSessionsStore
from pal.bunshin.v2.storage.role_submissions import RoleSubmissionsStore


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
        assignment_ids: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Terminate nonterminal invocations bound to one aggregate.

        Pause and cancel both end the current assignment. The durable worker
        session remains resumable unless the caller separately closes it.
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
            rows = connection.execute(
                """
                SELECT * FROM bunshin_v2_role_assignments
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
            if assignment_ids is not None:
                exact_ids = frozenset(str(item) for item in assignment_ids)
                rows = [row for row in rows if str(row["assignment_id"]) in exact_ids]
            now = utc_now()
            for assignment in rows:
                if str(assignment["state"]) == RoleAssignmentState.RESULT_RECORDED.value:
                    self.role_submissions.settle_role_assignment_locked(
                        connection,
                        assignment_id=str(assignment["assignment_id"]),
                        submission_payload_hash=str(
                            assignment["submission_payload_hash"]
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE bunshin_v2_role_assignments
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
                        "SELECT * FROM bunshin_v2_role_attempts WHERE attempt_id = ?",
                        (attempt_id_value,),
                    ).fetchone()
                    if attempt is not None:
                        connection.execute(
                            """
                            UPDATE bunshin_v2_role_attempts
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
                                DELETE FROM bunshin_v2_leases
                                WHERE resource_key = ? AND owner_id = ? AND fencing_token = ?
                                """,
                                (lease_resource, attempt_id_value, fencing_token),
                            )
                connection.execute(
                    """
                    UPDATE bunshin_v2_role_assignments
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
                "SELECT * FROM bunshin_v2_role_assignments "
                f"WHERE assignment_id IN ({placeholders}) ORDER BY created_at, assignment_id",
                identifiers,
            ).fetchall()
            return tuple(_decode_role_assignment(row) for row in updated)
