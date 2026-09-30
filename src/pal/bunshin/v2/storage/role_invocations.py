from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _json
import json
from dataclasses import dataclass
from typing import Any, Mapping
from pal.foundation import utc_now
from pal.bunshin.v2.contracts import AggregateType
from pal.bunshin.v2.role_protocol import RoleSessionAction
from pal.bunshin.v2.storage.artifacts import ArtifactsStore
from pal.bunshin.v2.storage.connection_contracts import DatabasePort
from pal.bunshin.v2.storage.leases import LeasesStore
from pal.bunshin.v2.storage.projections import ProjectionsStore
from pal.bunshin.v2.storage.role_sessions import RoleSessionsStore


@dataclass
class RoleInvocationsStore:
    artifacts: ArtifactsStore
    database: DatabasePort
    leases: LeasesStore
    projections: ProjectionsStore
    role_sessions: RoleSessionsStore

    def record_role_invocation(
        self,
        *,
        invocation_id: str,
        workflow_id: str,
        aggregate_type: AggregateType,
        aggregate_id: str,
        lease_resource_key: str,
        fencing_token: int,
        role: str,
        mode: str,
        role_profile_id: str,
        harness_id: str = "pal",
        harness_generation: str = "",
        family_binding_sha: str,
        authoring_contract_version: str,
        prompt_pack_ref: Mapping[str, Any],
    ) -> None:
        self.leases.assert_fencing_token(lease_resource_key, invocation_id, fencing_token)
        self.database.ensure_schema()
        now = utc_now()
        with self.database.write_connection() as connection:
            self.artifacts.assert_artifact_refs_durable(connection, prompt_pack_ref)
            connection.execute(
                """
                INSERT INTO bunshin_v2_role_invocations(
                    invocation_id, workflow_id, aggregate_type, aggregate_id, lease_resource_key,
                    fencing_token, role, mode, role_profile_id, harness_id,
                    harness_generation, family_binding_sha,
                    authoring_contract_version, prompt_pack_ref_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                ON CONFLICT(invocation_id) DO UPDATE SET
                    workflow_id = excluded.workflow_id,
                    aggregate_type = excluded.aggregate_type,
                    aggregate_id = excluded.aggregate_id,
                    lease_resource_key = excluded.lease_resource_key,
                    fencing_token = excluded.fencing_token,
                    role = excluded.role,
                    mode = excluded.mode,
                    role_profile_id = excluded.role_profile_id,
                    harness_id = excluded.harness_id,
                    harness_generation = excluded.harness_generation,
                    family_binding_sha = excluded.family_binding_sha,
                    authoring_contract_version = excluded.authoring_contract_version,
                    prompt_pack_ref_json = excluded.prompt_pack_ref_json,
                    status = 'running',
                    updated_at = excluded.updated_at
                """,
                (
                    invocation_id,
                    workflow_id,
                    aggregate_type.value,
                    aggregate_id,
                    lease_resource_key,
                    fencing_token,
                    role,
                    mode,
                    role_profile_id,
                    str(harness_id or "pal"),
                    str(harness_generation or ""),
                    family_binding_sha,
                    str(authoring_contract_version),
                    _json(dict(prompt_pack_ref)),
                    now,
                    now,
                ),
            )

    def read_role_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_role_invocations WHERE invocation_id = ?",
                (str(invocation_id),),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        raw = str(value.pop("prompt_pack_ref_json", "{}") or "{}")
        value["prompt_pack_ref"] = json.loads(raw)
        return value

    def suspend_role_invocation(
        self,
        *,
        invocation_id: str,
        fencing_token: int,
        status: str = "suspended",
    ) -> None:
        normalized = str(status or "suspended").strip().lower()
        if normalized not in {"suspended", "interrupted"}:
            raise ValueError("worker suspension status must be suspended or interrupted")
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            invocation = connection.execute(
                "SELECT * FROM bunshin_v2_role_invocations WHERE invocation_id = ?",
                (str(invocation_id),),
            ).fetchone()
            if invocation is None:
                raise KeyError(f"unknown role invocation: {invocation_id}")
            self.leases.assert_lease_locked(
                connection,
                str(invocation["lease_resource_key"]),
                str(invocation_id),
                int(fencing_token),
            )
            now = utc_now()
            connection.execute(
                """
                UPDATE bunshin_v2_role_invocations
                SET status = ?, updated_at = ?
                WHERE invocation_id = ?
                """,
                (normalized, now, str(invocation_id)),
            )
            self.role_sessions.transition_role_session_locked(
                connection,
                str(invocation_id),
                RoleSessionAction.SUSPEND,
                now=now,
            )
            self.projections.rebuild_workflow_projection_locked(connection, str(invocation["workflow_id"]), "")

    def finish_role_invocation(self, *, invocation_id: str, fencing_token: int, status: str) -> None:
        normalized = str(status or "").strip().lower()
        if normalized not in {"completed", "failed", "cancelled"}:
            raise ValueError(f"invalid role invocation terminal status: {status}")
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            invocation = connection.execute(
                "SELECT * FROM bunshin_v2_role_invocations WHERE invocation_id = ?",
                (str(invocation_id),),
            ).fetchone()
            if invocation is None:
                raise KeyError(f"unknown role invocation: {invocation_id}")
            self.leases.assert_lease_locked(
                connection,
                str(invocation["lease_resource_key"]),
                str(invocation_id),
                int(fencing_token),
            )
            connection.execute(
                "UPDATE bunshin_v2_role_invocations SET status = ?, updated_at = ? WHERE invocation_id = ?",
                (normalized, utc_now(), str(invocation_id)),
            )
            self.projections.rebuild_workflow_projection_locked(connection, str(invocation["workflow_id"]), "")
