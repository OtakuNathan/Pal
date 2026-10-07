from __future__ import annotations
from pal.bunshin.storage.serialization import _snapshot_from_row
from pal.bunshin.storage.serialization import _json
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from pal.bunshin.contracts import AggregateSnapshot, AggregateType, AggregateVersionConflict
from pal.bunshin.storage.connection_contracts import DatabasePort


@dataclass
class SnapshotsStore:
    database: DatabasePort

    def read_snapshot(
        self,
        aggregate_type: AggregateType,
        aggregate_id: str,
        *,
        _connection: sqlite3.Connection | None = None,
    ) -> AggregateSnapshot | None:
        if _connection is None:
            self.database.ensure_schema()
        connection_scope = self.database.read_connection() if _connection is None else nullcontext(_connection)
        with connection_scope as connection:
            return self.read_snapshot_locked(connection, aggregate_type, aggregate_id)

    def read_snapshot_locked(
        self,
        connection: sqlite3.Connection,
        aggregate_type: AggregateType,
        aggregate_id: str,
    ) -> AggregateSnapshot | None:
        row = connection.execute(
            "SELECT * FROM bunshin_v2_aggregate_snapshots WHERE aggregate_type = ? AND aggregate_id = ?",
            (aggregate_type.value, aggregate_id),
        ).fetchone()
        return _snapshot_from_row(row) if row is not None else None

    def write_snapshot_locked(
        self,
        connection: sqlite3.Connection,
        current: AggregateSnapshot | None,
        snapshot: AggregateSnapshot,
    ) -> None:
        if current is None:
            try:
                connection.execute(
                    """
                    INSERT INTO bunshin_v2_aggregate_snapshots(
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
            UPDATE bunshin_v2_aggregate_snapshots
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
