from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _utc_datetime
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from pal.bunshin.v2.contracts import LeaseConflict
from pal.bunshin.v2.storage.connection_contracts import DatabasePort


@dataclass
class OutboxClaimsStore:
    database: DatabasePort

    def claim_outbox(self, worker_id: str, *, limit: int = 20, lease_seconds: int = 60) -> tuple[dict[str, Any], ...]:
        self.database.ensure_schema()
        now = _utc_datetime()
        now_text = now.isoformat()
        locked_until = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
        with self.database.write_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bunshin_v2_outbox
                WHERE next_retry_at <= ?
                  AND (
                    (status = 'pending' AND attempt_count < max_attempts)
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
                    UPDATE bunshin_v2_outbox
                    SET status = 'inflight',
                        attempt_count = CASE
                            WHEN status = 'pending' THEN attempt_count + 1
                            ELSE attempt_count
                        END,
                        locked_by = ?, locked_until = ?, updated_at = ?
                    WHERE effect_id = ?
                      AND (
                        (status = 'pending' AND attempt_count < max_attempts)
                        OR (status = 'inflight' AND locked_until <= ?)
                      )
                    """,
                    (worker_id, locked_until, now_text, str(row["effect_id"]), now_text),
                )
                if cursor.rowcount == 1:
                    if str(row["status"]) == "inflight":
                        connection.execute(
                            """
                            UPDATE bunshin_v2_effect_attempts
                            SET status = 'lost', error_kind = 'lease_expired',
                                error_text = 'outbox claim expired before settlement',
                                finished_at = ?
                            WHERE effect_id = ? AND status = 'running'
                            """,
                            (now_text, str(row["effect_id"])),
                        )
                    attempt_row = connection.execute(
                        """
                        SELECT COALESCE(MAX(attempt_index), 0) + 1 AS next_attempt_index
                        FROM bunshin_v2_effect_attempts
                        WHERE effect_id = ?
                        """,
                        (str(row["effect_id"]),),
                    ).fetchone()
                    effect_attempt_index = int(attempt_row["next_attempt_index"])
                    connection.execute(
                        """
                        INSERT INTO bunshin_v2_effect_attempts(
                            effect_id, attempt_index, worker_id, status, started_at
                        ) VALUES (?, ?, ?, 'running', ?)
                        """,
                        (
                            str(row["effect_id"]),
                            effect_attempt_index,
                            str(worker_id),
                            now_text,
                        ),
                    )
                    attempt_count = int(row["attempt_count"])
                    if str(row["status"]) == "pending":
                        attempt_count += 1
                    item = dict(row)
                    item.update(
                        {
                            "status": "inflight",
                            "attempt_count": attempt_count,
                            "effect_attempt_index": effect_attempt_index,
                            "claim_incremented_attempt": str(row["status"]) == "pending",
                            "locked_by": worker_id,
                            "locked_until": locked_until,
                            "payload": json.loads(str(row["payload_json"])),
                        }
                    )
                    claimed.append(item)
            return tuple(claimed)

    def renew_outbox_claim(self, effect_id: str, *, worker_id: str, lease_seconds: int = 60) -> None:
        self.database.ensure_schema()
        now = _utc_datetime()
        locked_until = now + timedelta(seconds=max(1, lease_seconds))
        with self.database.write_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE bunshin_v2_outbox
                SET locked_until = ?, updated_at = ?
                WHERE effect_id = ? AND status = 'inflight' AND locked_by = ?
                """,
                (locked_until.isoformat(), now.isoformat(), str(effect_id), str(worker_id)),
            )
            if cursor.rowcount != 1:
                raise LeaseConflict("outbox effect is not claimed by this worker")

    def list_effect_attempts(self, effect_id: str) -> tuple[dict[str, Any], ...]:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bunshin_v2_effect_attempts
                WHERE effect_id = ?
                ORDER BY attempt_index
                """,
                (str(effect_id),),
            ).fetchall()
            return tuple(
                {
                    **dict(row),
                    "result_artifact_ref": json.loads(str(row["result_artifact_ref_json"])),
                }
                for row in rows
            )
