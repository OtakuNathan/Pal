from __future__ import annotations
from pal.bunshin.storage.serialization import _decode_role_session
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping
from pal.foundation import utc_now
from pal.bunshin.checkpoint import AgentSessionCheckpointError, LogicalCoroutineCheckpointStore, normalize_agent_session_checkpoint
from pal.bunshin.contracts import AggregateType
from pal.bunshin.paths import cleanup_role_runtime
from pal.bunshin.role_protocol import RoleAssignmentState, RoleSessionAction, RoleSessionState, canonical_role_profile_parts, role_session_target
from pal.bunshin.role_contracts import RoleActivation, role_session_stage_key
from pal.bunshin.storage.connection_contracts import DatabasePort
from pal.bunshin.storage.projections import ProjectionsStore
from pal.bunshin.storage.role_session_checks import RoleSessionChecksStore
from pal.bunshin.storage.snapshots import SnapshotsStore


@dataclass
class RoleSessionsStore:
    database: DatabasePort
    projections: ProjectionsStore
    role_session_checks: RoleSessionChecksStore
    snapshots: SnapshotsStore

    def ensure_role_session(
        self,
        *,
        session_id: str,
        workflow_id: str,
        aggregate_type: AggregateType,
        aggregate_id: str,
        role: str,
        mode: str,
        role_profile_id: str,
        family_binding_sha: str,
        preferred_harness_id: str = "pal",
        preferred_harness_generation: str = "",
        scope_kind: str = "",
        subject_key: str = "",
    ) -> dict[str, Any]:
        values = {
            "session_id": str(session_id or "").strip(),
            "workflow_id": str(workflow_id or "").strip(),
            "aggregate_id": str(aggregate_id or "").strip(),
            "role": str(role or "").strip(),
            "mode": str(mode or "").strip(),
            "role_profile_id": str(role_profile_id or "").strip(),
            "family_binding_sha": str(family_binding_sha or "").strip(),
            "preferred_harness_id": str(
                preferred_harness_id or "pal"
            ).strip(),
            "preferred_harness_generation": str(
                preferred_harness_generation or ""
            ).strip(),
            "scope_kind": str(scope_kind or "").strip(),
            "subject_key": str(subject_key or "").strip(),
        }
        missing = [
            name
            for name in (
                "session_id",
                "workflow_id",
                "aggregate_id",
                "role",
                "mode",
                "role_profile_id",
                "family_binding_sha",
                "scope_kind",
                "subject_key",
            )
            if not values[name]
        ]
        if missing:
            raise ValueError("role session missing fields: " + ", ".join(missing))
        RoleActivation.from_values(values["role"], values["mode"])
        canonical_role_profile_parts(values["role_profile_id"])
        self.database.ensure_schema()
        now = utc_now()
        with self.database.write_connection() as connection:
            existing = connection.execute(
                "SELECT * FROM bunshin_v2_role_sessions WHERE session_id = ?",
                (values["session_id"],),
            ).fetchone()
            identity = (
                values["workflow_id"],
                values["role"],
                values["role_profile_id"],
                values["family_binding_sha"],
                values["scope_kind"],
                values["subject_key"],
            )
            if existing is not None:
                actual = (
                    str(existing["workflow_id"]),
                    str(existing["role"]),
                    str(existing["role_profile_id"]),
                    str(existing["family_binding_sha"]),
                    str(existing["scope_kind"] or ""),
                    str(existing["subject_key"] or ""),
                )
                if actual != identity:
                    raise ValueError("role session identity is immutable")
                if str(existing["mode"] or "") != values["mode"]:
                    connection.execute(
                        """
                        UPDATE bunshin_v2_role_sessions
                        SET mode = ?, updated_at = ?
                        WHERE session_id = ?
                        """,
                        (values["mode"], now, values["session_id"]),
                    )
                    existing = connection.execute(
                        "SELECT * FROM bunshin_v2_role_sessions WHERE session_id = ?",
                        (values["session_id"],),
                    ).fetchone()
                return _decode_role_session(existing)
            connection.execute(
                """
                INSERT INTO bunshin_v2_role_sessions(
                    session_id, workflow_id, aggregate_type, aggregate_id, role, mode,
                    role_profile_id, preferred_harness_id,
                    preferred_harness_generation, family_binding_sha,
                    scope_kind, subject_key, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["session_id"],
                    values["workflow_id"],
                    aggregate_type.value,
                    values["aggregate_id"],
                    values["role"],
                    values["mode"],
                    values["role_profile_id"],
                    values["preferred_harness_id"],
                    values["preferred_harness_generation"],
                    values["family_binding_sha"],
                    values["scope_kind"],
                    values["subject_key"],
                    RoleSessionState.UNINITIALIZED.value,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM bunshin_v2_role_sessions WHERE session_id = ?",
                (values["session_id"],),
            ).fetchone()
            return _decode_role_session(row)

    def publish_role_session_checkpoint_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        fencing_token: int,
        checkpoint: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist the file before acknowledging that fresh admission is over.

        A crash between the file replace and database commit leaves an
        uninitialized session with a valid checkpoint; preparation restores it.
        An acknowledged worker may do semantic work only after both succeed.
        """
        session = connection.execute(
            "SELECT * FROM bunshin_v2_role_sessions WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
        if session is None:
            raise AgentSessionCheckpointError("worker checkpoint has no durable session")
        # Check lifecycle before touching the durable file.
        role_session_target(str(session["status"]), RoleSessionAction.INITIALIZE)
        payload = normalize_agent_session_checkpoint(checkpoint)
        expected = {
            "logical_coroutine_id": str(session_id),
            "workflow_id": str(session["workflow_id"]),
            "stage_key": role_session_stage_key(
                str(session["scope_kind"]), str(session["subject_key"]), str(session["role"]),
            ),
            "producer_fencing_token": int(fencing_token),
        }
        for key, value in expected.items():
            if payload[key] != value:
                raise AgentSessionCheckpointError(f"worker checkpoint has the wrong {key}")
        store = LogicalCoroutineCheckpointStore(self.database.runtime_root)
        # The first safe point is acknowledged while the worker is alive and
        # can be observed again at process retirement or after a lost ACK.
        # Only an exact replay is idempotent; equal-sequence mutations fail.
        if store.read(session_id) != payload:
            store.publish(
                payload,
                expected_logical_coroutine_id=session_id,
                current_fencing_token=fencing_token,
            )
        self.transition_role_session_locked(
            connection, session_id, RoleSessionAction.INITIALIZE, now=utc_now(),
        )
        return payload

    def read_role_session(self, session_id: str) -> dict[str, Any] | None:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_role_sessions WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
        return _decode_role_session(row) if row is not None else None

    def list_role_sessions(
        self,
        *,
        workflow_id: str,
        aggregate_type: AggregateType | str,
        aggregate_id: str,
        role: str = "",
    ) -> tuple[dict[str, Any], ...]:
        self.database.ensure_schema()
        clauses = ["workflow_id = ?", "aggregate_type = ?", "aggregate_id = ?"]
        parameters: list[Any] = [
            str(workflow_id),
            AggregateType(str(aggregate_type)).value,
            str(aggregate_id),
        ]
        if str(role or "").strip():
            clauses.append("role = ?")
            parameters.append(str(role))
        with self.database.read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM bunshin_v2_role_sessions WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at, session_id",
                tuple(parameters),
            ).fetchall()
        return tuple(_decode_role_session(row) for row in rows)

    def complete_role_session(self, session_id: str, *, status: str = "completed") -> bool:
        normalized = str(status or "completed").strip().lower()
        if normalized not in {"completed", "cancelled"}:
            raise ValueError("role session completion status must be completed or cancelled")
        self.database.ensure_schema()
        completed = False
        with self.database.write_connection() as connection:
            invocation = connection.execute(
                "SELECT * FROM bunshin_v2_role_invocations WHERE invocation_id = ?",
                (str(session_id),),
            ).fetchone()
            session = connection.execute(
                "SELECT * FROM bunshin_v2_role_sessions WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
            if invocation is None and session is None:
                completed = False
            elif (
                (invocation is None or str(invocation["status"]) == normalized)
                and (session is None or str(session["status"]) == normalized)
            ):
                completed = True
            else:
                owner = session if session is not None else invocation
                assignments = connection.execute(
                    "SELECT state FROM bunshin_v2_role_assignments WHERE session_id = ?",
                    (str(session_id),),
                ).fetchall()
                states = {str(row["state"]) for row in assignments}
                if states - {
                    RoleAssignmentState.SETTLED.value,
                    RoleAssignmentState.CANCELLED.value,
                }:
                    raise ValueError("role session cannot complete with a non-terminal assignment")
                if session is not None:
                    self.role_session_checks.assert_role_session_scope_terminal_locked(
                        connection,
                        session,
                        cancelled=normalized == RoleSessionState.CANCELLED.value,
                    )
                else:
                    snapshot = self.snapshots.read_snapshot_locked(
                        connection,
                        AggregateType(str(owner["aggregate_type"])),
                        str(owner["aggregate_id"]),
                    )
                    if snapshot is None or snapshot.state not in {
                        "ACCEPTED",
                        "REJECTED",
                        "CANCELLED",
                    }:
                        raise ValueError(
                            "legacy role invocation cannot complete before its aggregate is terminal"
                        )
                now = utc_now()
                if invocation is not None:
                    connection.execute(
                        "UPDATE bunshin_v2_role_invocations SET status = ?, updated_at = ? WHERE invocation_id = ?",
                        (normalized, now, str(session_id)),
                    )
                if session is not None:
                    self.transition_role_session_locked(
                        connection,
                        str(session_id),
                        (
                            RoleSessionAction.COMPLETE
                            if normalized == RoleSessionState.COMPLETED.value
                            else RoleSessionAction.CANCEL
                        ),
                        now=now,
                    )
                self.projections.rebuild_workflow_projection_locked(connection, str(owner["workflow_id"]), "")
                completed = True
        # The database transition is the durable authority. Delete the
        # encrypted worker payload only after COMMIT succeeds, so a failed
        # transition cannot strand an otherwise resumable coroutine.
        if completed:
            LogicalCoroutineCheckpointStore(self.database.runtime_root).delete(
                str(session_id)
            )
            cleanup_role_runtime(
                self.database.runtime_root,
                invocation_id=str(session_id),
            )
        return completed

    @staticmethod
    def transition_role_session_locked(
        connection: sqlite3.Connection,
        session_id: str,
        action: RoleSessionAction,
        *,
        now: str,
    ) -> RoleSessionState:
        session = connection.execute(
            "SELECT status FROM bunshin_v2_role_sessions WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
        if session is None:
            raise KeyError(f"unknown role session: {session_id}")
        target = role_session_target(str(session["status"]), action)
        connection.execute(
            "UPDATE bunshin_v2_role_sessions SET status = ?, updated_at = ? WHERE session_id = ?",
            (target.value, str(now), str(session_id)),
        )
        return target
