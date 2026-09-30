from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _decode_role_assignment
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any
from pal.foundation import utc_now
from pal.bunshin.v2.contracts import StaleFencingToken
from pal.bunshin.v2.role_protocol import RoleAssignmentState, RoleAttemptState
from pal.bunshin.v2.storage.connection_contracts import DatabasePort
from pal.bunshin.v2.storage.leases import LeasesStore
from pal.bunshin.v2.storage.role_attempts import RoleAttemptsStore


@dataclass
class RoleAccessStore:
    database: DatabasePort
    leases: LeasesStore
    role_attempts: RoleAttemptsStore

    def issue_role_attempt_access_token(
        self,
        *,
        assignment_id: str,
        attempt_id_value: str,
        fencing_token: int,
    ) -> str:
        """Issue one opaque token for the active process attempt.

        Only the token hash is durable. A later attempt always replaces the
        authorization surface, while the cognitive session remains reusable.
        """

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            assignment, attempt = self.role_attempts.role_assignment_attempt_locked(
                connection,
                assignment_id=assignment_id,
                attempt_id_value=attempt_id_value,
            )
            if str(assignment["state"]) != RoleAssignmentState.RUNNING.value:
                raise ValueError("role assignment is not running")
            self.leases.assert_lease_locked(
                connection,
                str(attempt["lease_resource_key"]),
                str(attempt_id_value),
                int(fencing_token),
            )
            connection.execute(
                """
                UPDATE bunshin_v2_role_attempts
                SET access_token_hash = ?, updated_at = ?
                WHERE attempt_id = ? AND status = ?
                """,
                (
                    token_hash,
                    utc_now(),
                    str(attempt_id_value),
                    RoleAttemptState.RUNNING.value,
                ),
            )
        return token

    def authenticate_role_attempt(self, access_token: str) -> dict[str, Any]:
        token = str(access_token or "").strip()
        if not token:
            raise ValueError("role assignment access token is required")
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    a.*,
                    t.attempt_id AS authenticated_attempt_id,
                    t.attempt_index AS authenticated_attempt_index,
                    t.lease_resource_key AS authenticated_lease_resource_key,
                    t.fencing_token AS authenticated_fencing_token,
                    t.status AS authenticated_attempt_status
                FROM bunshin_v2_role_attempts AS t
                JOIN bunshin_v2_role_assignments AS a
                  ON a.assignment_id = t.assignment_id
                WHERE t.access_token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                raise ValueError("role assignment access token is invalid")
            if str(row["active_attempt_id"]) != str(row["authenticated_attempt_id"]):
                raise StaleFencingToken("role assignment token belongs to a stale attempt")
            if str(row["state"]) not in {
                RoleAssignmentState.RUNNING.value,
                RoleAssignmentState.RESULT_RECORDED.value,
            }:
                raise ValueError("role assignment is not active")
            if str(row["authenticated_attempt_status"]) not in {
                RoleAttemptState.RUNNING.value,
                RoleAttemptState.SUBMITTED.value,
            }:
                raise StaleFencingToken("role assignment attempt is no longer active")
            self.leases.assert_lease_locked(
                connection,
                str(row["authenticated_lease_resource_key"]),
                str(row["authenticated_attempt_id"]),
                int(row["authenticated_fencing_token"]),
            )
            assignment = _decode_role_assignment(row)
            return {
                "assignment": assignment,
                "attempt_id": str(row["authenticated_attempt_id"]),
                "attempt_index": int(row["authenticated_attempt_index"]),
                "lease_resource_key": str(row["authenticated_lease_resource_key"]),
                "fencing_token": int(row["authenticated_fencing_token"]),
            }
