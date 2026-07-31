from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from pal.foundation import utc_now
from pal.minion.config import minion_db_path
from pal.minion.v2.role_contracts import RoleActivation
from pal.minion.v2.schema import ensure_minion_v2_schema


AUTHORING_CONTRACT_VERSION = "8"
ACTIVE_DRAFT_STATUS = "active"
SUBMITTED_DRAFT_STATUS = "submitted"


@dataclass(frozen=True)
class SubmissionDraftContext:
    workflow_id: str
    invocation_id: str
    lease_resource_key: str
    fencing_token: int
    role: str
    mode: str
    draft_kind: str
    input_fingerprint: str
    authoring_contract_version: str = AUTHORING_CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "invocation_id": self.invocation_id,
            "lease_resource_key": self.lease_resource_key,
            "fencing_token": self.fencing_token,
            "role": self.role,
            "mode": self.mode,
            "draft_kind": self.draft_kind,
            "input_fingerprint": self.input_fingerprint,
            "authoring_contract_version": self.authoring_contract_version,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SubmissionDraftContext":
        return cls(
            workflow_id=str(value.get("workflow_id") or "").strip(),
            invocation_id=str(value.get("invocation_id") or "").strip(),
            lease_resource_key=str(value.get("lease_resource_key") or "").strip(),
            fencing_token=int(value.get("fencing_token") or 0),
            role=str(value.get("role") or "").strip(),
            mode=str(value.get("mode") or "").strip(),
            draft_kind=str(value.get("draft_kind") or "").strip(),
            input_fingerprint=str(value.get("input_fingerprint") or "").strip(),
            authoring_contract_version=str(
                value.get("authoring_contract_version") or ""
            ).strip(),
        )

    @property
    def draft_key(self) -> str:
        return _stable_hash(
            {
                "workflow_id": self.workflow_id,
                "invocation_id": self.invocation_id,
                "fencing_token": self.fencing_token,
                "role": self.role,
                "mode": self.mode,
                "draft_kind": self.draft_kind,
                "input_fingerprint": self.input_fingerprint,
            }
        )

    @classmethod
    def from_workspace(cls, workspace: Mapping[str, Any], *, draft_kind: str) -> "SubmissionDraftContext":
        binding = dict(workspace.get("minion_v2") or {})
        context = cls(
            workflow_id=str(binding.get("workflow_id") or "").strip(),
            invocation_id=str(binding.get("invocation_id") or workspace.get("invocation_id") or "").strip(),
            lease_resource_key=str(binding.get("lease_resource_key") or binding.get("lease_resource") or "").strip(),
            fencing_token=int(binding.get("fencing_token") or 0),
            role=str(binding.get("role") or "").strip(),
            mode=str(binding.get("mode") or "").strip(),
            draft_kind=str(draft_kind or "").strip(),
            input_fingerprint=str(binding.get("authoring_input_fingerprint") or "").strip(),
            authoring_contract_version=str(
                binding.get("authoring_contract_version") or ""
            ).strip(),
        )
        missing = [
            name
            for name, value in (
                ("workflow_id", context.workflow_id),
                ("invocation_id", context.invocation_id),
                ("lease_resource_key", context.lease_resource_key),
                ("fencing_token", context.fencing_token),
                ("role", context.role),
                ("mode", context.mode),
                ("draft_kind", context.draft_kind),
                ("input_fingerprint", context.input_fingerprint),
                ("authoring_contract_version", context.authoring_contract_version),
            )
            if not value
        ]
        if missing:
            raise ValueError("submission Draft is missing bound runtime fields: " + ", ".join(missing))
        if context.authoring_contract_version != AUTHORING_CONTRACT_VERSION:
            raise ValueError(
                "role authoring contract is stale; expected "
                f"{AUTHORING_CONTRACT_VERSION}, received {context.authoring_contract_version}"
            )
        RoleActivation.from_values(context.role, context.mode)
        return context


