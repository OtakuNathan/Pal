from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _decode_role_attempt
from pal.bunshin.v2.storage.serialization import _json
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping
from pal.foundation import utc_now
from pal.bunshin.v2.role_protocol import RoleAssignmentAction, RoleAssignmentState, RoleAttemptState, RoleSessionAction, role_assignment_target
from pal.bunshin.v2.storage.artifacts import ArtifactsStore
from pal.bunshin.v2.storage.connection_contracts import DatabasePort
from pal.bunshin.v2.storage.leases import LeasesStore
from pal.bunshin.v2.storage.role_sessions import RoleSessionsStore


@dataclass
class RoleAttemptsStore:
    artifacts: ArtifactsStore
    database: DatabasePort
    leases: LeasesStore
    role_sessions: RoleSessionsStore

    def start_role_attempt(
        self,
        *,
        assignment_id: str,
        attempt_id_value: str,
        lease_resource_key: str,
        fencing_token: int,
        prompt_pack_ref: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            assignment, _attempt = self.role_assignment_attempt_locked(
                connection,
                assignment_id=assignment_id,
                attempt_id_value=attempt_id_value,
            )
            if str(assignment["state"]) != RoleAssignmentState.CLAIMED.value:
                raise ValueError("role assignment is not claimed")
            target_state = role_assignment_target(
                str(assignment["state"]),
                RoleAssignmentAction.START,
            )
            self.leases.assert_lease_locked(
                connection,
                str(lease_resource_key),
                str(attempt_id_value),
                int(fencing_token),
            )
            self.artifacts.assert_artifact_refs_durable(connection, prompt_pack_ref)
            now = utc_now()
            connection.execute(
                """
                UPDATE bunshin_v2_role_attempts
                SET lease_resource_key = ?, fencing_token = ?, status = ?,
                    prompt_pack_ref_json = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    str(lease_resource_key),
                    int(fencing_token),
                    RoleAttemptState.RUNNING.value,
                    _json(dict(prompt_pack_ref)),
                    now,
                    str(attempt_id_value),
                ),
            )
            connection.execute(
                """
                UPDATE bunshin_v2_role_assignments
                SET state = ?, updated_at = ? WHERE assignment_id = ?
                """,
                (
                    target_state.value,
                    now,
                    str(assignment_id),
                ),
            )
            self.role_sessions.transition_role_session_locked(
                connection,
                str(assignment["session_id"]),
                RoleSessionAction.ACTIVATE,
                now=now,
            )
            row = connection.execute(
                "SELECT * FROM bunshin_v2_role_attempts WHERE attempt_id = ?",
                (str(attempt_id_value),),
            ).fetchone()
            return _decode_role_attempt(row)

    def read_role_attempt(self, attempt_id_value: str) -> dict[str, Any] | None:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_role_attempts WHERE attempt_id = ?",
                (str(attempt_id_value),),
            ).fetchone()
            return _decode_role_attempt(row) if row is not None else None

    def read_latest_completed_role_harness_attempt(
        self,
        *,
        session_id: str,
        harness_id: str,
    ) -> dict[str, Any] | None:
        """Return the process shell that authored the resumable session state."""

        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT attempt.*
                FROM bunshin_v2_role_attempts AS attempt
                JOIN bunshin_v2_role_assignments AS assignment
                  ON assignment.assignment_id = attempt.assignment_id
                WHERE assignment.session_id = ?
                  AND attempt.harness_id = ?
                  AND attempt.status = ?
                ORDER BY attempt.finished_at DESC, attempt.started_at DESC,
                         attempt.attempt_index DESC
                LIMIT 1
                """,
                (
                    str(session_id),
                    str(harness_id),
                    RoleAttemptState.COMPLETED.value,
                ),
            ).fetchone()
        return _decode_role_attempt(row) if row is not None else None

    def read_role_harness_continuation(
        self,
        *,
        session_id: str,
        harness_id: str,
        harness_generation: str,
    ) -> dict[str, Any]:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT attempt.harness_state_json
                FROM bunshin_v2_role_attempts AS attempt
                JOIN bunshin_v2_role_assignments AS assignment
                  ON assignment.assignment_id = attempt.assignment_id
                WHERE assignment.session_id = ? AND attempt.harness_id = ?
                  AND attempt.harness_generation = ?
                  AND attempt.harness_state_json != '{}'
                ORDER BY attempt.started_at DESC, attempt.attempt_index DESC
                LIMIT 1
                """,
                (
                    str(session_id),
                    str(harness_id),
                    str(harness_generation),
                ),
            ).fetchone()
        if row is None:
            return {}
        value = json.loads(str(row["harness_state_json"] or "{}"))
        return dict(value) if isinstance(value, Mapping) else {}

    def write_role_attempt_harness_state(
        self,
        *,
        assignment_id: str,
        attempt_id_value: str,
        fencing_token: int,
        harness_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            _assignment, attempt = self.role_assignment_attempt_locked(
                connection,
                assignment_id=str(assignment_id),
                attempt_id_value=str(attempt_id_value),
            )
            self.leases.assert_lease_locked(
                connection,
                str(attempt["lease_resource_key"]),
                str(attempt_id_value),
                int(fencing_token),
            )
            if str(attempt["status"]) != RoleAttemptState.RUNNING.value:
                raise ValueError("role attempt is not running")
            state = dict(harness_state or {})
            encoded = _json(state)
            if len(encoded.encode("utf-8")) > 64 * 1024:
                raise ValueError("harness continuation exceeds 64 KiB")
            connection.execute(
                """
                UPDATE bunshin_v2_role_attempts
                SET harness_state_json = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (encoded, utc_now(), str(attempt_id_value)),
            )
            return state

    def list_role_attempts(self, assignment_id: str) -> tuple[dict[str, Any], ...]:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bunshin_v2_role_attempts
                WHERE assignment_id = ?
                ORDER BY attempt_index
                """,
                (str(assignment_id),),
            ).fetchall()
        return tuple(_decode_role_attempt(row) for row in rows)

    @staticmethod
    def role_assignment_attempt_locked(
        connection: sqlite3.Connection,
        *,
        assignment_id: str,
        attempt_id_value: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        assignment = connection.execute(
            "SELECT * FROM bunshin_v2_role_assignments WHERE assignment_id = ?",
            (str(assignment_id),),
        ).fetchone()
        if assignment is None:
            raise KeyError(f"unknown role assignment: {assignment_id}")
        attempt = connection.execute(
            """
            SELECT * FROM bunshin_v2_role_attempts
            WHERE attempt_id = ? AND assignment_id = ?
            """,
            (str(attempt_id_value), str(assignment_id)),
        ).fetchone()
        if attempt is None or str(assignment["active_attempt_id"]) != str(
            attempt_id_value
        ):
            raise ValueError("role attempt is not active for this assignment")
        return assignment, attempt
