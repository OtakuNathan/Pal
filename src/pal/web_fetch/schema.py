from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WebFetchSchemaMigrationResult:
    status: str
    removed_rows: int = 0
    archived_rows: int = 0
    archive_path: str = ""


def migrate_web_fetch_schema(db_path: Path) -> WebFetchSchemaMigrationResult:
    """Remove the retired provider family outside plugin attach/runtime startup."""

    path = Path(db_path)
    if not path.is_file():
        return WebFetchSchemaMigrationResult(status="new_database")
    database = sqlite3.connect(path)
    database.row_factory = sqlite3.Row
    try:
        exists = database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_fetch_providers'"
        ).fetchone()
        settings_exists = database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pal_runtime_settings'"
        ).fetchone()
        legacy_setting = (
            database.execute(
                "SELECT 1 FROM pal_runtime_settings WHERE setting_key = ?",
                ("active_web_fetch_provider_id",),
            ).fetchone()
            if settings_exists
            else None
        )
        rows: list[dict[str, Any]] = []
        if exists:
            rows = [dict(row) for row in database.execute("SELECT * FROM web_fetch_providers ORDER BY provider_id")]
        custom = [
            row
            for row in rows
            if str(row.get("provider_id") or "")
            not in {"playwright_fetch_default", "plain_http_fetch_default"}
        ]
        archive_path = ""
        if custom:
            target = path.parent / "data" / "web_fetch" / "legacy_provider_backup.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(
                json.dumps({"providers": custom}, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            os.replace(temporary, target)
            archive_path = str(target)
        database.execute("BEGIN IMMEDIATE")
        try:
            if exists:
                database.execute("DROP TABLE web_fetch_providers")
            if settings_exists:
                database.execute(
                    "DELETE FROM pal_runtime_settings WHERE setting_key = ?",
                    ("active_web_fetch_provider_id",),
                )
            database.commit()
        except Exception:
            database.rollback()
            raise
        return WebFetchSchemaMigrationResult(
            status="migrated" if exists or legacy_setting else "current",
            removed_rows=len(rows),
            archived_rows=len(custom),
            archive_path=archive_path,
        )
    finally:
        database.close()


__all__ = ["WebFetchSchemaMigrationResult", "migrate_web_fetch_schema"]
