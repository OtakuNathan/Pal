from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _decode_role_assignment
from pal.bunshin.v2.storage.serialization import _json
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping
from pal.foundation import utc_now
from pal.bunshin.v2.role_protocol import RoleAssignmentAction, RoleAssignmentState, RoleAttemptState, RoleSessionAction, RoleSubmissionReceipt, role_assignment_target
from pal.bunshin.v2.storage.artifacts import ArtifactsStore
from pal.bunshin.v2.storage.connection_contracts import DatabasePort
from pal.bunshin.v2.storage.leases import LeasesStore
from pal.bunshin.v2.storage.role_attempts import RoleAttemptsStore
from pal.bunshin.v2.storage.role_sessions import RoleSessionsStore


@dataclass
class RoleSubmissionsStore:
    artifacts: ArtifactsStore
    database: DatabasePort
    leases: LeasesStore
    role_attempts: RoleAttemptsStore
    role_sessions: RoleSessionsStore

    def record_role_submission(
        self,
        *,
        assignment_id: str,
        attempt_id_value: str,
        fencing_token: int,
        artifact_ref: Mapping[str, Any],
        payload_hash: str,
        settlement_action: Mapping[str, Any],
    ) -> RoleSubmissionReceipt:
        if not str(payload_hash or "").strip():
            raise ValueError("role submission requires a payload hash")
        if not dict(settlement_action or {}).get("action_type"):
            raise ValueError("role submission requires a settlement action")
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            assignment, attempt = self.role_attempts.role_assignment_attempt_locked(
                connection,
                assignment_id=assignment_id,
                attempt_id_value=attempt_id_value,
            )
            existing_ref = json.loads(
                str(assignment["submission_artifact_ref_json"] or "{}")
            )
            if existing_ref:
                existing = RoleSubmissionReceipt(
                    assignment_id=str(assignment_id),
                    artifact_ref=existing_ref,
                    payload_hash=str(assignment["submission_payload_hash"]),
                    settlement_action=json.loads(
                        str(assignment["settlement_action_json"] or "{}")
                    ),
                )
                requested = RoleSubmissionReceipt(
                    assignment_id=str(assignment_id),
                    artifact_ref=dict(artifact_ref),
                    payload_hash=str(payload_hash),
                    settlement_action=dict(settlement_action),
                )
                if existing.to_dict() != requested.to_dict():
                    raise ValueError(
                        "role assignment already has a different submission receipt"
                    )
                return existing
            if str(assignment["state"]) != RoleAssignmentState.RUNNING.value:
                raise ValueError("role assignment is not accepting a submission")
            target_state = role_assignment_target(
                str(assignment["state"]),
                RoleAssignmentAction.RECORD_RESULT,
            )
            self.leases.assert_lease_locked(
                connection,
                str(attempt["lease_resource_key"]),
                str(attempt_id_value),
                int(fencing_token),
            )
            self.artifacts.assert_artifact_refs_durable(connection, artifact_ref)
            now = utc_now()
            connection.execute(
                """
                UPDATE bunshin_v2_role_assignments
                SET state = ?, submission_artifact_ref_json = ?,
                    submission_payload_hash = ?, settlement_action_json = ?,
                    updated_at = ?
                WHERE assignment_id = ?
                """,
                (
                    target_state.value,
                    _json(dict(artifact_ref)),
                    str(payload_hash),
                    _json(dict(settlement_action)),
                    now,
                    str(assignment_id),
                ),
            )
            connection.execute(
                """
                UPDATE bunshin_v2_role_attempts
                SET status = ?, response_artifact_ref_json = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    RoleAttemptState.SUBMITTED.value,
                    _json(dict(artifact_ref)),
                    now,
                    str(attempt_id_value),
                ),
            )
            return RoleSubmissionReceipt(
                assignment_id=str(assignment_id),
                artifact_ref=dict(artifact_ref),
                payload_hash=str(payload_hash),
                settlement_action=dict(settlement_action),
            )

    def settle_role_assignment(
        self,
        *,
        assignment_id: str,
        submission_payload_hash: str,
    ) -> dict[str, Any]:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            self.settle_role_assignment_locked(
                connection,
                assignment_id=str(assignment_id),
                submission_payload_hash=str(submission_payload_hash),
            )
            row = connection.execute(
                "SELECT * FROM bunshin_v2_role_assignments WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
            return _decode_role_assignment(row)

    def settle_role_assignment_locked(
        self,
        connection: sqlite3.Connection,
        *,
        assignment_id: str,
        submission_payload_hash: str,
        workflow_id: str = "",
        aggregate_type: str = "",
        aggregate_id: str = "",
    ) -> None:
        assignment = connection.execute(
            "SELECT * FROM bunshin_v2_role_assignments WHERE assignment_id = ?",
            (str(assignment_id),),
        ).fetchone()
        if assignment is None:
            raise KeyError(f"unknown role assignment: {assignment_id}")
        if str(assignment["submission_payload_hash"]) != str(
            submission_payload_hash
        ):
            raise ValueError("role assignment settlement receipt does not match")
        expected_binding = (str(workflow_id), str(aggregate_type), str(aggregate_id))
        if any(expected_binding) and expected_binding != (
            str(assignment["workflow_id"]),
            str(assignment["aggregate_type"]),
            str(assignment["aggregate_id"]),
        ):
            raise ValueError("role assignment settlement targets a different aggregate")
        if str(assignment["state"]) == RoleAssignmentState.SETTLED.value:
            return
        if str(assignment["state"]) != RoleAssignmentState.RESULT_RECORDED.value:
            raise ValueError("role assignment has no recorded submission")
        target_state = role_assignment_target(
            str(assignment["state"]),
            RoleAssignmentAction.SETTLE,
        )
        now = utc_now()
        connection.execute(
            """
            UPDATE bunshin_v2_role_assignments
            SET state = ?, updated_at = ? WHERE assignment_id = ?
            """,
            (target_state.value, now, str(assignment_id)),
        )
        connection.execute(
            """
            UPDATE bunshin_v2_role_attempts
            SET status = CASE WHEN status = ? THEN ? ELSE status END,
                access_token_hash = '', finished_at = ?, updated_at = ?
            WHERE attempt_id = ?
            """,
            (
                RoleAttemptState.SUBMITTED.value,
                RoleAttemptState.COMPLETED.value,
                now,
                now,
                str(assignment["active_attempt_id"]),
            ),
        )
        attempt = connection.execute(
            "SELECT * FROM bunshin_v2_role_attempts WHERE attempt_id = ?",
            (str(assignment["active_attempt_id"]),),
        ).fetchone()
        if attempt is not None:
            lease_resource = str(attempt["lease_resource_key"] or "")
            fencing_token = int(attempt["fencing_token"] or 0)
            if lease_resource and fencing_token:
                connection.execute(
                    """
                    DELETE FROM bunshin_v2_leases
                    WHERE resource_key = ? AND owner_id = ? AND fencing_token = ?
                    """,
                    (
                        lease_resource,
                        str(assignment["active_attempt_id"]),
                        fencing_token,
                    ),
                )
        self.role_sessions.transition_role_session_locked(
            connection,
            str(assignment["session_id"]),
            (
                RoleSessionAction.PARK
                if json.loads(str(assignment["settlement_action_json"] or "{}")).get("action_type") == "ROLE_FAILED"
                else RoleSessionAction.SUSPEND
            ),
            now=now,
        )
