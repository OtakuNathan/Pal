from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pal.foundation.persistence import utc_now


@dataclass(frozen=True)
class StoredTelegramInteraction:
    interaction_id: str
    interaction_kind: str
    target: dict[str, Any]
    actions: dict[str, dict[str, Any]]
    expires_at: str | None
    state: str


class TelegramInteractionStore:
    """Provider-owned durable projection for Telegram callback keyboards."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.database_path = self.data_root / "state.sqlite3"
        self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS interactions (
                    interaction_id TEXT PRIMARY KEY,
                    interaction_kind TEXT NOT NULL,
                    target_json TEXT NOT NULL,
                    actions_json TEXT NOT NULL,
                    expires_at TEXT,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        os.chmod(self.database_path, 0o600)

    def put_open(
        self,
        *,
        interaction_id: str,
        interaction_kind: str,
        target: dict[str, Any],
        actions: dict[str, dict[str, Any]],
        expires_at: str | None,
    ) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO interactions (
                    interaction_id, interaction_kind, target_json, actions_json,
                    expires_at, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
                ON CONFLICT(interaction_id) DO UPDATE SET
                    interaction_kind = excluded.interaction_kind,
                    target_json = excluded.target_json,
                    actions_json = excluded.actions_json,
                    expires_at = excluded.expires_at,
                    state = 'open',
                    updated_at = excluded.updated_at
                """,
                (
                    interaction_id,
                    interaction_kind,
                    _dump_json(target),
                    _dump_json(actions),
                    expires_at,
                    now,
                    now,
                ),
            )

    def get_open(self, interaction_id: str) -> StoredTelegramInteraction | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT interaction_id, interaction_kind, target_json, actions_json,
                       expires_at, state
                FROM interactions
                WHERE interaction_id = ? AND state = 'open'
                """,
                (str(interaction_id),),
            ).fetchone()
        if row is None:
            return None
        return StoredTelegramInteraction(
            interaction_id=str(row["interaction_id"]),
            interaction_kind=str(row["interaction_kind"]),
            target=_load_mapping(row["target_json"]),
            actions={
                str(key): dict(value)
                for key, value in _load_mapping(row["actions_json"]).items()
                if isinstance(value, dict)
            },
            expires_at=str(row["expires_at"]) if row["expires_at"] else None,
            state=str(row["state"]),
        )

    def list_open(
        self,
        *,
        interaction_kind: str | None = None,
    ) -> list[StoredTelegramInteraction]:
        normalized_kind = str(interaction_kind or "").strip()
        query = (
            """
            SELECT interaction_id, interaction_kind, target_json, actions_json,
                   expires_at, state
            FROM interactions
            WHERE state = 'open' AND interaction_kind = ?
            ORDER BY created_at ASC
            """
            if normalized_kind
            else """
            SELECT interaction_id, interaction_kind, target_json, actions_json,
                   expires_at, state
            FROM interactions
            WHERE state = 'open'
            ORDER BY created_at ASC
            """
        )
        params = (normalized_kind,) if normalized_kind else ()
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            StoredTelegramInteraction(
                interaction_id=str(row["interaction_id"]),
                interaction_kind=str(row["interaction_kind"]),
                target=_load_mapping(row["target_json"]),
                actions={
                    str(key): dict(value)
                    for key, value in _load_mapping(row["actions_json"]).items()
                    if isinstance(value, dict)
                },
                expires_at=(
                    str(row["expires_at"])
                    if row["expires_at"]
                    else None
                ),
                state=str(row["state"]),
            )
            for row in rows
        ]

    def set_state(self, interaction_id: str, state: str) -> None:
        normalized = str(state or "").strip()
        if normalized not in {"resolved", "superseded", "expired"}:
            raise ValueError(f"unsupported Telegram interaction state: {normalized}")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE interactions
                SET state = ?, updated_at = ?
                WHERE interaction_id = ?
                """,
                (normalized, utc_now(), str(interaction_id)),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection


def _dump_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_mapping(raw: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}
