from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

from pal.bunshin.config import bunshin_db_path
from pal.bunshin.cutover import cutover_bunshin_runtime
from pal.bunshin.schema import ensure_bunshin_schema
from pal.shared.messages import BunshinInvocationPack


def test_versioned_database_is_upgraded_once_without_losing_rows_or_fts(tmp_path):
    path = bunshin_db_path(tmp_path)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE bunshin_v2_schema_meta(schema_key TEXT PRIMARY KEY, schema_value TEXT NOT NULL);
            INSERT INTO bunshin_v2_schema_meta VALUES ('schema_version', '29');
            CREATE VIRTUAL TABLE bunshin_v2_tasks_fts USING fts5(task_id UNINDEXED, title, objective, workspace);
            INSERT INTO bunshin_v2_tasks_fts VALUES ('task-1', 'running', 'preserve task', 'repo');
            CREATE TABLE bunshin_v2_leases(resource_key TEXT PRIMARY KEY, owner_id TEXT, fencing_token INTEGER,
                acquired_at TEXT, renewed_at TEXT, expires_at TEXT, metadata_json TEXT);
            CREATE INDEX bunshin_v2_test_owner ON bunshin_v2_leases(owner_id);
            INSERT INTO bunshin_v2_leases VALUES ('scope', 'active', 3, '', '', '', '{}');
        """)
    assert cutover_bunshin_runtime(tmp_path).status == "migrated_in_place"

    def open_runtime():
        with sqlite3.connect(path) as db:
            ensure_bunshin_schema(db)
    with ThreadPoolExecutor(max_workers=2) as threads:
        list(threads.map(lambda _: open_runtime(), range(2)))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT owner_id, fencing_token FROM bunshin_leases").fetchone() == ("active", 3)
        assert db.execute("SELECT task_id FROM bunshin_tasks_fts WHERE bunshin_tasks_fts MATCH 'running'").fetchall() == [("task-1",)]
        assert not db.execute("SELECT name FROM sqlite_schema WHERE name GLOB 'bunshin_v2_*'").fetchall()
        assert db.execute("SELECT name FROM sqlite_schema WHERE name='bunshin_test_owner'").fetchone()
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_pinned_old_pack_is_read_without_rewriting_the_input():
    old = {"invocation_id": "role", "workspace": {"bunshin_v2": {"mode": "repair"}, "v2_role_workspace": True},
           "metadata": {"bunshin_v2": {"role": "implementation"}}}
    pack = BunshinInvocationPack.from_dict(old)
    assert pack.workspace["bunshin"] == {"mode": "repair"}
    assert pack.workspace["role_workspace_prepared"] is True
    assert "v2_role_workspace" not in pack.workspace
    assert pack.metadata["bunshin"] == {"role": "implementation"}
    assert "bunshin_v2" in old["workspace"]
