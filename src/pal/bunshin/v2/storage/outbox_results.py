from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _json
from pal.bunshin.v2.storage.serialization import _utc_datetime
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping
from pal.foundation import utc_now
from pal.bunshin.v2.contracts import ActionEnvelope, DispatchResult, LeaseConflict
from pal.bunshin.v2.storage.artifacts import ArtifactsStore
from pal.bunshin.v2.storage.connection_contracts import DatabasePort
from pal.bunshin.v2.storage.transitions import TransitionsStore


@dataclass
class OutboxResultsStore:
    artifacts: ArtifactsStore
    database: DatabasePort
    transitions: TransitionsStore

    def complete_outbox_effect(
        self,
        effect_id: str,
        *,
        worker_id: str,
        provider_request_id: str = "",
        result_artifact_ref: Mapping[str, Any] | None = None,
    ) -> bool:
        self.database.ensure_schema()
        result_ref = dict(result_artifact_ref or {})
        now = utc_now()
        with self.database.write_connection() as connection:
            self.artifacts.assert_artifact_refs_durable(connection, result_ref)
            row = connection.execute(
                "SELECT * FROM bunshin_v2_outbox WHERE effect_id = ?",
                (str(effect_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown outbox effect: {effect_id}")
            receipt = connection.execute(
                "SELECT request_hash FROM bunshin_v2_effect_receipts WHERE effect_key = ?",
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
                INSERT INTO bunshin_v2_effect_receipts(
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
                UPDATE bunshin_v2_outbox
                SET status = 'completed', provider_request_id = ?, result_artifact_ref_json = ?,
                    locked_by = '', locked_until = '', last_error = '', updated_at = ?
                WHERE effect_id = ?
                """,
                (str(provider_request_id), _json(result_ref), now, str(effect_id)),
            )
            self.finish_effect_attempt_locked(
                connection,
                effect_id=str(effect_id),
                worker_id=str(worker_id),
                status="completed",
                provider_request_id=str(provider_request_id),
                result_artifact_ref=result_ref,
                finished_at=now,
            )
            return True

    def retry_outbox_effect(
        self,
        effect_id: str,
        *,
        worker_id: str,
        error: str,
        retry_after_seconds: int = 5,
        triage_action: ActionEnvelope | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> str:
        if _connection is None:
            self.database.ensure_schema()
        now = _utc_datetime()
        transaction = self.database.write_connection() if _connection is None else nullcontext(_connection)
        with transaction as connection:
            row = connection.execute(
                "SELECT status, locked_by, attempt_count, max_attempts FROM bunshin_v2_outbox WHERE effect_id = ?",
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
                UPDATE bunshin_v2_outbox
                SET status = ?, next_retry_at = ?, locked_by = '', locked_until = '',
                    last_error = ?, updated_at = ?
                WHERE effect_id = ?
                """,
                (status, next_retry_at, str(error), now.isoformat(), str(effect_id)),
            )
            self.finish_effect_attempt_locked(
                connection,
                effect_id=str(effect_id),
                worker_id=str(worker_id),
                status="failed" if exhausted else "retryable",
                error_kind="effect_failed" if exhausted else "effect_retry",
                error_text=str(error),
                finished_at=now.isoformat(),
            )
            if exhausted and triage_action is not None:
                self.transitions.dispatch(triage_action, _connection=connection)
            return status

    def defer_outbox_effect(
        self,
        effect_id: str,
        *,
        worker_id: str,
        reason: str,
        attempt_was_incremented: bool = True,
    ) -> None:
        """Return a claimed effect to the queue without spending a retry attempt."""

        self.database.ensure_schema()
        now = utc_now()
        with self.database.write_connection() as connection:
            row = connection.execute(
                "SELECT status, locked_by, attempt_count FROM bunshin_v2_outbox WHERE effect_id = ?",
                (str(effect_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown outbox effect: {effect_id}")
            if str(row["status"]) != "inflight" or str(row["locked_by"]) != str(worker_id):
                raise LeaseConflict("outbox effect is not claimed by this worker")
            connection.execute(
                """
                UPDATE bunshin_v2_outbox
                SET status = 'pending', attempt_count = ?, next_retry_at = ?,
                    locked_by = '', locked_until = '', last_error = ?, updated_at = ?
                WHERE effect_id = ?
                """,
                (
                    max(
                        0,
                        int(row["attempt_count"]) - (1 if attempt_was_incremented else 0),
                    ),
                    now,
                    str(reason),
                    now,
                    str(effect_id),
                ),
            )
            self.finish_effect_attempt_locked(
                connection,
                effect_id=str(effect_id),
                worker_id=str(worker_id),
                status="deferred",
                error_kind="manager_deferred",
                error_text=str(reason),
                finished_at=now,
            )

    def fail_outbox_effect(
        self,
        effect_id: str,
        *,
        worker_id: str,
        error: str,
        triage_action: ActionEnvelope | None = None,
        _connection: sqlite3.Connection | None = None,
    ) -> DispatchResult | None:
        if _connection is None:
            self.database.ensure_schema()
        now = utc_now()
        transaction = self.database.write_connection() if _connection is None else nullcontext(_connection)
        with transaction as connection:
            row = connection.execute(
                "SELECT status, locked_by FROM bunshin_v2_outbox WHERE effect_id = ?",
                (str(effect_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown outbox effect: {effect_id}")
            if str(row["status"]) != "inflight" or str(row["locked_by"]) != str(worker_id):
                raise LeaseConflict("outbox effect is not claimed by this worker")
            connection.execute(
                """
                UPDATE bunshin_v2_outbox
                SET status = 'failed', next_retry_at = ?, locked_by = '', locked_until = '',
                    last_error = ?, updated_at = ?
                WHERE effect_id = ?
                """,
                (now, str(error), now, str(effect_id)),
            )
            self.finish_effect_attempt_locked(
                connection,
                effect_id=str(effect_id),
                worker_id=str(worker_id),
                status="failed",
                error_kind="effect_failed",
                error_text=str(error),
                finished_at=now,
            )
            if triage_action is not None:
                return self.transitions.dispatch(triage_action, _connection=connection)
            return None

    @staticmethod
    def finish_effect_attempt_locked(
        connection: sqlite3.Connection,
        *,
        effect_id: str,
        worker_id: str,
        status: str,
        error_kind: str = "",
        error_text: str = "",
        provider_request_id: str = "",
        result_artifact_ref: Mapping[str, Any] | None = None,
        finished_at: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT attempt_index FROM bunshin_v2_effect_attempts
            WHERE effect_id = ? AND worker_id = ? AND status = 'running'
            ORDER BY attempt_index DESC
            LIMIT 1
            """,
            (str(effect_id), str(worker_id)),
        ).fetchone()
        if row is None:
            raise LeaseConflict("outbox effect has no active attempt for this worker")
        connection.execute(
            """
            UPDATE bunshin_v2_effect_attempts
            SET status = ?, error_kind = ?, error_text = ?,
                provider_request_id = ?, result_artifact_ref_json = ?, finished_at = ?
            WHERE effect_id = ? AND attempt_index = ? AND status = 'running'
            """,
            (
                str(status),
                str(error_kind),
                str(error_text),
                str(provider_request_id),
                _json(dict(result_artifact_ref or {})),
                str(finished_at),
                str(effect_id),
                int(row["attempt_index"]),
            ),
        )
