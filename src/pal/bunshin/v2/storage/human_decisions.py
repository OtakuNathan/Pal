from __future__ import annotations
from pal.bunshin.v2.storage.serialization import _manifest_sha_from_action
from pal.bunshin.v2.storage.serialization import _utc_datetime
import hashlib
import sqlite3
import secrets
from dataclasses import dataclass
from typing import Any
from pal.bunshin.v2.contracts import ActionEnvelope
from pal.bunshin.v2.storage.connection_contracts import DatabasePort


@dataclass
class HumanDecisionsStore:
    database: DatabasePort

    def issue_human_decision_token(
        self,
        *,
        workflow_id: str,
        architecture_revision_id: str,
        manifest_sha: str,
        actor_id: str,
    ) -> str:
        required = {
            "workflow_id": workflow_id,
            "architecture_revision_id": architecture_revision_id,
            "manifest_sha": manifest_sha,
            "actor_id": actor_id,
        }
        missing = [key for key, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"human decision token missing fields: {', '.join(missing)}")
        self.database.ensure_schema()
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = _utc_datetime()
        with self.database.write_connection() as connection:
            artifact = connection.execute(
                "SELECT durable FROM bunshin_v2_artifacts WHERE sha256 = ?",
                (str(manifest_sha).removeprefix("sha256:"),),
            ).fetchone()
            if artifact is None or int(artifact["durable"]) != 1:
                raise ValueError("human decision token requires a durable architecture manifest")
            connection.execute(
                """
                UPDATE bunshin_v2_human_decisions
                SET status = 'expired'
                WHERE workflow_id = ? AND architecture_revision_id = ? AND status = 'issued'
                """,
                (workflow_id, architecture_revision_id),
            )
            connection.execute(
                """
                INSERT INTO bunshin_v2_human_decisions(
                    token_hash, workflow_id, architecture_revision_id, manifest_sha,
                    actor_id, expires_at, issued_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    workflow_id,
                    architecture_revision_id,
                    str(manifest_sha).removeprefix("sha256:"),
                    actor_id,
                    "",
                    now.isoformat(),
                ),
            )
        return token

    def inspect_human_decision_token(self, token: str) -> dict[str, Any] | None:
        self.database.ensure_schema()
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        with self.database.read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM bunshin_v2_human_decisions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result.pop("token_hash", None)
            return result

    def expire_human_decisions_for_revision(
        self,
        *,
        workflow_id: str,
        architecture_revision_id: str,
    ) -> int:
        self.database.ensure_schema()
        with self.database.write_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE bunshin_v2_human_decisions
                SET status = 'expired'
                WHERE workflow_id = ? AND architecture_revision_id = ? AND status = 'issued'
                """,
                (str(workflow_id), str(architecture_revision_id)),
            )
            return int(cursor.rowcount)

    def reissue_human_decision_token(
        self,
        *,
        workflow_id: str,
        actor_id: str,
    ) -> str:
        """Replace the unique pending card token for a semantic/manual decision path."""

        required = {
            "workflow_id": workflow_id,
            "actor_id": actor_id,
        }
        missing = [key for key, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"human decision binding missing fields: {', '.join(missing)}")
        self.database.ensure_schema()
        now = _utc_datetime()
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.database.write_connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM bunshin_v2_human_decisions
                WHERE workflow_id = ? AND actor_id = ? AND status = 'issued'
                ORDER BY issued_at DESC
                """,
                (str(workflow_id), str(actor_id)),
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
                "SELECT durable FROM bunshin_v2_artifacts WHERE sha256 = ?",
                (manifest_sha,),
            ).fetchone()
            if artifact is None or int(artifact["durable"]) != 1:
                raise ValueError("pending human decision references a non-durable artifact")
            connection.execute(
                """
                UPDATE bunshin_v2_human_decisions
                SET status = 'expired'
                WHERE workflow_id = ? AND architecture_revision_id = ? AND manifest_sha = ?
                  AND actor_id = ? AND status = 'issued'
                """,
                (
                    str(workflow_id),
                    architecture_revision_id,
                    manifest_sha,
                    str(actor_id),
                ),
            )
            connection.execute(
                """
                INSERT INTO bunshin_v2_human_decisions(
                    token_hash, workflow_id, architecture_revision_id, manifest_sha,
                    actor_id, expires_at, issued_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    str(workflow_id),
                    architecture_revision_id,
                    manifest_sha,
                    str(actor_id),
                    "",
                    now.isoformat(),
                ),
            )
        return token

    def consume_human_decision_locked(self, connection: sqlite3.Connection, action: ActionEnvelope) -> None:
        if action.action_type not in {"HUMAN_ACCEPT", "HUMAN_EDIT", "HUMAN_REJECT"}:
            return
        token = str(action.payload.get("decision_token") or "")
        if not token:
            raise ValueError("human decision action requires decision_token")
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        row = connection.execute(
            "SELECT * FROM bunshin_v2_human_decisions WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown human decision token")
        if str(row["status"]) != "issued":
            raise ValueError("human decision token is stale or already consumed")
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
        if mismatches:
            raise ValueError(f"stale human decision binding: {', '.join(mismatches)}")
        cursor = connection.execute(
            """
            UPDATE bunshin_v2_human_decisions
            SET status = 'consumed', decision = ?, action_id = ?, consumed_at = ?
            WHERE token_hash = ? AND status = 'issued'
            """,
            (action.action_type, action.action_id, action.created_at, token_hash),
        )
        if cursor.rowcount != 1:
            raise ValueError("human decision token was consumed concurrently")
