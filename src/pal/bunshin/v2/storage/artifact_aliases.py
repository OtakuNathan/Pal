from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any
from pal.foundation import utc_now
from pal.bunshin.v2.storage.connection_contracts import DatabasePort


@dataclass
class ArtifactAliasesStore:
    database: DatabasePort

    def bind_artifact_alias(
        self,
        *,
        actor_id: str,
        alias: str,
        artifact_sha256: str,
    ) -> None:
        self.database.ensure_schema()
        actor = str(actor_id or "").strip()
        name = str(alias or "").strip()
        digest = str(artifact_sha256 or "").removeprefix("sha256:")
        if not actor or not name or not digest:
            raise ValueError("artifact alias requires actor, name, and artifact")
        with self.database.write_connection() as connection:
            row = connection.execute(
                "SELECT durable FROM bunshin_v2_artifacts WHERE sha256 = ?",
                (digest,),
            ).fetchone()
            if row is None or int(row["durable"]) != 1:
                raise ValueError("artifact alias requires a durable artifact")
            connection.execute(
                """
                INSERT INTO bunshin_v2_artifact_aliases(actor_id, alias, artifact_sha256, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(actor_id, alias) DO UPDATE SET
                    artifact_sha256 = excluded.artifact_sha256,
                    updated_at = excluded.updated_at
                """,
                (actor, name, digest, utc_now()),
            )

    def resolve_artifact_alias(self, *, actor_id: str, alias: str) -> dict[str, Any] | None:
        self.database.ensure_schema()
        with self.database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT a.* FROM bunshin_v2_artifacts AS a
                JOIN bunshin_v2_artifact_aliases AS x ON x.artifact_sha256 = a.sha256
                WHERE x.actor_id = ? AND x.alias = ?
                """,
                (str(actor_id or "").strip(), str(alias or "").strip()),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["durable"] = bool(result["durable"])
        result["provenance"] = json.loads(str(result.pop("provenance_json")))
        result["metadata"] = json.loads(str(result.pop("metadata_json")))
        return result
