from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from pal.foundation.persistence import BaseModel


MEMORY_DOCUMENT_PROJECTION_SQL = """
CREATE VIEW IF NOT EXISTS memory_document_projection AS
SELECT
    'fact:' || fact_id AS document_id,
    'fact' AS document_kind,
    scope AS scope,
    task_id AS task_id,
    COALESCE(title, '') AS title,
    COALESCE(summary, '') AS summary,
    COALESCE(search_text, '') AS search_text,
    COALESCE(summary, '') AS rendered,
    use_count AS use_count,
    last_used_at AS last_used_at,
    created_at AS created_at,
    updated_at AS updated_at
FROM memory_facts
UNION ALL
SELECT
    'case:' || case_id AS document_id,
    'case' AS document_kind,
    scope AS scope,
    task_id AS task_id,
    COALESCE(title, '') AS title,
    COALESCE(summary, '') AS summary,
    COALESCE(search_text, '') AS search_text,
    trim(
        COALESCE(situation_text, '')
        || CASE WHEN COALESCE(task_text, '') != '' THEN '\nTask: ' || task_text ELSE '' END
        || CASE WHEN COALESCE(action_text, '') != '' THEN '\nAction: ' || action_text ELSE '' END
        || CASE WHEN COALESCE(result_text, '') != '' THEN '\nResult: ' || result_text ELSE '' END
    ) AS rendered,
    use_count AS use_count,
    last_used_at AS last_used_at,
    created_at AS created_at,
    updated_at AS updated_at
FROM memory_cases
"""

MEMORIES_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    document_id UNINDEXED,
    title,
    summary,
    search_text
)
"""


@dataclass(frozen=True)
class SQLiteVecStatus:
    available: bool
    detail: str


def ensure_memory_schema() -> SQLiteVecStatus:
    db = BaseModel._meta.database
    db.execute_sql(MEMORY_DOCUMENT_PROJECTION_SQL)
    db.execute_sql(MEMORIES_FTS_SQL)
    return ensure_sqlite_vec_loaded()


def ensure_sqlite_vec_loaded() -> SQLiteVecStatus:
    db = BaseModel._meta.database
    try:
        connection = db.connection()
    except Exception as exc:
        return SQLiteVecStatus(available=False, detail=f"db-unavailable:{exc.__class__.__name__}")
    try:
        import sqlite_vec
    except Exception:
        return SQLiteVecStatus(available=False, detail="sqlite-vec-not-installed")
    try:  # pragma: no cover - exercised only when optional extension is present
        sqlite_vec.load(connection)
    except sqlite3.OperationalError as exc:
        return SQLiteVecStatus(available=False, detail=f"sqlite-vec-load-failed:{exc}")
    except Exception as exc:  # pragma: no cover - optional dependency edge
        return SQLiteVecStatus(available=False, detail=f"sqlite-vec-error:{exc.__class__.__name__}")
    return SQLiteVecStatus(available=True, detail="sqlite-vec-loaded")
