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
FROM memory_facts WHERE lifecycle = 'active'
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
FROM memory_cases WHERE lifecycle = 'active'
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


def ensure_memory_schema(database=None) -> SQLiteVecStatus:
    db = database if database is not None else BaseModel._meta.database
    _ensure_schema_migrations(db.connection())
    view = db.execute_sql("SELECT sql FROM sqlite_master WHERE type='view' AND name='memory_document_projection'").fetchone()
    if view is not None and "WHERE lifecycle = 'active'" not in str(view[0]):
        db.execute_sql("DROP VIEW IF EXISTS memory_document_projection")
    db.execute_sql(MEMORY_DOCUMENT_PROJECTION_SQL)
    db.execute_sql(MEMORIES_FTS_SQL)
    return ensure_sqlite_vec_loaded(db)


def _ensure_schema_migrations(connection: sqlite3.Connection) -> None:
    for table in ("memory_facts", "memory_cases"):
        _ensure_column(connection, table_name=table, column_name="content_revision", definition="INTEGER NOT NULL DEFAULT 1")
    _ensure_column(connection, table_name="memory_cases", column_name="canonical_key", definition="TEXT")
    _ensure_column(connection, table_name="memory_embeddings", column_name="text_processing_version", definition="TEXT NOT NULL DEFAULT 'search_text_v1'")
    connection.execute("CREATE TABLE IF NOT EXISTS memory_mutations (mutation_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, document_id TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS memory_revisions (document_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, document_json TEXT NOT NULL, successors_json TEXT NOT NULL, reason TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0)")
    _ensure_column(
        connection,
        table_name="memory_embeddings",
        column_name="provider_id",
        definition="TEXT DEFAULT 'ollama_local_embedding'",
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS memory_embeddings_provider_model_status_idx
        ON memory_embeddings(provider_id, model_name, index_status)
        """
    )


def _ensure_column(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    cursor = connection.execute(f"PRAGMA table_info({table_name})")
    columns = {str(row[1]) for row in cursor.fetchall() if len(row) > 1}
    if column_name in columns:
        return
    connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def ensure_sqlite_vec_loaded(database=None) -> SQLiteVecStatus:
    db = database if database is not None else BaseModel._meta.database
    try:
        connection = db.connection()
    except Exception as exc:
        return SQLiteVecStatus(available=False, detail=f"db-unavailable:{exc.__class__.__name__}")
    try:
        import sqlite_vec
    except Exception:
        return SQLiteVecStatus(available=False, detail="sqlite-vec-not-installed")
    try:  # pragma: no cover - exercised only when optional extension is present
        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
    except sqlite3.OperationalError as exc:
        return SQLiteVecStatus(available=False, detail=f"sqlite-vec-load-failed:{exc}")
    except Exception as exc:  # pragma: no cover - optional dependency edge
        return SQLiteVecStatus(available=False, detail=f"sqlite-vec-error:{exc.__class__.__name__}")
    return SQLiteVecStatus(available=True, detail="sqlite-vec-loaded")
