"""SQLite-owned change capture and retired-generation write rejection.

Triggers record keys in the same transaction as content and replay receipts.
Derived search indexes are rebuilt by the target backend and are not truth.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


CONTENT_TABLES = {
    "memory_facts": "fact_id",
    "memory_cases": "case_id",
    "memory_topics": "topic_id",
    "memory_mutations": "mutation_id",
    "memory_batch_receipts": "batch_id",
    "memory_revisions": "document_id",
}
GUARDED_TABLES = {**CONTENT_TABLES, "memory_embeddings": "embedding_id", "memory_embedding_vec": "embedding_id",
                  "memory_embedding_space": "singleton", "memory_embedding_configuration": "singleton"}


def install_capture(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS memory_generation_guard (singleton INTEGER PRIMARY KEY CHECK(singleton=1), writable INTEGER NOT NULL)")
    connection.execute("INSERT OR IGNORE INTO memory_generation_guard VALUES (1,1)")
    connection.execute("CREATE TABLE IF NOT EXISTS memory_change_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL, row_key TEXT NOT NULL)")
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='memory_change_identity'").fetchone():
        connection.execute("DELETE FROM memory_change_log WHERE seq NOT IN (SELECT MAX(seq) FROM memory_change_log GROUP BY table_name,row_key)")
        connection.execute("CREATE UNIQUE INDEX memory_change_identity ON memory_change_log(table_name,row_key)")
        for table in CONTENT_TABLES:
            for action in ("insert", "update", "delete"):
                connection.execute(f"DROP TRIGGER IF EXISTS capture_{table}_{action}")
    for table, key in GUARDED_TABLES.items():
        for action in ("INSERT", "UPDATE", "DELETE"):
            connection.execute(f'''CREATE TRIGGER IF NOT EXISTS guard_{table}_{action.lower()}
                BEFORE {action} ON {table} BEGIN
                SELECT CASE WHEN (SELECT writable FROM memory_generation_guard WHERE singleton=1)=0
                    THEN RAISE(ABORT, 'retired memory generation cannot accept writes') END;
                END''')
            if table not in CONTENT_TABLES:
                continue
            names = ("NEW",) if action == "INSERT" else ("OLD",) if action == "DELETE" else ("OLD", "NEW")
            inserts = "".join(f"INSERT INTO memory_change_log(table_name,row_key) VALUES ('{table}',{name}.{key}) "
                              "ON CONFLICT(table_name,row_key) DO UPDATE SET seq=excluded.seq;" for name in names)
            connection.execute(f"CREATE TRIGGER IF NOT EXISTS capture_{table}_{action.lower()} AFTER {action} ON {table} BEGIN {inserts} END")


def cursor(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COALESCE(MAX(seq),0) FROM memory_change_log").fetchone()[0])


def snapshot(source: Path, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)) as origin:
        origin.execute("BEGIN")
        sequence = cursor(origin)
        with closing(sqlite3.connect(target)) as destination:
            origin.backup(destination)
        return sequence


def replay(source: Path, destination: sqlite3.Connection, after: int) -> tuple[int, set[str]]:
    documents: set[str] = set()
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)) as origin:
        origin.execute("BEGIN")
        sequence = cursor(origin)
        changes = origin.execute("SELECT DISTINCT table_name,row_key FROM memory_change_log WHERE seq>? AND seq<=?", (after, sequence)).fetchall()
        for table, row_key in changes:
            key = CONTENT_TABLES[table]
            columns = [row[1] for row in origin.execute(f"PRAGMA table_info({table})")]
            row = origin.execute(f"SELECT * FROM {table} WHERE {key}=?", (row_key,)).fetchone()
            destination.execute(f"DELETE FROM {table} WHERE {key}=?", (row_key,))
            if row is not None:
                marks = ",".join("?" for _ in columns)
                destination.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES ({marks})", row)
            if table in ("memory_facts", "memory_cases"):
                documents.add(("fact:" if table == "memory_facts" else "case:") + row_key)
    return sequence, documents


def content_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table, key in CONTENT_TABLES.items():
        digest.update(table.encode())
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY {key}"):
            digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()
