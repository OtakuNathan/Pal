from __future__ import annotations
from pal.bunshin.storage.serialization import _decode_role_attempt
from pal.bunshin.storage.serialization import _json
import json
import sqlite3
from pal.bunshin.checkpoint import AgentSessionCheckpointError, LogicalCoroutineCheckpointStore, open_agent_session_checkpoint
from pal.bunshin.workspace_resources import inherited_process_group_live
from pal.bunshin.paths import invocation_root
from dataclasses import dataclass
from typing import Any, Mapping
from pal.foundation import utc_now
from pal.bunshin.role_protocol import RoleAssignmentAction, RoleAssignmentState, RoleAttemptState, RoleSessionAction, role_assignment_target
from pal.bunshin.storage.artifacts import ArtifactsStore
from pal.bunshin.storage.connection_contracts import DatabasePort
from pal.bunshin.storage.leases import LeasesStore
from pal.bunshin.storage.role_sessions import RoleSessionsStore
from pal.bunshin.storage.role_assignments import assert_semantic_role_admission_locked
from pal.bunshin.storage.role_business_leases import (
    read_role_attempt_business_lease_locked, record_role_attempt_business_lease_locked,
)


class UnprovenInteractionOutcome(ValueError):
    """The retired attempt has no authenticated closed continuation."""


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
        business_lease: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            assignment, _attempt = self.role_assignment_attempt_locked(
                connection,
                assignment_id=assignment_id,
                attempt_id_value=attempt_id_value,
            )
            assert_semantic_role_admission_locked(
                connection, workflow_id=str(assignment["workflow_id"]),
                aggregate_type=str(assignment["aggregate_type"]),
                aggregate_id=str(assignment["aggregate_id"]),
                execution_spec=json.loads(str(assignment["execution_spec_json"])),
                business_lease=business_lease,
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
            binding = business_lease if business_lease is not None else json.loads(
                str(assignment["execution_spec_json"])
            ).get("business_lease")
            if binding is not None:
                ownership = record_role_attempt_business_lease_locked(
                    self.database, connection, assignment=assignment,
                    attempt_id=attempt_id_value, business_lease=binding,
                )
                # Keep the immutable ownership witness reachable through the
                # exact prompt artifact used by this attempt.
                connection.execute(
                    "INSERT OR IGNORE INTO bunshin_artifact_refs(parent_sha256, child_sha256, relation, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (str(prompt_pack_ref["sha256"]), str(ownership["artifact_ref"]["sha256"]),
                     "role_attempt_business_lease", utc_now()),
                )
            now = utc_now()
            connection.execute(
                """
                UPDATE bunshin_role_attempts
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
                UPDATE bunshin_role_assignments
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
                "SELECT * FROM bunshin_role_attempts WHERE attempt_id = ?",
                (str(attempt_id_value),),
            ).fetchone()
            return _decode_role_attempt(row)

    def publish_initial_role_checkpoint(
        self,
        *,
        assignment_id: str,
        attempt_id_value: str,
        fencing_token: int,
        allow_submitted: bool = False,
        expected_sequence: int | None = None,
        parked_wait_id: str | None = None,
    ) -> dict[str, Any]:
        """Acknowledge only this authenticated live attempt's first safe point."""
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            assignment, attempt = self.role_assignment_attempt_locked(
                connection, assignment_id=assignment_id, attempt_id_value=attempt_id_value,
            )
            self.leases.assert_lease_locked(
                connection, str(attempt["lease_resource_key"]),
                str(attempt_id_value), int(fencing_token),
            )
            allowed = {RoleAttemptState.RUNNING.value}
            if allow_submitted:
                allowed.add(RoleAttemptState.SUBMITTED.value)
            if str(attempt["status"]) not in allowed:
                raise AgentSessionCheckpointError("checkpoint initialization requires a running attempt")
            payload = self._read_attempt_checkpoint(str(assignment["session_id"]), attempt_id_value)
            if expected_sequence is not None and payload.get("sequence") != expected_sequence:
                raise AgentSessionCheckpointError("interaction checkpoint sequence changed")
            return self.role_sessions.publish_role_session_checkpoint_locked(
                connection, session_id=str(assignment["session_id"]),
                fencing_token=fencing_token, checkpoint=payload,
                interaction=expected_sequence is not None,
                parked_wait_id=parked_wait_id,
            )

    def _read_attempt_checkpoint(self, session_id: str, attempt_id_value: str) -> dict[str, Any]:
        path = (invocation_root(self.database.runtime_root) / session_id / "session-attempts"
                / str(attempt_id_value) / "continuation-output.json")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentSessionCheckpointError("worker checkpoint is unreadable") from exc
        if not isinstance(payload, dict):
            raise AgentSessionCheckpointError("worker checkpoint is not an object")
        return payload

    def reconcile_interrupted_interactions(
        self, workflow_id: str, aggregate_type: str, aggregate_id: str, *, allow_unproven: bool = False,
    ) -> None:
        """Adopt the closed boundary of a retired attempt in the affected sessions.

        A module or architecture-cycle session can outlive its aggregate. The
        next aggregate's assignment may still be waiting on the selected
        checkpoint of an earlier assignment in that same session.
        """
        with self.database.write_connection() as connection:
            rows = connection.execute(
                """SELECT w.wait_id, w.state, a.session_id, p.attempt_id, p.fencing_token, p.status,
                          l.metadata_json
                   FROM bunshin_interaction_waits w
                   JOIN bunshin_role_assignments a ON a.assignment_id=w.assignment_id
                   JOIN bunshin_role_attempts p ON p.attempt_id=w.resume_attempt_id
                       AND p.assignment_id=a.assignment_id
                   LEFT JOIN bunshin_leases l ON l.resource_key=p.lease_resource_key
                       AND l.fencing_token=p.fencing_token
                   WHERE w.state IN ('resuming','cancelled')
                     AND a.workflow_id=? AND a.session_id IN (
                         SELECT session_id FROM bunshin_role_assignments
                         WHERE workflow_id=? AND aggregate_type=? AND aggregate_id=?
                     )""",
                (workflow_id, workflow_id, aggregate_type, aggregate_id),
            ).fetchall()
            for wait_id, wait_state, session_id, attempt_id, fence, status, metadata_json in rows:
                store = LogicalCoroutineCheckpointStore(self.database.runtime_root)
                try:
                    current = store.read_locked(connection, session_id)
                    parked = (open_agent_session_checkpoint(self.database.runtime_root, current)
                              ["coroutine_state"].get("parked_batch") if current else None)
                except AgentSessionCheckpointError as exc:
                    if allow_unproven:
                        continue
                    raise UnprovenInteractionOutcome("Interaction outcome has no proven closed checkpoint.") from exc
                matches_cursor = isinstance(parked, Mapping) and parked.get("wait_id") == wait_id
                # Supersession may have closed a claimed wait. Only the
                # still-selected cursor can require outcome reconciliation.
                if wait_state == "cancelled" and not matches_cursor:
                    continue
                if status not in {"lost", "failed", "cancelled", "completed"}:
                    raise ValueError("Wait for the original interaction attempt to retire before resolving triage")
                if connection.execute(
                    """SELECT 1 FROM bunshin_role_attempts p
                       JOIN bunshin_role_assignments a ON a.assignment_id=p.assignment_id
                       WHERE a.session_id=? AND p.status IN ('starting','running','submitted')""",
                    (session_id,),
                ).fetchone():
                    raise ValueError("Wait for the role session's running attempt to retire before resolving triage")
                metadata = json.loads(metadata_json or "{}")
                group = metadata.get("process_group_id")
                birth = str(metadata.get("process_group_birth") or "")
                if type(group) is int and inherited_process_group_live(group, birth):
                    raise ValueError("Wait for the original interaction worker group to exit before resolving triage")
                try:
                    payload = self._read_attempt_checkpoint(session_id, attempt_id)
                    if current is None:
                        raise AgentSessionCheckpointError("no previously selected interaction checkpoint")
                    if current != payload and not matches_cursor:
                        raise AgentSessionCheckpointError("selected checkpoint has a different interaction cursor")
                    self.role_sessions.publish_role_session_checkpoint_locked(
                        connection, session_id=session_id, fencing_token=fence,
                        checkpoint=payload, interaction=True,
                    )
                except AgentSessionCheckpointError as exc:
                    if allow_unproven:
                        continue
                    raise UnprovenInteractionOutcome("Interaction outcome has no proven closed checkpoint.") from exc
                connection.execute("UPDATE bunshin_interaction_waits SET state='consumed' WHERE wait_id=?", (wait_id,))

    def read_role_attempt_business_lease(self, attempt_id_value: str) -> dict[str, Any] | None:
        """Read actual claimed ownership, never a later aggregate/snapshot lease."""
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            return read_role_attempt_business_lease_locked(self.database, connection, str(attempt_id_value))

    def read_role_attempt(self, attempt_id_value: str) -> dict[str, Any] | None:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_role_attempts WHERE attempt_id = ?",
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
                FROM bunshin_role_attempts AS attempt
                JOIN bunshin_role_assignments AS assignment
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
                FROM bunshin_role_attempts AS attempt
                JOIN bunshin_role_assignments AS assignment
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
                UPDATE bunshin_role_attempts
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
                SELECT * FROM bunshin_role_attempts
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
            "SELECT * FROM bunshin_role_assignments WHERE assignment_id = ?",
            (str(assignment_id),),
        ).fetchone()
        if assignment is None:
            raise KeyError(f"unknown role assignment: {assignment_id}")
        attempt = connection.execute(
            """
            SELECT * FROM bunshin_role_attempts
            WHERE attempt_id = ? AND assignment_id = ?
            """,
            (str(attempt_id_value), str(assignment_id)),
        ).fetchone()
        if attempt is None or str(assignment["active_attempt_id"]) != str(
            attempt_id_value
        ):
            raise ValueError("role attempt is not active for this assignment")
        return assignment, attempt