@dataclass(frozen=True)
class SubmissionDraftSnapshot:
    draft_key: str
    version: int
    status: str
    payload: Mapping[str, Any]
    source_draft_key: str = ""
    workflow_id: str = ""
    invocation_id: str = ""
    lease_resource_key: str = ""
    fencing_token: int = 0
    role: str = ""
    mode: str = ""
    draft_kind: str = ""
    input_fingerprint: str = ""
    submission_artifact_ref: Mapping[str, Any] = field(default_factory=dict)
    submission_payload_hash: str = ""
    submitted_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_key": self.draft_key,
            "version": self.version,
            "status": self.status,
            "payload": dict(self.payload),
            "source_draft_key": self.source_draft_key,
            "workflow_id": self.workflow_id,
            "invocation_id": self.invocation_id,
            "lease_resource_key": self.lease_resource_key,
            "fencing_token": self.fencing_token,
            "role": self.role,
            "mode": self.mode,
            "draft_kind": self.draft_kind,
            "input_fingerprint": self.input_fingerprint,
            "submission_artifact_ref": dict(self.submission_artifact_ref),
            "submission_payload_hash": self.submission_payload_hash,
            "submitted_at": self.submitted_at,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SubmissionDraftSnapshot":
        return cls(
            draft_key=str(value.get("draft_key") or ""),
            version=int(value.get("version") or 0),
            status=str(value.get("status") or ""),
            payload=dict(value.get("payload") or {}),
            source_draft_key=str(value.get("source_draft_key") or ""),
            workflow_id=str(value.get("workflow_id") or ""),
            invocation_id=str(value.get("invocation_id") or ""),
            lease_resource_key=str(value.get("lease_resource_key") or ""),
            fencing_token=int(value.get("fencing_token") or 0),
            role=str(value.get("role") or ""),
            mode=str(value.get("mode") or ""),
            draft_kind=str(value.get("draft_kind") or ""),
            input_fingerprint=str(value.get("input_fingerprint") or ""),
            submission_artifact_ref=dict(value.get("submission_artifact_ref") or {}),
            submission_payload_hash=str(value.get("submission_payload_hash") or ""),
            submitted_at=str(value.get("submitted_at") or ""),
        )


DraftReducer = Callable[[dict[str, Any]], tuple[dict[str, Any], Mapping[str, Any]]]


