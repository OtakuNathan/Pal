from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _decode_role_assignment
from pal.bunshin.v2.storage.serialization import _decode_role_attempt
from pal.bunshin.v2.storage.serialization import _json
from dataclasses import dataclass
from typing import Any
from pal.foundation import utc_now
from pal.bunshin.v2.role_protocol import RoleAssignmentAction, RoleAssignmentRequest, RoleAssignmentState, RoleAttemptState, RoleSessionState, attempt_id, role_assignment_target
from pal.bunshin.v2.storage.artifacts import ArtifactsStore
from pal.bunshin.v2.storage.connection_contracts import DatabasePort
from pal.bunshin.v2.storage.role_session_checks import RoleSessionChecksStore


@dataclass
class RoleAssignmentsStore:
    artifacts: ArtifactsStore
    database: DatabasePort
    role_session_checks: RoleSessionChecksStore

    def create_role_assignment(self, request: RoleAssignmentRequest) -> dict[str, Any]:
        self.database.ensure_schema()
        now = utc_now()
        with self.database.write_connection() as connection:
            session = connection.execute(
                "SELECT * FROM bunshin_v2_role_sessions WHERE session_id = ?",
                (request.session_id,),
            ).fetchone()
            if session is None:
                raise ValueError("role assignment requires an existing role session")
            if str(session["status"]) in {
                RoleSessionState.COMPLETED.value,
                RoleSessionState.CANCELLED.value,
            }:
                raise ValueError("role assignment cannot use a terminal role session")
            session_identity = (
                str(session["workflow_id"]),
                str(session["role"]),
                str(session["role_profile_id"]),
                str(session["family_binding_sha"]),
            )
            request_identity = (
                request.workflow_id,
                request.role,
                request.role_profile_id,
                request.family_binding_sha,
            )
            if session_identity != request_identity:
                raise ValueError("role assignment does not match its session identity")
            self.role_session_checks.assert_assignment_in_role_session_scope_locked(
                connection,
                session,
                request,
            )
            self.artifacts.assert_artifact_refs_durable(connection, request.input_refs)
            existing = connection.execute(
                "SELECT * FROM bunshin_v2_role_assignments WHERE assignment_key = ?",
                (request.assignment_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["request_hash"]) != request.request_hash:
                    raise ValueError("role assignment key was reused with different inputs")
                return _decode_role_assignment(existing)
            open_assignment = connection.execute(
                """
                SELECT assignment_id
                FROM bunshin_v2_role_assignments
                WHERE session_id = ?
                  AND state IN (
                      'queued', 'claimed', 'running', 'retry_queued',
                      'result_recorded'
                  )
                LIMIT 1
                """,
                (request.session_id,),
            ).fetchone()
            if open_assignment is not None:
                raise ValueError(
                    "role session already has an open assignment: "
                    + str(open_assignment["assignment_id"])
                )
            connection.execute(
                """
                INSERT INTO bunshin_v2_role_assignments(
                    assignment_id, assignment_key, request_hash, session_id,
                    workflow_id, aggregate_type, aggregate_id, role, mode,
                    role_profile_id, family_binding_sha,
                    input_fingerprint, required_inputs_json, input_refs_json,
                    execution_spec_json, submission_kind, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.assignment_id,
                    request.assignment_key,
                    request.request_hash,
                    request.session_id,
                    request.workflow_id,
                    request.aggregate_type,
                    request.aggregate_id,
                    request.role,
                    request.mode,
                    request.role_profile_id,
                    request.family_binding_sha,
                    request.input_fingerprint,
                    _json(sorted(request.required_inputs)),
                    _json({name: dict(ref) for name, ref in request.input_refs.items()}),
                    _json(dict(request.execution_spec)),
                    request.submission_kind,
                    RoleAssignmentState.QUEUED.value,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM bunshin_v2_role_assignments WHERE assignment_id = ?",
                (request.assignment_id,),
            ).fetchone()
            return _decode_role_assignment(row)

    def claim_role_assignment(
        self,
        assignment_id: str,
        *,
        harness_id: str = "pal",
        harness_generation: str = "",
    ) -> dict[str, Any]:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            assignment = connection.execute(
                "SELECT * FROM bunshin_v2_role_assignments WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
            if assignment is None:
                raise KeyError(f"unknown role assignment: {assignment_id}")
            if str(assignment["state"]) not in {
                RoleAssignmentState.QUEUED.value,
                RoleAssignmentState.RETRY_QUEUED.value,
            }:
                raise ValueError("role assignment is not claimable")
            target_state = role_assignment_target(
                str(assignment["state"]),
                RoleAssignmentAction.CLAIM,
            )
            attempt_index = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(attempt_index), 0) + 1
                    FROM bunshin_v2_role_attempts WHERE assignment_id = ?
                    """,
                    (str(assignment_id),),
                ).fetchone()[0]
            )
            identifier = attempt_id(str(assignment_id), attempt_index)
            now = utc_now()
            connection.execute(
                """
                INSERT INTO bunshin_v2_role_attempts(
                    attempt_id, assignment_id, attempt_index, lease_resource_key,
                    fencing_token, harness_id, harness_generation,
                    status, started_at, updated_at
                ) VALUES (?, ?, ?, '', 0, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    str(assignment_id),
                    attempt_index,
                    str(harness_id or "pal"),
                    str(harness_generation or ""),
                    RoleAttemptState.STARTING.value,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE bunshin_v2_role_assignments
                SET state = ?, active_attempt_id = ?, last_error = '', updated_at = ?
                WHERE assignment_id = ?
                """,
                (
                    target_state.value,
                    identifier,
                    now,
                    str(assignment_id),
                ),
            )
            # The session is the logical coroutine while an attempt is only
            # its current process shell.  Pin the shell that actually claimed
            # the assignment, not merely the harness that was preferred when
            # the session was first created.  A fallback may select Pal after
            # an optional harness fails; later registry refreshes must then
            # restore the Pal-authored checkpoint with that same generation.
            connection.execute(
                """
                UPDATE bunshin_v2_role_sessions
                SET preferred_harness_id = ?,
                    preferred_harness_generation = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (
                    str(harness_id or "pal"),
                    str(harness_generation or ""),
                    now,
                    str(assignment["session_id"]),
                ),
            )
            return _decode_role_attempt(
                connection.execute(
                    "SELECT * FROM bunshin_v2_role_attempts WHERE attempt_id = ?",
                    (identifier,),
                ).fetchone()
            )

    def read_role_assignment(self, assignment_id: str) -> dict[str, Any] | None:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_role_assignments WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
            return _decode_role_assignment(row) if row is not None else None

    def list_role_assignments(
        self,
        *,
        workflow_id: str = "",
        states: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], ...]:
        self.database.ensure_schema()
        clauses: list[str] = []
        parameters: list[Any] = []
        if workflow_id:
            clauses.append("workflow_id = ?")
            parameters.append(str(workflow_id))
        if states:
            clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
            parameters.extend(str(item) for item in states)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.database.read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM bunshin_v2_role_assignments"
                + where
                + " ORDER BY created_at, assignment_id",
                tuple(parameters),
            ).fetchall()
        return tuple(_decode_role_assignment(row) for row in rows)
