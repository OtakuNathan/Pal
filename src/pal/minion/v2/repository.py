from __future__ import annotations

import hashlib
import json
import sqlite3
import secrets
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

from pal.foundation import utc_now
from pal.minion.config import minion_db_path
from pal.minion.v2.artifacts import ArtifactRef
from pal.minion.v2.contracts import (
    ActionEnvelope,
    AggregateSnapshot,
    AggregateType,
    AggregateVersionConflict,
    DispatchResult,
    DomainEvent,
    LeaseConflict,
    LeaseGrant,
    StaleFencingToken,
)
from pal.minion.v2.engine import TransitionEngine
from pal.minion.v2.machines import build_default_transition_engine
from pal.minion.v2.schema import ensure_minion_v2_schema


_QUEUED_STATES = {
    "ARCHITECT_QUEUED",
    "REVIEW_QUEUED",
    "QUEUED",
    "REPAIR_QUEUED",
    "STARTING",
}
_HUMAN_WAIT_STATES = {"HUMAN_REVIEW", "CLARIFICATION_PENDING"}
_TERMINAL_WORKFLOW_STATES = {"COMPLETED", "REJECTED", "CANCELLED"}


@dataclass
class MinionV2Repository:
    runtime_root: Path
    engine: TransitionEngine = field(default_factory=build_default_transition_engine)

    @property
    def db_path(self) -> Path:
        return minion_db_path(Path(self.runtime_root))

    def ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            ensure_minion_v2_schema(connection)

    def dispatch(self, action: ActionEnvelope) -> DispatchResult:
        self.ensure_schema()
        request_hash = _stable_hash(_action_request_payload(action))
        with self._transaction() as connection:
            duplicate = connection.execute(
                """
                SELECT request_hash, result_json
                FROM minion_v2_action_dedup
                WHERE aggregate_type = ? AND aggregate_id = ? AND idempotency_key = ?
                """,
                (action.aggregate_type.value, action.aggregate_id, action.dedup_key),
            ).fetchone()
            if duplicate is not None:
                if str(duplicate["request_hash"]) != request_hash:
                    raise ValueError("idempotency key was reused with a different action request")
                return _decode_dispatch_result(json.loads(str(duplicate["result_json"])), duplicate=True)

            self._consume_human_decision_locked(connection, action)
            self._assert_artifact_refs_durable(connection, action.payload)
            current = self._read_snapshot_locked(connection, action.aggregate_type, action.aggregate_id)
            outcome = self.engine.transition(current, action)
            self._write_snapshot_locked(connection, current, outcome.snapshot)

            events: list[DomainEvent] = []
            for draft in outcome.events:
                event = DomainEvent(
                    event_id=f"evt_{uuid4().hex}",
                    workflow_id=action.workflow_id,
                    aggregate_type=action.aggregate_type,
                    aggregate_id=action.aggregate_id,
                    aggregate_version=outcome.snapshot.version,
                    event_type=draft.event_type,
                    payload=dict(draft.payload),
                    action_id=action.action_id,
                    correlation_id=action.correlation_id or action.action_id,
                    causation_id=action.causation_id,
                    created_at=action.created_at,
                )
                connection.execute(
                    """
                    INSERT INTO minion_v2_domain_events(
                        event_id, workflow_id, aggregate_type, aggregate_id, aggregate_version,
                        event_type, payload_json, action_id, correlation_id, causation_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.workflow_id,
                        event.aggregate_type.value,
                        event.aggregate_id,
                        event.aggregate_version,
                        event.event_type,
                        _json(event.payload),
                        event.action_id,
                        event.correlation_id,
                        event.causation_id,
                        event.created_at,
                    ),
                )
                events.append(event)

            if outcome.effects and not events:
                raise RuntimeError("outbox effects require a causative domain event")
            effect_ids: list[str] = []
            event_id = events[0].event_id if events else ""
            for index, effect in enumerate(outcome.effects):
                effect_id = f"eff_{uuid4().hex}"
                effect_key = f"{event_id}:{index}"
                payload = dict(effect.payload)
                effect_request_hash = _stable_hash({"effect_type": effect.effect_type, "payload": payload})
                connection.execute(
                    """
                    INSERT INTO minion_v2_outbox(
                        effect_id, effect_key, workflow_id, aggregate_type, aggregate_id,
                        event_id, effect_index, effect_type, request_hash, payload_json,
                        status, attempt_count, max_attempts, next_retry_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                    """,
                    (
                        effect_id,
                        effect_key,
                        action.workflow_id,
                        action.aggregate_type.value,
                        action.aggregate_id,
                        event_id,
                        index,
                        effect.effect_type,
                        effect_request_hash,
                        _json(payload),
                        max(1, int(effect.max_attempts)),
                        action.created_at,
                        action.created_at,
                        action.created_at,
                    ),
                )
                effect_ids.append(effect_id)

            result = DispatchResult(
                snapshot=outcome.snapshot,
                events=tuple(events),
                outbox_effect_ids=tuple(effect_ids),
            )
            encoded_result = _encode_dispatch_result(result)
            connection.execute(
                """
                INSERT INTO minion_v2_action_dedup(
                    aggregate_type, aggregate_id, idempotency_key, action_id,
                    request_hash, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action.aggregate_type.value,
                    action.aggregate_id,
                    action.dedup_key,
                    action.action_id,
                    request_hash,
                    _json(encoded_result),
                    action.created_at,
                ),
            )
            self._update_task_projection_locked(connection, outcome.snapshot)
            self._update_node_projection_locked(connection, outcome.snapshot)
            if action.aggregate_type != AggregateType.TASK:
                self._rebuild_workflow_projection_locked(connection, action.workflow_id, events[-1].event_id if events else "")
            return result

    def read_snapshot(self, aggregate_type: AggregateType, aggregate_id: str) -> AggregateSnapshot | None:
        self.ensure_schema()
        with self._connect() as connection:
            return self._read_snapshot_locked(connection, aggregate_type, aggregate_id)

    def list_workflow_snapshots(self, workflow_id: str) -> tuple[AggregateSnapshot, ...]:
        self.ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM minion_v2_aggregate_snapshots
                WHERE workflow_id = ?
                ORDER BY created_at, aggregate_type, aggregate_id
                """,
                (str(workflow_id),),
            ).fetchall()
            return tuple(_snapshot_from_row(row) for row in rows)

    def read_workflow_projection(self, workflow_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT last_progress_event_id FROM minion_v2_workflow_projection WHERE workflow_id = ?",
                (str(workflow_id),),
            ).fetchone()
            self._rebuild_workflow_projection_locked(
                connection,
                str(workflow_id),
                str(existing["last_progress_event_id"] or "") if existing is not None else "",
            )
            row = connection.execute(
                "SELECT * FROM minion_v2_workflow_projection WHERE workflow_id = ?",
                (str(workflow_id),),
            ).fetchone()
            if row is None:
                return None
            return _decode_json_columns(
                row,
                {
                    "blocker_json": "blocker",
                    "next_legal_actions_json": "next_legal_actions",
                    "metrics_json": "metrics",
                },
            )

    def bind_channel_workflow(self, *, actor_id: str, channel_id: str, workflow_id: str) -> None:
        self.ensure_schema()
        actor = str(actor_id or "").strip()
        channel = str(channel_id or "").strip()
        workflow = str(workflow_id or "").strip()
        if not actor or not channel or not workflow:
            raise ValueError("channel workflow binding requires actor, channel, and workflow")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT 1 FROM minion_v2_aggregate_snapshots WHERE aggregate_type = ? AND aggregate_id = ?",
                (AggregateType.WORKFLOW.value, workflow),
            ).fetchone()
            if row is None:
                raise ValueError("channel workflow binding requires an existing workflow")
            connection.execute(
                """
                INSERT INTO minion_v2_channel_bindings(actor_id, channel_id, workflow_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(actor_id, channel_id) DO UPDATE SET
                    workflow_id = excluded.workflow_id,
                    updated_at = excluded.updated_at
                """,
                (actor, channel, workflow, utc_now()),
            )

    def read_channel_workflow(self, *, actor_id: str, channel_id: str) -> str:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT workflow_id FROM minion_v2_channel_bindings WHERE actor_id = ? AND channel_id = ?",
                (str(actor_id or "").strip(), str(channel_id or "").strip()),
            ).fetchone()
        return str(row["workflow_id"]) if row is not None else ""

    def search_workflows(
        self,
        *,
        actor_id: str,
        channel_id: str,
        query: str = "",
        include_terminal: bool = False,
        limit: int = 20,
    ) -> tuple[dict[str, Any], ...]:
        self.ensure_schema()
        clauses = ["s.aggregate_type = ?", "json_extract(s.payload_json, '$.owner') = ?"]
        parameters: list[Any] = [AggregateType.WORKFLOW.value, str(actor_id or "").strip()]
        if channel_id:
            clauses.append("json_extract(s.payload_json, '$.active_channel') = ?")
            parameters.append(str(channel_id).strip())
        if not include_terminal:
            clauses.append("s.state NOT IN ('COMPLETED', 'REJECTED', 'CANCELLED')")
        text = str(query or "").strip().lower()
        if text:
            clauses.append("(lower(coalesce(t.title, '')) LIKE ? OR lower(coalesce(t.objective, '')) LIKE ?)")
            pattern = f"%{text}%"
            parameters.extend((pattern, pattern))
        parameters.append(max(1, min(int(limit), 100)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.aggregate_id AS workflow_id, s.state AS workflow_state,
                       s.updated_at, coalesce(t.title, '') AS task_title,
                       coalesce(t.objective, '') AS task_objective
                FROM minion_v2_aggregate_snapshots AS s
                LEFT JOIN minion_v2_task_projection AS t
                  ON t.task_id = json_extract(s.payload_json, '$.task_id')
                WHERE """
                + " AND ".join(clauses)
                + " ORDER BY s.updated_at DESC, s.aggregate_id LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def bind_artifact_alias(
        self,
        *,
        actor_id: str,
        channel_id: str,
        alias: str,
        artifact_sha256: str,
    ) -> None:
        self.ensure_schema()
        actor = str(actor_id or "").strip()
        channel = str(channel_id or "").strip()
        name = str(alias or "").strip()
        digest = str(artifact_sha256 or "").removeprefix("sha256:")
        if not actor or not channel or not name or not digest:
            raise ValueError("artifact alias requires actor, channel, name, and artifact")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT durable FROM minion_v2_artifacts WHERE sha256 = ?",
                (digest,),
            ).fetchone()
            if row is None or int(row["durable"]) != 1:
                raise ValueError("artifact alias requires a durable artifact")
            connection.execute(
                """
                INSERT INTO minion_v2_artifact_aliases(actor_id, channel_id, alias, artifact_sha256, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(actor_id, channel_id, alias) DO UPDATE SET
                    artifact_sha256 = excluded.artifact_sha256,
                    updated_at = excluded.updated_at
                """,
                (actor, channel, name, digest, utc_now()),
            )

    def resolve_artifact_alias(self, *, actor_id: str, channel_id: str, alias: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT a.* FROM minion_v2_artifacts AS a
                JOIN minion_v2_artifact_aliases AS x ON x.artifact_sha256 = a.sha256
                WHERE x.actor_id = ? AND x.channel_id = ? AND x.alias = ?
                """,
                (str(actor_id or "").strip(), str(channel_id or "").strip(), str(alias or "").strip()),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["durable"] = bool(result["durable"])
        result["provenance"] = json.loads(str(result.pop("provenance_json")))
        result["metadata"] = json.loads(str(result.pop("metadata_json")))
        return result

    def read_latest_workflow_event(self, workflow_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT event_type, aggregate_type, created_at
                FROM minion_v2_domain_events
                WHERE workflow_id = ?
                ORDER BY created_at DESC, event_id DESC
                LIMIT 1
                """,
                (str(workflow_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def search_tasks(
        self,
        *,
        query: str = "",
        task_id: str = "",
        family_id: str = "",
        include_archived: bool = False,
        limit: int = 20,
    ) -> tuple[dict[str, Any], ...]:
        self.ensure_schema()
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(str(task_id))
        if family_id:
            clauses.append("family_id = ?")
            parameters.append(str(family_id))
        if not include_archived:
            clauses.append("state != 'ARCHIVED'")
        text = str(query or "").strip().lower()
        if text:
            clauses.append("(lower(title) LIKE ? OR lower(objective) LIKE ? OR lower(workspace_key) LIKE ?)")
            pattern = f"%{text}%"
            parameters.extend((pattern, pattern, pattern))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(max(1, min(int(limit), 100)))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM minion_v2_task_projection"
                + where
                + " ORDER BY updated_at DESC, task_id LIMIT ?",
                tuple(parameters),
            ).fetchall()
            return tuple(dict(row) for row in rows)

    def has_nonterminal_workflows_for_task(self, task_id: str) -> bool:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM minion_v2_aggregate_snapshots
                WHERE aggregate_type = ?
                  AND json_extract(payload_json, '$.task_id') = ?
                  AND state NOT IN ('COMPLETED', 'REJECTED', 'CANCELLED')
                LIMIT 1
                """,
                (AggregateType.WORKFLOW.value, str(task_id)),
            ).fetchone()
            return row is not None

    def claim_outbox(self, worker_id: str, *, limit: int = 20, lease_seconds: int = 60) -> tuple[dict[str, Any], ...]:
        self.ensure_schema()
        now = _utc_datetime()
        now_text = now.isoformat()
        locked_until = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM minion_v2_outbox
                WHERE attempt_count < max_attempts
                  AND next_retry_at <= ?
                  AND (
                    status = 'pending'
                    OR (status = 'inflight' AND locked_until <= ?)
                  )
                ORDER BY created_at, effect_id
                LIMIT ?
                """,
                (now_text, now_text, max(1, int(limit))),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                cursor = connection.execute(
                    """
                    UPDATE minion_v2_outbox
                    SET status = 'inflight', attempt_count = attempt_count + 1,
                        locked_by = ?, locked_until = ?, updated_at = ?
                    WHERE effect_id = ?
                      AND (status = 'pending' OR (status = 'inflight' AND locked_until <= ?))
                    """,
                    (worker_id, locked_until, now_text, str(row["effect_id"]), now_text),
                )
                if cursor.rowcount == 1:
                    item = dict(row)
                    item.update(
                        {
                            "status": "inflight",
                            "attempt_count": int(row["attempt_count"]) + 1,
                            "locked_by": worker_id,
                            "locked_until": locked_until,
                            "payload": json.loads(str(row["payload_json"])),
                        }
                    )
                    claimed.append(item)
            return tuple(claimed)

    def complete_outbox_effect(
        self,
        effect_id: str,
        *,
        worker_id: str,
        provider_request_id: str = "",
        result_artifact_ref: Mapping[str, Any] | None = None,
    ) -> bool:
        self.ensure_schema()
        result_ref = dict(result_artifact_ref or {})
        now = utc_now()
        with self._transaction() as connection:
            self._assert_artifact_refs_durable(connection, result_ref)
            row = connection.execute(
                "SELECT * FROM minion_v2_outbox WHERE effect_id = ?",
                (str(effect_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown outbox effect: {effect_id}")
            receipt = connection.execute(
                "SELECT request_hash FROM minion_v2_effect_receipts WHERE effect_key = ?",
                (str(row["effect_key"]),),
            ).fetchone()
            if receipt is not None:
                if str(receipt["request_hash"]) != str(row["request_hash"]):
                    raise ValueError("effect receipt request hash mismatch")
                return False
            if str(row["status"]) != "inflight" or str(row["locked_by"]) != str(worker_id):
                raise LeaseConflict("outbox effect is not claimed by this worker")
            connection.execute(
                """
                INSERT INTO minion_v2_effect_receipts(
                    effect_key, effect_id, request_hash, provider_request_id,
                    result_artifact_ref_json, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(row["effect_key"]),
                    str(effect_id),
                    str(row["request_hash"]),
                    str(provider_request_id),
                    _json(result_ref),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE minion_v2_outbox
                SET status = 'completed', provider_request_id = ?, result_artifact_ref_json = ?,
                    locked_by = '', locked_until = '', last_error = '', updated_at = ?
                WHERE effect_id = ?
                """,
                (str(provider_request_id), _json(result_ref), now, str(effect_id)),
            )
            return True

    def renew_outbox_claim(self, effect_id: str, *, worker_id: str, lease_seconds: int = 60) -> None:
        self.ensure_schema()
        now = _utc_datetime()
        locked_until = now + timedelta(seconds=max(1, lease_seconds))
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE minion_v2_outbox
                SET locked_until = ?, updated_at = ?
                WHERE effect_id = ? AND status = 'inflight' AND locked_by = ?
                """,
                (locked_until.isoformat(), now.isoformat(), str(effect_id), str(worker_id)),
            )
            if cursor.rowcount != 1:
                raise LeaseConflict("outbox effect is not claimed by this worker")

    def retry_outbox_effect(
        self,
        effect_id: str,
        *,
        worker_id: str,
        error: str,
        retry_after_seconds: int = 5,
    ) -> str:
        self.ensure_schema()
        now = _utc_datetime()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status, locked_by, attempt_count, max_attempts FROM minion_v2_outbox WHERE effect_id = ?",
                (str(effect_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown outbox effect: {effect_id}")
            if str(row["status"]) != "inflight" or str(row["locked_by"]) != str(worker_id):
                raise LeaseConflict("outbox effect is not claimed by this worker")
            exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
            status = "failed" if exhausted else "pending"
            next_retry_at = (now + timedelta(seconds=max(0, retry_after_seconds))).isoformat()
            connection.execute(
                """
                UPDATE minion_v2_outbox
                SET status = ?, next_retry_at = ?, locked_by = '', locked_until = '',
                    last_error = ?, updated_at = ?
                WHERE effect_id = ?
                """,
                (status, next_retry_at, str(error), now.isoformat(), str(effect_id)),
            )
            return status

    def claim_lease(
        self,
        resource_key: str,
        owner_id: str,
        *,
        ttl_seconds: int = 60,
        metadata: Mapping[str, Any] | None = None,
    ) -> LeaseGrant:
        if not resource_key.strip() or not owner_id.strip():
            raise ValueError("resource_key and owner_id are required")
        self.ensure_schema()
        now = _utc_datetime()
        expires_at = now + timedelta(seconds=max(1, ttl_seconds))
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            if row is not None and str(row["owner_id"]) and _parse_datetime(str(row["expires_at"])) > now:
                raise LeaseConflict(f"resource already leased by {row['owner_id']}")
            fencing_token = (int(row["fencing_token"]) if row is not None else 0) + 1
            connection.execute(
                """
                INSERT INTO minion_v2_leases(
                    resource_key, owner_id, fencing_token, acquired_at, renewed_at, expires_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resource_key) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    fencing_token = excluded.fencing_token,
                    acquired_at = excluded.acquired_at,
                    renewed_at = excluded.renewed_at,
                    expires_at = excluded.expires_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    resource_key,
                    owner_id,
                    fencing_token,
                    now.isoformat(),
                    now.isoformat(),
                    expires_at.isoformat(),
                    _json(dict(metadata or {})),
                ),
            )
            return LeaseGrant(resource_key, owner_id, fencing_token, now.isoformat(), expires_at.isoformat())

    def renew_lease(
        self,
        resource_key: str,
        owner_id: str,
        fencing_token: int,
        *,
        ttl_seconds: int = 60,
    ) -> LeaseGrant:
        self.ensure_schema()
        now = _utc_datetime()
        expires_at = now + timedelta(seconds=max(1, ttl_seconds))
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            self._assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=now)
            connection.execute(
                "UPDATE minion_v2_leases SET renewed_at = ?, expires_at = ? WHERE resource_key = ?",
                (now.isoformat(), expires_at.isoformat(), resource_key),
            )
            return LeaseGrant(resource_key, owner_id, fencing_token, str(row["acquired_at"]), expires_at.isoformat())

    def assert_fencing_token(self, resource_key: str, owner_id: str, fencing_token: int) -> None:
        self.ensure_schema()
        now = _utc_datetime()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            self._assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=now)

    def release_lease(self, resource_key: str, owner_id: str, fencing_token: int) -> None:
        self.ensure_schema()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            self._assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=None)
            connection.execute(
                "UPDATE minion_v2_leases SET owner_id = '', renewed_at = ?, expires_at = ? WHERE resource_key = ?",
                (utc_now(), utc_now(), resource_key),
            )

    def expired_leases(self) -> tuple[dict[str, Any], ...]:
        self.ensure_schema()
        now = utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM minion_v2_leases WHERE owner_id != '' AND expires_at <= ? ORDER BY expires_at",
                (now,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["metadata"] = json.loads(str(item.pop("metadata_json")))
                result.append(item)
            return tuple(result)

    def read_lease(self, resource_key: str) -> dict[str, Any] | None:
        """Read lease ownership and process metadata without changing its lifetime."""
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_leases WHERE resource_key = ?",
                (str(resource_key),),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["metadata"] = json.loads(str(item.pop("metadata_json")))
            return item

    def update_lease_metadata(
        self,
        resource_key: str,
        owner_id: str,
        fencing_token: int,
        metadata: Mapping[str, Any],
    ) -> None:
        self.ensure_schema()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            self._assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=_utc_datetime())
            connection.execute(
                "UPDATE minion_v2_leases SET metadata_json = ?, renewed_at = ? WHERE resource_key = ?",
                (_json(dict(metadata)), utc_now(), resource_key),
            )

    def clear_expired_lease(self, resource_key: str, fencing_token: int) -> bool:
        self.ensure_schema()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE minion_v2_leases
                SET owner_id = '', renewed_at = ?, expires_at = ?
                WHERE resource_key = ? AND fencing_token = ? AND expires_at <= ?
                """,
                (utc_now(), utc_now(), resource_key, int(fencing_token), utc_now()),
            )
            return cursor.rowcount == 1

    def rebuild_workflow_projections(self) -> int:
        self.ensure_schema()
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT aggregate_id FROM minion_v2_aggregate_snapshots WHERE aggregate_type = ?",
                (AggregateType.WORKFLOW.value,),
            ).fetchall()
            for row in rows:
                self._rebuild_workflow_projection_locked(connection, str(row["aggregate_id"]), "")
            return len(rows)

    def orphaned_workflow_ids(self) -> tuple[str, ...]:
        self.rebuild_workflow_projections()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT workflow_id FROM minion_v2_workflow_projection WHERE liveness = 'orphaned' ORDER BY workflow_id"
            ).fetchall()
            return tuple(str(row["workflow_id"]) for row in rows)

    def record_artifact(
        self,
        ref: ArtifactRef,
        *,
        storage_path: Path,
        provenance: Mapping[str, Any],
        metadata: Mapping[str, Any],
        child_refs: tuple[tuple[str, str], ...],
    ) -> None:
        self.ensure_schema()
        if not storage_path.is_file():
            raise FileNotFoundError(storage_path)
        if storage_path.stat().st_size != ref.byte_size:
            raise IOError("artifact size changed before metadata publication")
        now = utc_now()
        with self._transaction() as connection:
            for child_sha, _relation in child_refs:
                child = connection.execute(
                    "SELECT durable FROM minion_v2_artifacts WHERE sha256 = ?",
                    (str(child_sha),),
                ).fetchone()
                if child is None or int(child["durable"]) != 1:
                    raise ValueError(f"child artifact is not durable: {child_sha}")
            existing = connection.execute(
                "SELECT artifact_type, schema_version, media_type, byte_size, storage_path FROM minion_v2_artifacts WHERE sha256 = ?",
                (ref.sha256,),
            ).fetchone()
            if existing is not None:
                expected = (ref.artifact_type, ref.schema_version, ref.media_type, ref.byte_size, str(storage_path))
                actual = (
                    str(existing["artifact_type"]),
                    str(existing["schema_version"]),
                    str(existing["media_type"]),
                    int(existing["byte_size"]),
                    str(existing["storage_path"]),
                )
                if actual != expected:
                    raise ValueError("typed artifact metadata is immutable")
            connection.execute(
                """
                INSERT INTO minion_v2_artifacts(
                    sha256, artifact_type, schema_version, media_type, byte_size,
                    storage_path, durable, provenance_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(sha256) DO NOTHING
                """,
                (
                    ref.sha256,
                    ref.artifact_type,
                    ref.schema_version,
                    ref.media_type,
                    ref.byte_size,
                    str(storage_path),
                    _json(dict(provenance)),
                    _json(dict(metadata)),
                    now,
                ),
            )
            for child_sha, relation in child_refs:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO minion_v2_artifact_refs(parent_sha256, child_sha256, relation, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (ref.sha256, str(child_sha), str(relation), now),
                )

    def artifact_is_durable(self, sha256: str) -> bool:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT durable, storage_path FROM minion_v2_artifacts WHERE sha256 = ?",
                (str(sha256).removeprefix("sha256:"),),
            ).fetchone()
            return bool(row is not None and int(row["durable"]) == 1 and Path(str(row["storage_path"])).is_file())

    def read_artifact_record(self, sha256: str) -> dict[str, Any] | None:
        self.ensure_schema()
        digest = str(sha256).removeprefix("sha256:")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_artifacts WHERE sha256 = ?",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["durable"] = bool(result["durable"])
            result["provenance"] = json.loads(str(result.pop("provenance_json")))
            result["metadata"] = json.loads(str(result.pop("metadata_json")))
            return result

    def issue_human_decision_token(
        self,
        *,
        workflow_id: str,
        architecture_revision_id: str,
        manifest_sha: str,
        actor_id: str,
        active_channel_id: str,
        ttl_seconds: int = 86400,
    ) -> str:
        required = {
            "workflow_id": workflow_id,
            "architecture_revision_id": architecture_revision_id,
            "manifest_sha": manifest_sha,
            "actor_id": actor_id,
            "active_channel_id": active_channel_id,
        }
        missing = [key for key, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"human decision token missing fields: {', '.join(missing)}")
        self.ensure_schema()
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = _utc_datetime()
        expires_at = now + timedelta(seconds=max(60, int(ttl_seconds)))
        with self._transaction() as connection:
            artifact = connection.execute(
                "SELECT durable FROM minion_v2_artifacts WHERE sha256 = ?",
                (str(manifest_sha).removeprefix("sha256:"),),
            ).fetchone()
            if artifact is None or int(artifact["durable"]) != 1:
                raise ValueError("human decision token requires a durable architecture manifest")
            connection.execute(
                """
                INSERT INTO minion_v2_human_decisions(
                    token_hash, workflow_id, architecture_revision_id, manifest_sha,
                    actor_id, active_channel_id, expires_at, issued_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    workflow_id,
                    architecture_revision_id,
                    str(manifest_sha).removeprefix("sha256:"),
                    actor_id,
                    active_channel_id,
                    expires_at.isoformat(),
                    now.isoformat(),
                ),
            )
        return token

    def inspect_human_decision_token(self, token: str) -> dict[str, Any] | None:
        self.ensure_schema()
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_human_decisions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result.pop("token_hash", None)
            return result

    def reissue_human_decision_token(
        self,
        *,
        workflow_id: str,
        actor_id: str,
        active_channel_id: str,
        ttl_seconds: int = 86400,
    ) -> str:
        """Replace the unique pending card token for a semantic/manual decision path."""

        required = {
            "workflow_id": workflow_id,
            "actor_id": actor_id,
            "active_channel_id": active_channel_id,
        }
        missing = [key for key, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"human decision binding missing fields: {', '.join(missing)}")
        self.ensure_schema()
        now = _utc_datetime()
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = now + timedelta(seconds=max(60, int(ttl_seconds)))
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE minion_v2_human_decisions
                SET status = 'expired'
                WHERE status = 'issued' AND expires_at <= ?
                """,
                (now.isoformat(),),
            )
            rows = connection.execute(
                """
                SELECT * FROM minion_v2_human_decisions
                WHERE workflow_id = ? AND actor_id = ? AND active_channel_id = ? AND status = 'issued'
                ORDER BY issued_at DESC
                """,
                (str(workflow_id), str(actor_id), str(active_channel_id)),
            ).fetchall()
            bindings = {
                (
                    str(row["architecture_revision_id"]),
                    str(row["manifest_sha"]),
                )
                for row in rows
            }
            if not bindings:
                raise ValueError("current workflow has no pending human decision")
            if len(bindings) != 1:
                raise ValueError("current workflow has multiple pending human decisions")
            architecture_revision_id, manifest_sha = next(iter(bindings))
            artifact = connection.execute(
                "SELECT durable FROM minion_v2_artifacts WHERE sha256 = ?",
                (manifest_sha,),
            ).fetchone()
            if artifact is None or int(artifact["durable"]) != 1:
                raise ValueError("pending human decision references a non-durable artifact")
            connection.execute(
                """
                UPDATE minion_v2_human_decisions
                SET status = 'expired'
                WHERE workflow_id = ? AND architecture_revision_id = ? AND manifest_sha = ?
                  AND actor_id = ? AND active_channel_id = ? AND status = 'issued'
                """,
                (
                    str(workflow_id),
                    architecture_revision_id,
                    manifest_sha,
                    str(actor_id),
                    str(active_channel_id),
                ),
            )
            connection.execute(
                """
                INSERT INTO minion_v2_human_decisions(
                    token_hash, workflow_id, architecture_revision_id, manifest_sha,
                    actor_id, active_channel_id, expires_at, issued_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    str(workflow_id),
                    architecture_revision_id,
                    manifest_sha,
                    str(actor_id),
                    str(active_channel_id),
                    expires_at.isoformat(),
                    now.isoformat(),
                ),
            )
        return token

    def record_worker_invocation(
        self,
        *,
        invocation_id: str,
        workflow_id: str,
        aggregate_type: AggregateType,
        aggregate_id: str,
        lease_resource_key: str,
        fencing_token: int,
        role: str,
        prompt_pack_ref: Mapping[str, Any],
    ) -> None:
        self.assert_fencing_token(lease_resource_key, invocation_id, fencing_token)
        self.ensure_schema()
        now = utc_now()
        with self._transaction() as connection:
            self._assert_artifact_refs_durable(connection, prompt_pack_ref)
            connection.execute(
                """
                INSERT INTO minion_v2_worker_invocations(
                    invocation_id, workflow_id, aggregate_type, aggregate_id, lease_resource_key,
                    fencing_token, role, prompt_pack_ref_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                ON CONFLICT(invocation_id) DO UPDATE SET
                    workflow_id = excluded.workflow_id,
                    aggregate_type = excluded.aggregate_type,
                    aggregate_id = excluded.aggregate_id,
                    lease_resource_key = excluded.lease_resource_key,
                    fencing_token = excluded.fencing_token,
                    role = excluded.role,
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
                    _json(dict(prompt_pack_ref)),
                    now,
                    now,
                ),
            )

    def read_worker_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_worker_invocations WHERE invocation_id = ?",
                (str(invocation_id),),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        for key in ("prompt_pack_ref_json", "continuation_ref_json"):
            raw = str(value.pop(key, "{}") or "{}")
            value[key.removesuffix("_json")] = json.loads(raw)
        return value

    def suspend_worker_invocation(
        self,
        *,
        invocation_id: str,
        fencing_token: int,
        continuation_ref: Mapping[str, Any],
        status: str = "suspended",
    ) -> None:
        normalized = str(status or "suspended").strip().lower()
        if normalized not in {"suspended", "interrupted"}:
            raise ValueError("worker suspension status must be suspended or interrupted")
        self.ensure_schema()
        with self._transaction() as connection:
            invocation = connection.execute(
                "SELECT * FROM minion_v2_worker_invocations WHERE invocation_id = ?",
                (str(invocation_id),),
            ).fetchone()
            if invocation is None:
                raise KeyError(f"unknown worker invocation: {invocation_id}")
            self._assert_lease_locked(
                connection,
                str(invocation["lease_resource_key"]),
                str(invocation_id),
                int(fencing_token),
            )
            self._assert_artifact_refs_durable(connection, continuation_ref)
            connection.execute(
                """
                UPDATE minion_v2_worker_invocations
                SET status = ?, continuation_ref_json = ?, updated_at = ?
                WHERE invocation_id = ?
                """,
                (normalized, _json(dict(continuation_ref)), utc_now(), str(invocation_id)),
            )
            self._rebuild_workflow_projection_locked(connection, str(invocation["workflow_id"]), "")

    def complete_worker_session(self, invocation_id: str, *, status: str = "completed") -> bool:
        normalized = str(status or "completed").strip().lower()
        if normalized not in {"completed", "cancelled"}:
            raise ValueError("worker session completion status must be completed or cancelled")
        self.ensure_schema()
        with self._transaction() as connection:
            invocation = connection.execute(
                "SELECT * FROM minion_v2_worker_invocations WHERE invocation_id = ?",
                (str(invocation_id),),
            ).fetchone()
            if invocation is None:
                return False
            if str(invocation["status"]) == normalized:
                return True
            snapshot = self._read_snapshot_locked(
                connection,
                AggregateType(str(invocation["aggregate_type"])),
                str(invocation["aggregate_id"]),
            )
            allowed_terminal = {
                AggregateType.ARCHITECTURE_REVISION: {"ACCEPTED", "REJECTED", "CANCELLED"},
                AggregateType.DAG_NODE_RUN: {"ACCEPTED", "STALE", "CANCELLED"},
            }
            if snapshot is None or snapshot.state not in allowed_terminal.get(snapshot.aggregate_type, set()):
                raise ValueError("worker session cannot complete before its owned aggregate reaches a terminal outcome")
            connection.execute(
                "UPDATE minion_v2_worker_invocations SET status = ?, updated_at = ? WHERE invocation_id = ?",
                (normalized, utc_now(), str(invocation_id)),
            )
            self._rebuild_workflow_projection_locked(connection, str(invocation["workflow_id"]), "")
            return True

    def record_worker_event(self, event: Mapping[str, Any]) -> None:
        invocation_id = str(event.get("invocation_id") or event.get("minion_id") or "").strip()
        if not invocation_id:
            return
        event_kind = str(event.get("event_kind") or "progress")
        payload = dict(event.get("payload") or {})
        phase = str(payload.get("phase") or "")
        round_index = int(payload.get("round") or 0)
        tool_call_count = int(payload.get("tool_call_count") or 0)
        created_at = str(event.get("created_at") or utc_now())
        self.ensure_schema()
        with self._transaction() as connection:
            invocation = connection.execute(
                "SELECT status FROM minion_v2_worker_invocations WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
            if invocation is None:
                return
            connection.execute(
                """
                INSERT INTO minion_v2_worker_events(
                    invocation_id, event_kind, phase, round_index, tool_call_count, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (invocation_id, event_kind, phase, round_index, tool_call_count, _json(payload), created_at),
            )
            if event_kind == "progress" and phase == "llm_round_completed":
                connection.execute(
                    """
                    UPDATE minion_v2_worker_invocations
                    SET last_completed_turn = MAX(last_completed_turn, ?), updated_at = ?
                    WHERE invocation_id = ?
                    """,
                    (round_index, created_at, invocation_id),
                )

    def record_worker_turn(
        self,
        *,
        invocation_id: str,
        fencing_token: int,
        turn_index: int,
        llm_request_ref: Mapping[str, Any],
        llm_response_ref: Mapping[str, Any],
        tool_summary_ref: Mapping[str, Any] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float = 0,
        latency_ms: int = 0,
        tool_latency_ms: int = 0,
        wall_latency_ms: int = 0,
    ) -> None:
        self.ensure_schema()
        with self._transaction() as connection:
            invocation = connection.execute(
                "SELECT * FROM minion_v2_worker_invocations WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
            if invocation is None:
                raise KeyError(f"unknown worker invocation: {invocation_id}")
            self._assert_lease_locked(
                connection,
                str(invocation["lease_resource_key"]),
                invocation_id,
                fencing_token,
            )
            refs = {
                "request": dict(llm_request_ref),
                "response": dict(llm_response_ref),
                "tools": dict(tool_summary_ref or {}),
            }
            self._assert_artifact_refs_durable(connection, refs)
            now = utc_now()
            connection.execute(
                """
                INSERT INTO minion_v2_worker_turns(
                    invocation_id, turn_index, llm_request_ref_json, llm_response_ref_json,
                    tool_summary_ref_json, input_tokens, output_tokens, cost, latency_ms,
                    tool_latency_ms, wall_latency_ms, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(invocation_id, turn_index) DO NOTHING
                """,
                (
                    invocation_id,
                    int(turn_index),
                    _json(dict(llm_request_ref)),
                    _json(dict(llm_response_ref)),
                    _json(dict(tool_summary_ref or {})),
                    max(0, int(input_tokens)),
                    max(0, int(output_tokens)),
                    max(0.0, float(cost)),
                    max(0, int(latency_ms)),
                    max(0, int(tool_latency_ms)),
                    max(0, int(wall_latency_ms)),
                    now,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] == 1:
                connection.execute(
                    """
                    UPDATE minion_v2_worker_invocations
                    SET last_completed_turn = MAX(last_completed_turn, ?),
                        total_input_tokens = total_input_tokens + ?,
                        total_output_tokens = total_output_tokens + ?,
                        total_cost = total_cost + ?, total_latency_ms = total_latency_ms + ?,
                        total_tool_latency_ms = total_tool_latency_ms + ?,
                        total_wall_latency_ms = total_wall_latency_ms + ?,
                        updated_at = ?
                    WHERE invocation_id = ?
                    """,
                    (
                        int(turn_index),
                        max(0, int(input_tokens)),
                        max(0, int(output_tokens)),
                        max(0.0, float(cost)),
                        max(0, int(latency_ms)),
                        max(0, int(tool_latency_ms)),
                        max(0, int(wall_latency_ms)),
                        now,
                        invocation_id,
                    ),
                )
            self._rebuild_workflow_projection_locked(
                connection,
                str(invocation["workflow_id"]),
                "",
            )

    def finish_worker_invocation(self, *, invocation_id: str, fencing_token: int, status: str) -> None:
        normalized = str(status or "").strip().lower()
        if normalized not in {"completed", "failed", "cancelled"}:
            raise ValueError(f"invalid worker invocation terminal status: {status}")
        self.ensure_schema()
        with self._transaction() as connection:
            invocation = connection.execute(
                "SELECT * FROM minion_v2_worker_invocations WHERE invocation_id = ?",
                (str(invocation_id),),
            ).fetchone()
            if invocation is None:
                raise KeyError(f"unknown worker invocation: {invocation_id}")
            self._assert_lease_locked(
                connection,
                str(invocation["lease_resource_key"]),
                str(invocation_id),
                int(fencing_token),
            )
            connection.execute(
                "UPDATE minion_v2_worker_invocations SET status = ?, updated_at = ? WHERE invocation_id = ?",
                (normalized, utc_now(), str(invocation_id)),
            )
            self._rebuild_workflow_projection_locked(connection, str(invocation["workflow_id"]), "")

    def update_node_journal(
        self,
        *,
        node_run_id: str,
        workflow_id: str,
        lease_resource_key: str,
        owner_id: str,
        fencing_token: int,
        expected_generation: int,
        journal: Mapping[str, Any],
    ) -> int:
        self.ensure_schema()
        with self._transaction() as connection:
            self._assert_lease_locked(connection, lease_resource_key, owner_id, fencing_token)
            row = connection.execute(
                "SELECT generation FROM minion_v2_node_journals WHERE node_run_id = ?",
                (node_run_id,),
            ).fetchone()
            current_generation = int(row["generation"]) if row is not None else 0
            if current_generation != int(expected_generation):
                raise AggregateVersionConflict(
                    f"expected journal generation {expected_generation}, found {current_generation}"
                )
            next_generation = current_generation + 1
            connection.execute(
                """
                INSERT INTO minion_v2_node_journals(
                    node_run_id, workflow_id, lease_resource_key, fencing_token,
                    generation, journal_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_run_id) DO UPDATE SET
                    lease_resource_key = excluded.lease_resource_key,
                    fencing_token = excluded.fencing_token,
                    generation = excluded.generation,
                    journal_json = excluded.journal_json,
                    updated_at = excluded.updated_at
                """,
                (
                    node_run_id,
                    workflow_id,
                    lease_resource_key,
                    fencing_token,
                    next_generation,
                    _json(dict(journal)),
                    utc_now(),
                ),
            )
            return next_generation

    def read_node_journal(self, node_run_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM minion_v2_node_journals WHERE node_run_id = ?",
                (str(node_run_id),),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["journal"] = json.loads(str(result.pop("journal_json")))
            return result

    def _read_snapshot_locked(
        self,
        connection: sqlite3.Connection,
        aggregate_type: AggregateType,
        aggregate_id: str,
    ) -> AggregateSnapshot | None:
        row = connection.execute(
            "SELECT * FROM minion_v2_aggregate_snapshots WHERE aggregate_type = ? AND aggregate_id = ?",
            (aggregate_type.value, aggregate_id),
        ).fetchone()
        return _snapshot_from_row(row) if row is not None else None

    def _write_snapshot_locked(
        self,
        connection: sqlite3.Connection,
        current: AggregateSnapshot | None,
        snapshot: AggregateSnapshot,
    ) -> None:
        if current is None:
            try:
                connection.execute(
                    """
                    INSERT INTO minion_v2_aggregate_snapshots(
                        aggregate_type, aggregate_id, workflow_id, state, version,
                        payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.aggregate_type.value,
                        snapshot.aggregate_id,
                        snapshot.workflow_id,
                        snapshot.state,
                        snapshot.version,
                        _json(snapshot.payload),
                        snapshot.created_at,
                        snapshot.updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AggregateVersionConflict("aggregate was concurrently created") from exc
            return
        cursor = connection.execute(
            """
            UPDATE minion_v2_aggregate_snapshots
            SET state = ?, version = ?, payload_json = ?, updated_at = ?
            WHERE aggregate_type = ? AND aggregate_id = ? AND version = ?
            """,
            (
                snapshot.state,
                snapshot.version,
                _json(snapshot.payload),
                snapshot.updated_at,
                snapshot.aggregate_type.value,
                snapshot.aggregate_id,
                current.version,
            ),
        )
        if cursor.rowcount != 1:
            raise AggregateVersionConflict("aggregate snapshot compare-and-swap failed")

    def _assert_artifact_refs_durable(self, connection: sqlite3.Connection, value: Any) -> None:
        for digest in _artifact_digests(value):
            row = connection.execute(
                "SELECT durable, storage_path FROM minion_v2_artifacts WHERE sha256 = ?",
                (digest,),
            ).fetchone()
            if row is None or int(row["durable"]) != 1:
                raise ValueError(f"action references a missing or non-durable artifact: {digest}")
            if not Path(str(row["storage_path"])).is_file():
                raise ValueError(f"action references an artifact with missing storage: {digest}")

    def _consume_human_decision_locked(self, connection: sqlite3.Connection, action: ActionEnvelope) -> None:
        if action.action_type not in {"HUMAN_ACCEPT", "HUMAN_EDIT", "HUMAN_REJECT", "CLARIFICATION_PROVIDED"}:
            return
        token = str(action.payload.get("decision_token") or "")
        if not token:
            raise ValueError("human decision action requires decision_token")
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        row = connection.execute(
            "SELECT * FROM minion_v2_human_decisions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown human decision token")
        if str(row["status"]) != "issued":
            raise ValueError("human decision token is stale or already consumed")
        if _parse_datetime(str(row["expires_at"])) <= _utc_datetime():
            connection.execute(
                "UPDATE minion_v2_human_decisions SET status = 'expired' WHERE token_hash = ?",
                (token_hash,),
            )
            raise ValueError("human decision token has expired")
        manifest_sha = _manifest_sha_from_action(action)
        mismatches = []
        if str(row["workflow_id"]) != action.workflow_id:
            mismatches.append("workflow_id")
        if str(row["architecture_revision_id"]) != action.aggregate_id:
            mismatches.append("architecture_revision_id")
        if manifest_sha != str(row["manifest_sha"]):
            mismatches.append("manifest_sha")
        if str(row["actor_id"]) != action.actor:
            mismatches.append("actor_id")
        if str(row["active_channel_id"]) != action.source_channel:
            mismatches.append("active_channel_id")
        if mismatches:
            raise ValueError(f"stale human decision binding: {', '.join(mismatches)}")
        cursor = connection.execute(
            """
            UPDATE minion_v2_human_decisions
            SET status = 'consumed', decision = ?, action_id = ?, consumed_at = ?
            WHERE token_hash = ? AND status = 'issued'
            """,
            (action.action_type, action.action_id, action.created_at, token_hash),
        )
        if cursor.rowcount != 1:
            raise ValueError("human decision token was consumed concurrently")

    def _update_node_projection_locked(self, connection: sqlite3.Connection, snapshot: AggregateSnapshot) -> None:
        if snapshot.aggregate_type != AggregateType.DAG_NODE_RUN:
            return
        payload = dict(snapshot.payload)
        connection.execute(
            """
            INSERT INTO minion_v2_node_projection(
                node_run_id, workflow_id, epoch_id, unit_id, node_kind, state,
                dependency_node_ids_json, active_worker_id, candidate_digest, blocker_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_run_id) DO UPDATE SET
                state = excluded.state,
                dependency_node_ids_json = excluded.dependency_node_ids_json,
                active_worker_id = excluded.active_worker_id,
                candidate_digest = excluded.candidate_digest,
                blocker_json = excluded.blocker_json,
                updated_at = excluded.updated_at
            """,
            (
                snapshot.aggregate_id,
                snapshot.workflow_id,
                str(payload.get("epoch_id") or ""),
                str(payload.get("unit_id") or ""),
                str(payload.get("node_kind") or "unit"),
                snapshot.state,
                _json(list(payload.get("dependency_node_ids") or [])),
                str(payload.get("active_worker_id") or ""),
                str(payload.get("candidate_digest") or ""),
                _json(dict(payload.get("blocker") or {})),
                snapshot.updated_at,
            ),
        )

    def _update_task_projection_locked(self, connection: sqlite3.Connection, snapshot: AggregateSnapshot) -> None:
        if snapshot.aggregate_type != AggregateType.TASK:
            return
        payload = dict(snapshot.payload)
        revision_ref = dict(payload.get("task_revision_ref") or {})
        connection.execute(
            """
            INSERT INTO minion_v2_task_projection(
                task_id, state, title, objective, family_id, workspace_key,
                task_revision_sha, owner, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                state = excluded.state,
                title = excluded.title,
                objective = excluded.objective,
                workspace_key = excluded.workspace_key,
                task_revision_sha = excluded.task_revision_sha,
                owner = excluded.owner,
                updated_at = excluded.updated_at
            """,
            (
                snapshot.aggregate_id,
                snapshot.state,
                str(payload.get("title") or ""),
                str(payload.get("objective") or ""),
                str(payload.get("family_id") or ""),
                str(payload.get("workspace_key") or ""),
                str(revision_ref.get("sha256") or ""),
                str(payload.get("owner") or ""),
                snapshot.updated_at,
            ),
        )

    def _rebuild_workflow_projection_locked(
        self,
        connection: sqlite3.Connection,
        workflow_id: str,
        last_event_id: str,
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM minion_v2_aggregate_snapshots WHERE workflow_id = ? ORDER BY updated_at DESC",
            (workflow_id,),
        ).fetchall()
        snapshots = [_snapshot_from_row(row) for row in rows]
        workflow = next((item for item in snapshots if item.aggregate_type == AggregateType.WORKFLOW), None)
        if workflow is None:
            return
        active = _active_projection_snapshot(snapshots, workflow)
        phase = _current_phase(workflow, active)
        next_actions = self.engine.legal_actions(active.aggregate_type, active.state) if active is not None else ()
        waiting_for_user = bool(active is not None and active.state in _HUMAN_WAIT_STATES)
        liveness = self._liveness_locked(connection, workflow, snapshots, waiting_for_user)
        blocker = dict((active.payload if active is not None else workflow.payload).get("blocker") or {})
        connection.execute(
            """
            INSERT INTO minion_v2_workflow_projection(
                workflow_id, current_phase, workflow_state, active_aggregate_type,
                active_aggregate_id, active_worker_id, blocker_json, next_legal_actions_json,
                waiting_for_user, liveness, metrics_json, last_progress_event_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workflow_id) DO UPDATE SET
                current_phase = excluded.current_phase,
                workflow_state = excluded.workflow_state,
                active_aggregate_type = excluded.active_aggregate_type,
                active_aggregate_id = excluded.active_aggregate_id,
                active_worker_id = excluded.active_worker_id,
                blocker_json = excluded.blocker_json,
                next_legal_actions_json = excluded.next_legal_actions_json,
                waiting_for_user = excluded.waiting_for_user,
                liveness = excluded.liveness,
                metrics_json = excluded.metrics_json,
                last_progress_event_id = excluded.last_progress_event_id,
                updated_at = excluded.updated_at
            """,
            (
                workflow_id,
                phase,
                workflow.state,
                active.aggregate_type.value if active is not None else "",
                active.aggregate_id if active is not None else "",
                str((active.payload if active is not None else {}).get("active_worker_id") or ""),
                _json(blocker),
                _json(list(next_actions)),
                int(waiting_for_user),
                liveness,
                _json(self._workflow_metrics_locked(connection, workflow_id)),
                last_event_id,
                utc_now(),
            ),
        )

    def _liveness_locked(
        self,
        connection: sqlite3.Connection,
        workflow: AggregateSnapshot,
        snapshots: list[AggregateSnapshot],
        waiting_for_user: bool,
    ) -> str:
        if workflow.state in _TERMINAL_WORKFLOW_STATES:
            return "terminal"
        if workflow.state == "PAUSED":
            return "paused"
        if workflow.state == "TRIAGE_REQUIRED" or any(item.state == "TRIAGE_REQUIRED" for item in snapshots):
            return "operator_wait"
        if waiting_for_user:
            return "human_wait"
        now = utc_now()
        live_lease = connection.execute(
            """
            SELECT 1 FROM minion_v2_leases
            WHERE owner_id != '' AND expires_at > ?
              AND json_extract(metadata_json, '$.workflow_id') = ?
            LIMIT 1
            """,
            (now, workflow.workflow_id),
        ).fetchone()
        if live_lease is not None:
            return "live_lease"
        pending_effect = connection.execute(
            "SELECT 1 FROM minion_v2_outbox WHERE workflow_id = ? AND status IN ('pending', 'inflight') LIMIT 1",
            (workflow.workflow_id,),
        ).fetchone()
        if pending_effect is not None:
            return "outbox"
        # Queue admission is represented by the outbox entry written in the
        # same transaction as the state transition.  There is no independent
        # snapshot-scanning scheduler, so a bare queued state is an orphaned
        # workflow, not a source of liveness.
        return "orphaned"

    def _workflow_metrics_locked(self, connection: sqlite3.Connection, workflow_id: str) -> dict[str, Any]:
        worker = connection.execute(
            """
            SELECT
                COALESCE(SUM(total_input_tokens), 0) AS input_tokens,
                COALESCE(SUM(total_output_tokens), 0) AS output_tokens,
                COALESCE(SUM(total_cost), 0) AS cost,
                COALESCE(SUM(total_latency_ms), 0) AS llm_time_ms,
                COALESCE(SUM(total_tool_latency_ms), 0) AS tool_time_ms,
                COALESCE(SUM(total_wall_latency_ms), 0) AS worker_time_ms,
                COUNT(*) AS worker_invocations
            FROM minion_v2_worker_invocations
            WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        outbox = connection.execute(
            """
            SELECT COUNT(*) AS effect_count, COALESCE(SUM(attempt_count), 0) AS effect_attempts
            FROM minion_v2_outbox WHERE workflow_id = ?
            """,
            (workflow_id,),
        ).fetchone()
        review = connection.execute(
            """
            SELECT COALESCE(SUM(total_latency_ms), 0) AS review_time_ms
            FROM minion_v2_worker_invocations
            WHERE workflow_id = ? AND role LIKE '%review%'
            """,
            (workflow_id,),
        ).fetchone()
        return {
            "queue_time_ms": self._workflow_queue_time_locked(connection, workflow_id),
            "llm_time_ms": int(worker["llm_time_ms"]),
            "tool_time_ms": int(worker["tool_time_ms"]),
            "worker_time_ms": int(worker["worker_time_ms"]),
            "review_time_ms": int(review["review_time_ms"]),
            "input_tokens": int(worker["input_tokens"]),
            "output_tokens": int(worker["output_tokens"]),
            "cost": float(worker["cost"]),
            "worker_invocations": int(worker["worker_invocations"]),
            "effect_count": int(outbox["effect_count"]),
            "effect_attempts": int(outbox["effect_attempts"]),
        }

    def _workflow_queue_time_locked(self, connection: sqlite3.Connection, workflow_id: str) -> int:
        rows = connection.execute(
            """
            SELECT aggregate_type, aggregate_id, payload_json, created_at
            FROM minion_v2_domain_events
            WHERE workflow_id = ?
            ORDER BY created_at, event_id
            """,
            (workflow_id,),
        ).fetchall()
        queued_since: dict[tuple[str, str], datetime] = {}
        total_seconds = 0.0
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            state = str(payload.get("target_state") or "")
            key = (str(row["aggregate_type"]), str(row["aggregate_id"]))
            created_at = _parse_datetime(str(row["created_at"]))
            if state in _QUEUED_STATES:
                queued_since.setdefault(key, created_at)
                continue
            started = queued_since.pop(key, None)
            if started is not None:
                total_seconds += max(0.0, (created_at - started).total_seconds())
        now = _utc_datetime()
        total_seconds += sum(max(0.0, (now - started).total_seconds()) for started in queued_since.values())
        return int(total_seconds * 1000)

    def _assert_lease_locked(
        self,
        connection: sqlite3.Connection,
        resource_key: str,
        owner_id: str,
        fencing_token: int,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM minion_v2_leases WHERE resource_key = ?",
            (resource_key,),
        ).fetchone()
        self._assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=_utc_datetime())

    @staticmethod
    def _assert_lease_row(
        row: sqlite3.Row | None,
        *,
        owner_id: str,
        fencing_token: int,
        now: datetime | None,
    ) -> None:
        if row is None:
            raise StaleFencingToken("lease does not exist")
        if str(row["owner_id"]) != str(owner_id) or int(row["fencing_token"]) != int(fencing_token):
            raise StaleFencingToken("worker fencing token is stale")
        if now is not None and _parse_datetime(str(row["expires_at"])) <= now:
            raise StaleFencingToken("worker lease has expired")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


def _snapshot_from_row(row: sqlite3.Row) -> AggregateSnapshot:
    return AggregateSnapshot(
        aggregate_type=AggregateType(str(row["aggregate_type"])),
        aggregate_id=str(row["aggregate_id"]),
        workflow_id=str(row["workflow_id"]),
        state=str(row["state"]),
        version=int(row["version"]),
        payload=json.loads(str(row["payload_json"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _action_request_payload(action: ActionEnvelope) -> dict[str, Any]:
    return {
        "action_type": action.action_type,
        "workflow_id": action.workflow_id,
        "aggregate_type": action.aggregate_type.value,
        "aggregate_id": action.aggregate_id,
        "actor": action.actor,
        "source_channel": action.source_channel,
        "payload": dict(action.payload),
        "correlation_id": action.correlation_id,
        "causation_id": action.causation_id,
    }


def _encode_dispatch_result(result: DispatchResult) -> dict[str, Any]:
    return {
        "snapshot": {
            **asdict(result.snapshot),
            "aggregate_type": result.snapshot.aggregate_type.value,
            "payload": dict(result.snapshot.payload),
        },
        "events": [
            {
                **asdict(event),
                "aggregate_type": event.aggregate_type.value,
                "payload": dict(event.payload),
            }
            for event in result.events
        ],
        "outbox_effect_ids": list(result.outbox_effect_ids),
    }


def _decode_dispatch_result(value: Mapping[str, Any], *, duplicate: bool) -> DispatchResult:
    snapshot_data = dict(value.get("snapshot") or {})
    snapshot = AggregateSnapshot(
        aggregate_type=AggregateType(str(snapshot_data["aggregate_type"])),
        aggregate_id=str(snapshot_data["aggregate_id"]),
        workflow_id=str(snapshot_data["workflow_id"]),
        state=str(snapshot_data["state"]),
        version=int(snapshot_data["version"]),
        payload=dict(snapshot_data.get("payload") or {}),
        created_at=str(snapshot_data["created_at"]),
        updated_at=str(snapshot_data["updated_at"]),
    )
    events = tuple(
        DomainEvent(
            event_id=str(item["event_id"]),
            workflow_id=str(item["workflow_id"]),
            aggregate_type=AggregateType(str(item["aggregate_type"])),
            aggregate_id=str(item["aggregate_id"]),
            aggregate_version=int(item["aggregate_version"]),
            event_type=str(item["event_type"]),
            payload=dict(item.get("payload") or {}),
            action_id=str(item["action_id"]),
            correlation_id=str(item["correlation_id"]),
            causation_id=str(item["causation_id"]),
            created_at=str(item["created_at"]),
        )
        for item in list(value.get("events") or [])
    )
    return DispatchResult(
        snapshot=snapshot,
        events=events,
        outbox_effect_ids=tuple(str(item) for item in list(value.get("outbox_effect_ids") or [])),
        duplicate=duplicate,
    )


def _artifact_digests(value: Any) -> set[str]:
    digests: set[str] = set()
    if isinstance(value, Mapping):
        raw_digest = value.get("sha256")
        if isinstance(raw_digest, str) and len(raw_digest.removeprefix("sha256:")) == 64:
            digests.add(raw_digest.removeprefix("sha256:"))
        for item in value.values():
            digests.update(_artifact_digests(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            digests.update(_artifact_digests(item))
    elif isinstance(value, str) and value.startswith("sha256:") and len(value) == 71:
        digests.add(value.removeprefix("sha256:"))
    return digests


def _manifest_sha_from_action(action: ActionEnvelope) -> str:
    value = action.payload.get("architecture_manifest_ref") or action.payload.get("clarification_ref")
    if isinstance(value, Mapping):
        return str(value.get("sha256") or "").removeprefix("sha256:")
    return str(value or action.payload.get("manifest_sha") or "").removeprefix("sha256:")


def _active_projection_snapshot(
    snapshots: list[AggregateSnapshot],
    workflow: AggregateSnapshot,
) -> AggregateSnapshot | None:
    if workflow.state in _TERMINAL_WORKFLOW_STATES | {"PAUSED", "PAUSE_REQUESTED", "CANCEL_REQUESTED", "TRIAGE_REQUIRED"}:
        return workflow
    execution_id = str(workflow.payload.get("execution_epoch_id") or "")
    architecture_id = str(workflow.payload.get("architecture_revision_id") or "")
    if execution_id:
        match = next((item for item in snapshots if item.aggregate_id == execution_id), None)
        if match is not None:
            return match
    if architecture_id:
        match = next((item for item in snapshots if item.aggregate_id == architecture_id), None)
        if match is not None:
            return match
    children = [item for item in snapshots if item.aggregate_type != AggregateType.WORKFLOW]
    return children[0] if children else workflow


def _current_phase(workflow: AggregateSnapshot, active: AggregateSnapshot | None) -> str:
    if workflow.state in _TERMINAL_WORKFLOW_STATES | {"PAUSED", "PAUSE_REQUESTED", "CANCEL_REQUESTED", "TRIAGE_REQUIRED"}:
        return workflow.state.lower()
    if active is None or active.aggregate_type == AggregateType.WORKFLOW:
        return "created" if workflow.state == "CREATED" else "routing"
    if active.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
        state = active.state
        if state.startswith("ARCHITECT") or state == "CLARIFICATION_PENDING":
            return "architecture"
        if state in {"REVIEW_QUEUED", "REVIEWING"}:
            return "architecture_review"
        if state in {"HUMAN_REVIEW", "REVISION_PENDING"}:
            return "human_review"
        return f"architecture_{state.lower()}"
    if active.aggregate_type == AggregateType.EXECUTION_EPOCH:
        return "finalizing" if active.state == "FINALIZING" else "executing"
    if active.aggregate_type == AggregateType.DAG_NODE_RUN:
        return "executing"
    return "standalone_review"


def _decode_json_columns(row: sqlite3.Row, columns: Mapping[str, str]) -> dict[str, Any]:
    result = dict(row)
    for source, target in columns.items():
        result[target] = json.loads(str(result.pop(source)))
    result["waiting_for_user"] = bool(result.get("waiting_for_user"))
    return result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _utc_datetime() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
