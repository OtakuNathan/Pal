from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _decode_role_assignment
from pal.bunshin.v2.storage.serialization import _decode_role_attempt
from pal.bunshin.v2.storage.serialization import _json
from dataclasses import dataclass
from typing import Any, Mapping
import json
import sqlite3
from pal.bunshin.v2.contracts import AggregateSnapshot, AggregateType, DeferredEffectError
from pal.bunshin.v2.storage.leases import LeasesStore
from pal.bunshin.v2.storage.serialization import _utc_datetime
from pal.foundation import utc_now
from pal.bunshin.v2.role_protocol import RoleAssignmentAction, RoleAssignmentRequest, RoleAssignmentState, RoleAttemptState, RoleSessionState, attempt_id, role_assignment_target
from pal.bunshin.v2.storage.artifacts import ArtifactsStore
from pal.bunshin.v2.storage.connection_contracts import DatabasePort
from pal.bunshin.v2.storage.role_session_checks import RoleSessionChecksStore
from pal.bunshin.v2.storage.role_business_leases import record_role_attempt_business_lease_locked


def semantic_business_lease(snapshot: AggregateSnapshot, *, owner_id: str, resource_key: str, fencing_token: int) -> dict[str, Any]:
    """Opt real semantic node execution into the durable admission guard."""
    if not isinstance(snapshot, AggregateSnapshot) or snapshot.aggregate_type != AggregateType.DAG_NODE_RUN:
        return {}
    return {
        "owner_id": str(owner_id), "resource_key": str(resource_key),
        "fencing_token": int(fencing_token), "expected_state": snapshot.state,
        "epoch_id": str(snapshot.payload.get("epoch_id") or ""),
        "graph_generation": int(snapshot.payload.get("graph_generation") or 0),
    }


def assert_semantic_role_admission_locked(
    connection: sqlite3.Connection, *, workflow_id: str, aggregate_type: str,
    aggregate_id: str, execution_spec: Mapping[str, Any],
    business_lease: Mapping[str, Any] | None = None,
    require_running: bool = True,
) -> None:
    """Serialize setup/claim/start against the real aggregate incarnation.

    Legacy standalone role-protocol clients do not supply a semantic binding.
    A supplied binding is never silently weakened when malformed or obsolete.
    """
    original = execution_spec.get("business_lease")
    binding = business_lease if business_lease is not None else original
    if binding is None:
        return
    if not isinstance(binding, Mapping) or not binding:
        raise ValueError("semantic role admission requires a business lease binding")
    if aggregate_type != AggregateType.DAG_NODE_RUN.value:
        raise ValueError("semantic role lease binding requires a node aggregate")
    owner = str(binding.get("owner_id") or "")
    resource = str(binding.get("resource_key") or "")
    token = int(binding.get("fencing_token") or 0)
    state = str(binding.get("expected_state") or "")
    if not owner or not resource or token <= 0 or not state:
        raise ValueError("semantic role admission has an incomplete business lease")
    current = connection.execute(
        "SELECT workflow_id, state, payload_json FROM bunshin_v2_aggregate_snapshots "
        "WHERE aggregate_type = ? AND aggregate_id = ?",
        (aggregate_type, aggregate_id),
    ).fetchone()
    if current is None or str(current["workflow_id"]) != workflow_id:
        raise DeferredEffectError("semantic role aggregate no longer exists")
    payload = json.loads(str(current["payload_json"]))
    if (
        str(current["state"]) != state
        or state not in (
            {"PRODUCING", "REPAIRING", "REVIEWING"}
            if require_running else
            {"PRODUCING", "REPAIRING", "REVIEWING", "QUIESCING", "SNAPSHOTTING",
             "REVIEW_QUIESCING", "REVIEW_SNAPSHOTTING"}
        )
        or str(payload.get("active_worker_id") or "") != owner
        or str(payload.get("lease_resource_key") or "") != resource
        or int(payload.get("fencing_token") or 0) != token
        or str(payload.get("epoch_id") or "") != str(binding.get("epoch_id") or "")
        or int(payload.get("graph_generation") or 0) != int(binding.get("graph_generation") or 0)
    ):
        raise DeferredEffectError("semantic role aggregate incarnation is frozen or replaced")
    lease = connection.execute(
        "SELECT * FROM bunshin_v2_leases WHERE resource_key = ?", (resource,),
    ).fetchone()
    LeasesStore.assert_lease_row(lease, owner_id=owner, fencing_token=token, now=_utc_datetime())


@dataclass
class RoleAssignmentsStore:
    artifacts: ArtifactsStore
    database: DatabasePort
    role_session_checks: RoleSessionChecksStore

    def create_role_assignment(self, request: RoleAssignmentRequest) -> dict[str, Any]:
        self.database.ensure_schema()
        now = utc_now()
        with self.database.write_connection() as connection:
            assert_semantic_role_admission_locked(
                connection, workflow_id=request.workflow_id,
                aggregate_type=request.aggregate_type, aggregate_id=request.aggregate_id,
                execution_spec=request.execution_spec,
            )
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
        business_lease: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            assignment = connection.execute(
                "SELECT * FROM bunshin_v2_role_assignments WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
            if assignment is None:
                raise KeyError(f"unknown role assignment: {assignment_id}")
            assert_semantic_role_admission_locked(
                connection, workflow_id=str(assignment["workflow_id"]),
                aggregate_type=str(assignment["aggregate_type"]),
                aggregate_id=str(assignment["aggregate_id"]),
                execution_spec=json.loads(str(assignment["execution_spec_json"])),
                business_lease=business_lease,
            )
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
            binding = business_lease if business_lease is not None else json.loads(
                str(assignment["execution_spec_json"])
            ).get("business_lease")
            if binding is not None:
                record_role_attempt_business_lease_locked(
                    self.database, connection, assignment=assignment,
                    attempt_id=identifier, business_lease=binding,
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

    def assert_semantic_admission(
        self, *, workflow_id: str, aggregate_type: str, aggregate_id: str,
        business_lease: Mapping[str, Any], require_running: bool = True,
    ) -> None:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            assert_semantic_role_admission_locked(
                connection, workflow_id=workflow_id, aggregate_type=aggregate_type,
                aggregate_id=aggregate_id, execution_spec={}, business_lease=business_lease,
                require_running=require_running,
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
