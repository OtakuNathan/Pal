from __future__ import annotations
from pal.bunshin.storage.serialization import _json
from pal.bunshin.storage.serialization import _utc_datetime
from pal.bunshin.storage.serialization import _parse_datetime
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping
from pal.foundation import utc_now
from pal.bunshin.contracts import LeaseConflict, LeaseGrant, StaleFencingToken
from pal.bunshin.storage.connection_contracts import DatabasePort


@dataclass
class LeasesStore:
    database: DatabasePort

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
        self.database.ensure_schema()
        now = _utc_datetime()
        expires_at = now + timedelta(seconds=max(1, ttl_seconds))
        with self.database.write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            if row is not None and str(row["owner_id"]) and _parse_datetime(str(row["expires_at"])) > now:
                raise LeaseConflict(f"resource already leased by {row['owner_id']}")
            fencing_token = (int(row["fencing_token"]) if row is not None else 0) + 1
            connection.execute(
                """
                INSERT INTO bunshin_v2_leases(
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
        self.database.ensure_schema()
        now = _utc_datetime()
        expires_at = now + timedelta(seconds=max(1, ttl_seconds))
        with self.database.write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            self.assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=now)
            connection.execute(
                "UPDATE bunshin_v2_leases SET renewed_at = ?, expires_at = ? WHERE resource_key = ?",
                (now.isoformat(), expires_at.isoformat(), resource_key),
            )
            return LeaseGrant(resource_key, owner_id, fencing_token, str(row["acquired_at"]), expires_at.isoformat())

    def assert_fencing_token(self, resource_key: str, owner_id: str, fencing_token: int) -> None:
        self.database.ensure_schema()
        now = _utc_datetime()
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            self.assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=now)

    def release_lease(self, resource_key: str, owner_id: str, fencing_token: int) -> None:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            self.assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=None)
            connection.execute(
                "UPDATE bunshin_v2_leases SET owner_id = '', renewed_at = ?, expires_at = ? WHERE resource_key = ?",
                (utc_now(), utc_now(), resource_key),
            )

    def expired_leases(self) -> tuple[dict[str, Any], ...]:
        self.database.ensure_schema()
        now = utc_now()
        with self.database.read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM bunshin_v2_leases WHERE owner_id != '' AND expires_at <= ? ORDER BY expires_at",
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
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_leases WHERE resource_key = ?",
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
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_leases WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            self.assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=_utc_datetime())
            connection.execute(
                "UPDATE bunshin_v2_leases SET metadata_json = ?, renewed_at = ? WHERE resource_key = ?",
                (_json(dict(metadata)), utc_now(), resource_key),
            )

    def clear_expired_lease(self, resource_key: str, fencing_token: int) -> bool:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE bunshin_v2_leases
                SET owner_id = '', renewed_at = ?, expires_at = ?
                WHERE resource_key = ? AND fencing_token = ? AND expires_at <= ?
                """,
                (utc_now(), utc_now(), resource_key, int(fencing_token), utc_now()),
            )
            return cursor.rowcount == 1

    def assert_lease_locked(
        self,
        connection: sqlite3.Connection,
        resource_key: str,
        owner_id: str,
        fencing_token: int,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM bunshin_v2_leases WHERE resource_key = ?",
            (resource_key,),
        ).fetchone()
        self.assert_lease_row(row, owner_id=owner_id, fencing_token=fencing_token, now=_utc_datetime())

    @staticmethod
    def assert_lease_row(
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
