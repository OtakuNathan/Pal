from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _decode_role_assignment
from pal.bunshin.v2.storage.serialization import _json
import json
from dataclasses import dataclass
from typing import Any, Mapping
from pal.foundation import utc_now
from pal.bunshin.v2.role_protocol import RoleAssignmentAction, RoleAssignmentState, RoleAttemptState, RoleSessionAction, RoleSubmissionReceipt, role_assignment_target
from pal.bunshin.v2.storage.artifacts import ArtifactsStore
from pal.bunshin.v2.storage.connection_contracts import DatabasePort
from pal.bunshin.v2.storage.role_attempts import RoleAttemptsStore
from pal.bunshin.v2.storage.role_sessions import RoleSessionsStore


@dataclass
class RoleRetriesStore:
    artifacts: ArtifactsStore
    database: DatabasePort
    role_attempts: RoleAttemptsStore
    role_sessions: RoleSessionsStore

    def queue_role_attempt_retry(
        self,
        *,
        assignment_id: str,
        attempt_id_value: str,
        error_kind: str,
        error_text: str,
    ) -> dict[str, Any]:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            assignment, attempt = self.role_attempts.role_assignment_attempt_locked(
                connection,
                assignment_id=assignment_id,
                attempt_id_value=attempt_id_value,
            )
            if str(assignment["state"]) in {
                RoleAssignmentState.SETTLED.value,
                RoleAssignmentState.CANCELLED.value,
                RoleAssignmentState.RESULT_RECORDED.value,
            }:
                return _decode_role_assignment(assignment)
            now = utc_now()
            target = role_assignment_target(
                str(assignment["state"]),
                RoleAssignmentAction.QUEUE_RETRY,
            )
            connection.execute(
                """
                UPDATE bunshin_v2_role_attempts
                SET status = ?, error_kind = ?, error_text = ?,
                    finished_at = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    RoleAttemptState.LOST.value,
                    str(error_kind),
                    str(error_text),
                    now,
                    now,
                    str(attempt_id_value),
                ),
            )
            connection.execute(
                """
                UPDATE bunshin_v2_role_assignments
                SET state = ?, last_error = ?, updated_at = ?
                WHERE assignment_id = ?
                """,
                (target.value, str(error_text), now, str(assignment_id)),
            )
            row = connection.execute(
                "SELECT * FROM bunshin_v2_role_assignments WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
            return _decode_role_assignment(row)

    def record_role_failure_result(
        self,
        *,
        assignment_id: str,
        attempt_id_value: str,
        error_kind: str,
        error_text: str,
        failure_artifact_ref: Mapping[str, Any],
        payload_hash: str,
        settlement_action: Mapping[str, Any],
    ) -> RoleSubmissionReceipt:
        """Record an exhausted activation failure as a normal durable result.

        The assignment owns only this receipt. The settlement action advances
        the parent aggregate to its explicit failure state in a separate,
        atomically acknowledged dispatch.
        """

        if not str(payload_hash or "").strip():
            raise ValueError("role failure result requires a payload hash")
        if str(dict(settlement_action or {}).get("action_type") or "") != "ROLE_FAILED":
            raise ValueError("role failure result must settle through ROLE_FAILED")
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
                receipt = RoleSubmissionReceipt(
                    assignment_id=str(assignment_id),
                    artifact_ref=existing_ref,
                    payload_hash=str(assignment["submission_payload_hash"]),
                    settlement_action=json.loads(
                        str(assignment["settlement_action_json"] or "{}")
                    ),
                )
                requested = RoleSubmissionReceipt(
                    assignment_id=str(assignment_id),
                    artifact_ref=dict(failure_artifact_ref),
                    payload_hash=str(payload_hash),
                    settlement_action=dict(settlement_action),
                )
                if receipt.to_dict() != requested.to_dict():
                    raise ValueError(
                        "role assignment already has a different result receipt"
                    )
                return receipt
            target_state = role_assignment_target(
                str(assignment["state"]),
                RoleAssignmentAction.RECORD_RESULT,
            )
            self.artifacts.assert_artifact_refs_durable(connection, failure_artifact_ref)
            now = utc_now()
            connection.execute(
                """
                UPDATE bunshin_v2_role_attempts
                SET status = ?, error_kind = ?, error_text = ?,
                    response_artifact_ref_json = ?, access_token_hash = '',
                    finished_at = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    RoleAttemptState.FAILED.value,
                    str(error_kind),
                    str(error_text),
                    _json(dict(failure_artifact_ref)),
                    now,
                    now,
                    str(attempt_id_value),
                ),
            )
            connection.execute(
                """
                UPDATE bunshin_v2_role_assignments
                SET state = ?, submission_artifact_ref_json = ?,
                    submission_payload_hash = ?, settlement_action_json = ?,
                    last_error = ?, updated_at = ?
                WHERE assignment_id = ?
                """,
                (
                    target_state.value,
                    _json(dict(failure_artifact_ref)),
                    str(payload_hash),
                    _json(dict(settlement_action)),
                    str(error_text),
                    now,
                    str(assignment_id),
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
                    (lease_resource, str(attempt_id_value), fencing_token),
                )
            self.role_sessions.transition_role_session_locked(
                connection,
                str(assignment["session_id"]),
                RoleSessionAction.PARK,
                now=now,
            )
            return RoleSubmissionReceipt(
                assignment_id=str(assignment_id),
                artifact_ref=dict(failure_artifact_ref),
                payload_hash=str(payload_hash),
                settlement_action=dict(settlement_action),
            )
