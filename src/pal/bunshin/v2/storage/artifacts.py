from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _artifact_digests
from pal.bunshin.v2.storage.serialization import _json
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.foundation import utc_now
from pal.bunshin.v2.artifacts import ArtifactRef
from pal.bunshin.v2.contracts import AggregateType
from pal.bunshin.v2.storage.connection_contracts import DatabasePort


@dataclass
class ArtifactsStore:
    database: DatabasePort

    def record_artifact(
        self,
        ref: ArtifactRef,
        *,
        storage_path: Path,
        provenance: Mapping[str, Any],
        metadata: Mapping[str, Any],
        child_refs: tuple[tuple[str, str], ...],
    ) -> None:
        self.database.ensure_schema()
        if not storage_path.is_file():
            raise FileNotFoundError(storage_path)
        if storage_path.stat().st_size != ref.byte_size:
            raise IOError("artifact size changed before metadata publication")
        now = utc_now()
        with self.database.write_connection() as connection:
            for child_sha, _relation in child_refs:
                child = connection.execute(
                    "SELECT durable FROM bunshin_v2_artifacts WHERE sha256 = ?",
                    (str(child_sha),),
                ).fetchone()
                if child is None or int(child["durable"]) != 1:
                    raise ValueError(f"child artifact is not durable: {child_sha}")
            existing = connection.execute(
                "SELECT artifact_type, schema_version, media_type, byte_size, storage_path FROM bunshin_v2_artifacts WHERE sha256 = ?",
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
                INSERT INTO bunshin_v2_artifacts(
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
                    INSERT OR IGNORE INTO bunshin_v2_artifact_refs(parent_sha256, child_sha256, relation, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (ref.sha256, str(child_sha), str(relation), now),
                )

    def artifact_is_durable(self, sha256: str) -> bool:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT durable, storage_path FROM bunshin_v2_artifacts WHERE sha256 = ?",
                (str(sha256).removeprefix("sha256:"),),
            ).fetchone()
            return bool(row is not None and int(row["durable"]) == 1 and Path(str(row["storage_path"])).is_file())

    def read_artifact_record(self, sha256: str) -> dict[str, Any] | None:
        self.database.ensure_schema()
        digest = str(sha256).removeprefix("sha256:")
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_artifacts WHERE sha256 = ?",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["durable"] = bool(result["durable"])
            result["provenance"] = json.loads(str(result.pop("provenance_json")))
            result["metadata"] = json.loads(str(result.pop("metadata_json")))
            return result

    def read_latest_effect_result_artifact(
        self,
        *,
        workflow_id: str,
        aggregate_type: AggregateType,
        aggregate_id: str,
        effect_type: str,
    ) -> dict[str, Any] | None:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT result_artifact_ref_json
                FROM bunshin_v2_outbox
                WHERE workflow_id = ? AND aggregate_type = ? AND aggregate_id = ?
                  AND effect_type = ? AND status = 'completed'
                  AND result_artifact_ref_json != '{}'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (str(workflow_id), aggregate_type.value, str(aggregate_id), str(effect_type)),
            ).fetchone()
            if row is None:
                return None
            value = json.loads(str(row["result_artifact_ref_json"] or "{}"))
            return dict(value) if isinstance(value, dict) and value.get("sha256") else None

    def assert_artifact_refs_durable(self, connection: sqlite3.Connection, value: Any) -> None:
        for digest in _artifact_digests(value):
            row = connection.execute(
                "SELECT durable, storage_path FROM bunshin_v2_artifacts WHERE sha256 = ?",
                (digest,),
            ).fetchone()
            if row is None or int(row["durable"]) != 1:
                raise ValueError(f"action references a missing or non-durable artifact: {digest}")
            if not Path(str(row["storage_path"])).is_file():
                raise ValueError(f"action references an artifact with missing storage: {digest}")