class SubmissionDraftStore:
    """Durable role-local authoring state guarded by the active role lease fence."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = Path(runtime_root)
        self.db_path = minion_db_path(self.runtime_root)
        from pal.minion.v2.role_gateway import role_gateway_client_from_env

        self._role_gateway = role_gateway_client_from_env(self.runtime_root)

    @property
    def uses_role_gateway(self) -> bool:
        return self._role_gateway is not None

    def mutate(
        self,
        context: SubmissionDraftContext,
        *,
        operation_key: str,
        request: Mapping[str, Any],
        reducer: DraftReducer,
        seed: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self._assert_authoring_contract(context)
        operation = str(operation_key or "").strip()
        if not operation:
            raise ValueError("Draft mutation requires an operation key")
        if self._role_gateway is not None:
            snapshot = self.read(context, seed=seed)
            if snapshot.status != ACTIVE_DRAFT_STATUS:
                raise ValueError("submission Draft is already frozen; start a new fenced invocation")
            next_payload, result = reducer(_deepcopy_json(snapshot.payload))
            if not isinstance(next_payload, dict):
                raise TypeError("Draft reducer must return an object payload")
            response = self._role_gateway.request_sync(
                "draft_mutate",
                {
                    "context": context.to_dict(),
                    "operation_key": operation,
                    "request": dict(request),
                    "expected_version": snapshot.version,
                    "next_payload": next_payload,
                    "result": dict(result),
                    "seed": dict(seed or {}),
                },
            )
            return dict(response.get("result") or {})
        request_hash = _stable_hash(dict(request))
        self._ensure_schema()
        with self._transaction() as connection:
            self._assert_fence(connection, context)
            snapshot = self._read_or_create_locked(connection, context, seed=seed)
            if snapshot.status != ACTIVE_DRAFT_STATUS:
                raise ValueError("submission Draft is already frozen; start a new fenced invocation")
            duplicate = connection.execute(
                """
                SELECT request_hash, result_json
                FROM minion_v2_submission_draft_ops
                WHERE draft_key = ? AND operation_key = ?
                """,
                (context.draft_key, operation),
            ).fetchone()
            if duplicate is not None:
                if str(duplicate["request_hash"]) != request_hash:
                    raise ValueError("Draft operation key was reused with different arguments")
                return dict(json.loads(str(duplicate["result_json"])))
            next_payload, result = reducer(_deepcopy_json(snapshot.payload))
            if not isinstance(next_payload, dict):
                raise TypeError("Draft reducer must return an object payload")
            next_version = snapshot.version + 1
            updated = connection.execute(
                """
                UPDATE minion_v2_submission_drafts
                SET payload_json = ?, version = ?, updated_at = ?
                WHERE draft_key = ? AND version = ? AND status = ?
                """,
                (
                    _json(next_payload),
                    next_version,
                    utc_now(),
                    context.draft_key,
                    snapshot.version,
                    ACTIVE_DRAFT_STATUS,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("submission Draft CAS conflict")
            encoded_result = {
                **dict(result),
                "draft_version": next_version,
            }
            connection.execute(
                """
                INSERT INTO minion_v2_submission_draft_ops(
                    draft_key, operation_key, request_hash, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (context.draft_key, operation, request_hash, _json(encoded_result), utc_now()),
            )
            return encoded_result

    def mutate_precomputed(
        self,
        context: SubmissionDraftContext,
        *,
        operation_key: str,
        request: Mapping[str, Any],
        expected_version: int,
        next_payload: Mapping[str, Any],
        result: Mapping[str, Any],
        seed: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """CAS a reducer result computed by an assignment-scoped role invocation.

        The Manager still owns idempotency, fencing and the durable mutation;
        only the pure reducer runs in the sandbox process.
        """

        self._assert_authoring_contract(context)
        operation = str(operation_key or "").strip()
        if not operation:
            raise ValueError("Draft mutation requires an operation key")
        if not isinstance(next_payload, Mapping):
            raise TypeError("Draft reducer must return an object payload")
        request_hash = _stable_hash(dict(request))
        self._ensure_schema()
        with self._transaction() as connection:
            self._assert_fence(connection, context)
            snapshot = self._read_or_create_locked(connection, context, seed=seed)
            duplicate = connection.execute(
                """
                SELECT request_hash, result_json
                FROM minion_v2_submission_draft_ops
                WHERE draft_key = ? AND operation_key = ?
                """,
                (context.draft_key, operation),
            ).fetchone()
            if duplicate is not None:
                if str(duplicate["request_hash"]) != request_hash:
                    raise ValueError("Draft operation key was reused with different arguments")
                return dict(json.loads(str(duplicate["result_json"])))
            if snapshot.status != ACTIVE_DRAFT_STATUS:
                raise ValueError("submission Draft is already frozen; start a new fenced invocation")
            if snapshot.version != int(expected_version):
                raise RuntimeError("submission Draft CAS conflict")
            next_version = snapshot.version + 1
            updated = connection.execute(
                """
                UPDATE minion_v2_submission_drafts
                SET payload_json = ?, version = ?, updated_at = ?
                WHERE draft_key = ? AND version = ? AND status = ?
                """,
                (
                    _json(dict(next_payload)),
                    next_version,
                    utc_now(),
                    context.draft_key,
                    snapshot.version,
                    ACTIVE_DRAFT_STATUS,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("submission Draft CAS conflict")
            encoded_result = {**dict(result), "draft_version": next_version}
            connection.execute(
                """
                INSERT INTO minion_v2_submission_draft_ops(
                    draft_key, operation_key, request_hash, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    context.draft_key,
                    operation,
                    request_hash,
                    _json(encoded_result),
                    utc_now(),
                ),
            )
            return encoded_result

    def read(
        self,
        context: SubmissionDraftContext,
        *,
        seed: Mapping[str, Any] | None = None,
    ) -> SubmissionDraftSnapshot:
        self._assert_authoring_contract(context)
        if self._role_gateway is not None:
            from pal.minion.v2.role_gateway import decode_remote_draft_snapshot

            return decode_remote_draft_snapshot(
                self._role_gateway.request_sync(
                    "draft_read",
                    {"context": context.to_dict(), "seed": dict(seed or {})},
                )
            )
        self._ensure_schema()
        with self._transaction() as connection:
            self._assert_fence(connection, context)
            return self._read_or_create_locked(connection, context, seed=seed)

    def mark_submitted(
        self,
        context: SubmissionDraftContext,
        *,
        expected_version: int,
        submission_artifact_ref: Mapping[str, Any] | None = None,
        submission_payload_hash: str = "",
        submission_payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self._assert_authoring_contract(context)
        if self._role_gateway is not None:
            if not isinstance(submission_payload, Mapping):
                raise ValueError("remote submission requires its compiled JSON payload")
            return dict(self._role_gateway.request_sync(
                "draft_submit",
                {
                    "context": context.to_dict(),
                    "expected_version": int(expected_version),
                    "submission": dict(submission_payload),
                },
            ))
        self._ensure_schema()
        artifact_ref = dict(submission_artifact_ref or {})
        payload_hash = str(submission_payload_hash or "")
        if bool(artifact_ref) != bool(payload_hash):
            raise ValueError("submission receipt requires both artifact ref and payload hash")
        submitted_at = utc_now()
        with self._transaction() as connection:
            self._assert_fence(connection, context)
            self._assert_submission_artifact_locked(connection, artifact_ref)
            updated = connection.execute(
                """
                UPDATE minion_v2_submission_drafts
                SET status = ?, submitted_artifact_ref_json = ?,
                    submission_payload_hash = ?, submitted_at = ?, updated_at = ?
                WHERE draft_key = ? AND version = ? AND status = ?
                """,
                (
                    SUBMITTED_DRAFT_STATUS,
                    _json(artifact_ref),
                    payload_hash,
                    submitted_at,
                    submitted_at,
                    context.draft_key,
                    int(expected_version),
                    ACTIVE_DRAFT_STATUS,
                ),
            )
            if updated.rowcount != 1:
                row = connection.execute(
                    """
                    SELECT version, status, submitted_artifact_ref_json,
                           submission_payload_hash
                    FROM minion_v2_submission_drafts WHERE draft_key = ?
                    """,
                    (context.draft_key,),
                ).fetchone()
                if row is not None and int(row["version"]) == int(expected_version) and str(row["status"]) == SUBMITTED_DRAFT_STATUS:
                    existing_ref = dict(json.loads(str(row["submitted_artifact_ref_json"] or "{}")))
                    existing_hash = str(row["submission_payload_hash"] or "")
                    if artifact_ref and (existing_ref != artifact_ref or existing_hash != payload_hash):
                        raise RuntimeError("submission Draft receipt changed after freeze")
                    return {
                        "submitted": True,
                        "submission_artifact_ref": existing_ref,
                        "submission_payload_hash": existing_hash,
                    }
                raise RuntimeError("submission Draft changed before freeze")
        return {
            "submitted": True,
            "submission_artifact_ref": artifact_ref,
            "submission_payload_hash": payload_hash,
        }

    def read_submitted(self, draft_key: str) -> SubmissionDraftSnapshot:
        """Read an immutable submission receipt without requiring the expired worker lease."""

        self._ensure_schema()
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM minion_v2_submission_drafts WHERE draft_key = ?",
                (str(draft_key),),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown submission Draft: {draft_key}")
        snapshot = _snapshot_from_row(row)
        if snapshot.status != SUBMITTED_DRAFT_STATUS:
            raise ValueError("submission Draft is not submitted")
        if not snapshot.submission_artifact_ref or not snapshot.submission_payload_hash:
            raise ValueError("submitted Draft has no durable submission receipt")
        return snapshot

    def latest_submitted(
        self,
        *,
        workflow_id: str,
        invocation_id: str,
        role: str,
        mode: str,
        draft_kind: str,
        input_fingerprint: str,
    ) -> SubmissionDraftSnapshot | None:
        """Resolve the newest exact-input receipt for one logical role invocation."""

        self._ensure_schema()
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT * FROM minion_v2_submission_drafts
                WHERE workflow_id = ? AND invocation_id = ? AND role = ? AND mode = ?
                  AND draft_kind = ? AND input_fingerprint = ?
                  AND authoring_contract_version = ? AND status = ?
                  AND submitted_artifact_ref_json != '{}'
                  AND submission_payload_hash != ''
                ORDER BY submitted_at DESC, updated_at DESC
                LIMIT 1
                """,
                (
                    str(workflow_id),
                    str(invocation_id),
                    str(role),
                    str(mode),
                    str(draft_kind),
                    str(input_fingerprint),
                    AUTHORING_CONTRACT_VERSION,
                    SUBMITTED_DRAFT_STATUS,
                ),
            ).fetchone()
        return _snapshot_from_row(row) if row is not None else None

    def submitted_for_invocation(
        self,
        *,
        workflow_id: str,
        invocation_id: str,
        role: str,
        mode: str,
        draft_kind: str,
        input_fingerprint: str,
    ) -> SubmissionDraftSnapshot | None:
        """Resolve a completed invocation receipt across contract upgrades.

        This is a recovery lookup, not a compatibility decision. The caller
        must bind the returned Draft to the durable assignment receipt before
        consuming its role-local state.
        """

        self._ensure_schema()
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT * FROM minion_v2_submission_drafts
                WHERE workflow_id = ? AND invocation_id = ? AND role = ? AND mode = ?
                  AND draft_kind = ? AND input_fingerprint = ?
                  AND status = ?
                  AND submitted_artifact_ref_json != '{}'
                  AND submission_payload_hash != ''
                ORDER BY submitted_at DESC, updated_at DESC
                LIMIT 1
                """,
                (
                    str(workflow_id),
                    str(invocation_id),
                    str(role),
                    str(mode),
                    str(draft_kind),
                    str(input_fingerprint),
                    SUBMITTED_DRAFT_STATUS,
                ),
            ).fetchone()
        return _snapshot_from_row(row) if row is not None else None

    def attach_submission_artifact(
        self,
        draft_key: str,
        *,
        submission_artifact_ref: Mapping[str, Any],
        submission_payload_hash: str,
    ) -> SubmissionDraftSnapshot:
        """Backfill a receipt for a legacy submitted Draft after validating its local artifact."""

        artifact_ref = dict(submission_artifact_ref or {})
        payload_hash = str(submission_payload_hash or "")
        if not artifact_ref or not payload_hash:
            raise ValueError("receipt backfill requires artifact ref and payload hash")
        self._ensure_schema()
        with self._transaction() as connection:
            self._assert_submission_artifact_locked(connection, artifact_ref)
            row = connection.execute(
                "SELECT * FROM minion_v2_submission_drafts WHERE draft_key = ?",
                (str(draft_key),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown submission Draft: {draft_key}")
            snapshot = _snapshot_from_row(row)
            if snapshot.status != SUBMITTED_DRAFT_STATUS:
                raise ValueError("only a submitted Draft may receive a receipt backfill")
            if snapshot.submission_artifact_ref:
                if (
                    dict(snapshot.submission_artifact_ref) != artifact_ref
                    or snapshot.submission_payload_hash != payload_hash
                ):
                    raise ValueError("submitted Draft already has a different receipt")
                return snapshot
            now = utc_now()
            connection.execute(
                """
                UPDATE minion_v2_submission_drafts
                SET submitted_artifact_ref_json = ?, submission_payload_hash = ?,
                    submitted_at = ?, updated_at = ?
                WHERE draft_key = ? AND status = ?
                """,
                (
                    _json(artifact_ref),
                    payload_hash,
                    now,
                    now,
                    str(draft_key),
                    SUBMITTED_DRAFT_STATUS,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM minion_v2_submission_drafts WHERE draft_key = ?",
                (str(draft_key),),
            ).fetchone()
            return _snapshot_from_row(updated)

    @staticmethod
    def _assert_submission_artifact_locked(
        connection: sqlite3.Connection,
        artifact_ref: Mapping[str, Any],
    ) -> None:
        if not artifact_ref:
            return
        digest = str(artifact_ref.get("sha256") or "")
        if not digest:
            raise ValueError("submission receipt artifact ref requires sha256")
        row = connection.execute(
            "SELECT durable, storage_path FROM minion_v2_artifacts WHERE sha256 = ?",
            (digest,),
        ).fetchone()
        if row is None or int(row["durable"] or 0) != 1:
            raise ValueError("submission receipt must reference a durable artifact")
        if not Path(str(row["storage_path"] or "")).is_file():
            raise ValueError("submission receipt artifact storage is unavailable")

    def _read_or_create_locked(
        self,
        connection: sqlite3.Connection,
        context: SubmissionDraftContext,
        *,
        seed: Mapping[str, Any] | None,
    ) -> SubmissionDraftSnapshot:
        row = connection.execute(
            "SELECT * FROM minion_v2_submission_drafts WHERE draft_key = ?",
            (context.draft_key,),
        ).fetchone()
        if row is None:
            inherited, source_key = self._inherited_payload_locked(connection, context)
            payload = _deepcopy_json(inherited if source_key else (seed or {}))
            now = utc_now()
            connection.execute(
                """
                INSERT INTO minion_v2_submission_drafts(
                    draft_key, workflow_id, invocation_id, lease_resource_key,
                    fencing_token, role, mode, draft_kind, input_fingerprint,
                    authoring_contract_version, version, status, payload_json,
                    source_draft_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                """,
                (
                    context.draft_key,
                    context.workflow_id,
                    context.invocation_id,
                    context.lease_resource_key,
                    context.fencing_token,
                    context.role,
                    context.mode,
                    context.draft_kind,
                    context.input_fingerprint,
                    context.authoring_contract_version,
                    ACTIVE_DRAFT_STATUS,
                    _json(payload),
                    source_key,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM minion_v2_submission_drafts WHERE draft_key = ?",
                (context.draft_key,),
            ).fetchone()
        return _snapshot_from_row(row)

    def _inherited_payload_locked(
        self,
        connection: sqlite3.Connection,
        context: SubmissionDraftContext,
    ) -> tuple[dict[str, Any], str]:
        rows = connection.execute(
            """
            SELECT draft_key, invocation_id, lease_resource_key, fencing_token,
                   payload_json
            FROM minion_v2_submission_drafts
            WHERE workflow_id = ? AND role = ? AND mode = ? AND draft_kind = ?
              AND input_fingerprint = ? AND authoring_contract_version = ?
              AND draft_key != ?
            ORDER BY updated_at DESC
            LIMIT 16
            """,
            (
                context.workflow_id,
                context.role,
                context.mode,
                context.draft_kind,
                context.input_fingerprint,
                context.authoring_contract_version,
                context.draft_key,
            ),
        ).fetchall()
        for row in rows:
            if self._draft_worker_is_live_locked(connection, row):
                continue
            payload = dict(json.loads(str(row["payload_json"])))
            return _inherited_payload(context.draft_kind, payload), str(row["draft_key"])
        return {}, ""

    @staticmethod
    def _draft_worker_is_live_locked(
        connection: sqlite3.Connection,
        draft_row: sqlite3.Row,
    ) -> bool:
        lease = connection.execute(
            """
            SELECT owner_id, fencing_token, expires_at
            FROM minion_v2_leases
            WHERE resource_key = ?
            """,
            (str(draft_row["lease_resource_key"]),),
        ).fetchone()
        if lease is None:
            return False
        if str(lease["owner_id"]) != str(draft_row["invocation_id"]):
            return False
        if int(lease["fencing_token"]) != int(draft_row["fencing_token"]):
            return False
        return _parse_datetime(str(lease["expires_at"] or "")) > datetime.now(timezone.utc)

    @staticmethod
    def _assert_authoring_contract(context: SubmissionDraftContext) -> None:
        if context.authoring_contract_version != AUTHORING_CONTRACT_VERSION:
            raise ValueError(
                "role authoring contract is stale; expected "
                f"{AUTHORING_CONTRACT_VERSION}, received "
                f"{context.authoring_contract_version or '<missing>'}"
            )
        RoleActivation.from_values(context.role, context.mode)

    def _assert_fence(self, connection: sqlite3.Connection, context: SubmissionDraftContext) -> None:
        row = connection.execute(
            "SELECT owner_id, fencing_token, expires_at FROM minion_v2_leases WHERE resource_key = ?",
            (context.lease_resource_key,),
        ).fetchone()
        if row is None:
            raise ValueError("submission Draft lease does not exist")
        if str(row["owner_id"]) != context.invocation_id or int(row["fencing_token"]) != context.fencing_token:
            raise ValueError("submission Draft write rejected by stale fencing token")
        expires_at = _parse_datetime(str(row["expires_at"] or ""))
        if expires_at <= datetime.now(timezone.utc):
            raise ValueError("submission Draft write rejected because the worker lease expired")

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            ensure_minion_v2_schema(connection)

    def _transaction(self):
        return _SqliteTransaction(self.db_path)


class _SqliteTransaction:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        self.connection = connection
        return connection

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        assert self.connection is not None
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()


def authoring_input_fingerprint(value: Mapping[str, Any]) -> str:
    return _stable_hash(dict(value))


def _inherited_payload(draft_kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if draft_kind == "work_items":
        return {
            "items": _deepcopy_json(payload.get("items") or []),
        }
    definitions = _deepcopy_json(payload.get("definitions") or {})
    if draft_kind not in {"verification", "standalone_review"}:
        return {
            "definitions": definitions,
            "evidence": {},
            "findings": [],
            "summary": {},
        }
    return {
        "definitions": definitions,
        "evidence": _deepcopy_json(payload.get("evidence") or {}),
        "findings": _deepcopy_json(payload.get("findings") or []),
        "summary": _deepcopy_json(payload.get("summary") or {}),
    }


def assert_authoring_schema_budget(schema: Mapping[str, Any], *, owner: str) -> None:
    """Reject tool schemas that make the model maintain a nested document compiler."""

    properties = dict(schema.get("properties") or {})
    if len(properties) > 12:
        raise ValueError(f"{owner} has {len(properties)} top-level properties; maximum is 12")

    def visit(node: Any, *, depth: int) -> None:
        if depth > 4:
            raise ValueError(f"{owner} schema depth exceeds 4")
        if not isinstance(node, Mapping):
            return
        if "oneOf" in node:
            raise ValueError(f"{owner} may not use oneOf")
        if "anyOf" in node:
            variants = list(node.get("anyOf") or [])
            non_null = [item for item in variants if not (isinstance(item, Mapping) and item.get("type") == "null")]
            if len(variants) != 2 or len(non_null) != 1:
                raise ValueError(f"{owner} may only use Pydantic nullable anyOf schemas")
            visit(non_null[0], depth=depth)
        additional = node.get("additionalProperties")
        if isinstance(additional, Mapping):
            raise ValueError(f"{owner} may not use schema-valued additionalProperties")
        if node.get("type") == "array":
            items = node.get("items")
            if isinstance(items, Mapping) and items.get("type") == "object":
                raise ValueError(f"{owner} may not use arrays of objects")
        for key in ("properties",):
            for property_name, child in dict(node.get(key) or {}).items():
                normalized_name = str(property_name).casefold()
                if (
                    normalized_name in {
                        "handle",
                        "refs",
                        "json_pointer",
                        "artifact_sha",
                        "input_read",
                    }
                    or normalized_name.endswith("_id")
                    or normalized_name.endswith("_ref")
                    or normalized_name.endswith("_sha")
                ):
                    raise ValueError(
                        f"{owner} exposes Manager-owned identity field {property_name}"
                    )
                visit(child, depth=depth + 1)
        if isinstance(node.get("items"), Mapping):
            visit(node["items"], depth=depth + 1)

    visit(schema, depth=1)


def _snapshot_from_row(row: sqlite3.Row) -> SubmissionDraftSnapshot:
    artifact_ref = dict(json.loads(str(row["submitted_artifact_ref_json"] or "{}")))
    return SubmissionDraftSnapshot(
        draft_key=str(row["draft_key"]),
        version=int(row["version"]),
        status=str(row["status"]),
        payload=dict(json.loads(str(row["payload_json"]))),
        source_draft_key=str(row["source_draft_key"] or ""),
        workflow_id=str(row["workflow_id"] or ""),
        invocation_id=str(row["invocation_id"] or ""),
        lease_resource_key=str(row["lease_resource_key"] or ""),
        fencing_token=int(row["fencing_token"] or 0),
        role=str(row["role"] or ""),
        mode=str(row["mode"] or ""),
        draft_kind=str(row["draft_kind"] or ""),
        input_fingerprint=str(row["input_fingerprint"] or ""),
        submission_artifact_ref=artifact_ref,
        submission_payload_hash=str(row["submission_payload_hash"] or ""),
        submitted_at=str(row["submitted_at"] or ""),
    )


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _deepcopy_json(value: Any) -> Any:
    return json.loads(_json(value))


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
