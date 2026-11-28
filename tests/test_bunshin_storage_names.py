from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from pal.bunshin.config import bunshin_db_path
from pal.bunshin.cutover import cutover_bunshin_runtime
from pal.bunshin.schema import BUNSHIN_SCHEMA_VERSION, ensure_bunshin_schema
from pal.bunshin.repository import BunshinRepository
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
    with sqlite3.connect(path) as legacy:
        with pytest.raises(RuntimeError, match="requires explicit cutover"):
            BunshinRepository(tmp_path).delivery_bindings.read_task_delivery("task-1")
        assert legacy.execute("SELECT schema_value FROM bunshin_v2_schema_meta").fetchone() == ("29",)
        assert legacy.execute("SELECT owner_id FROM bunshin_v2_leases").fetchone() == ("active",)
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


def test_v29_cutover_treats_manager_timeout_as_unknown(tmp_path, monkeypatch):
    from pal.bunshin.ipc import BunshinManagerClient, bunshin_port_path

    path = bunshin_db_path(tmp_path)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE bunshin_v2_schema_meta(schema_key TEXT PRIMARY KEY, schema_value TEXT)")
        db.execute("INSERT INTO bunshin_v2_schema_meta VALUES ('schema_version', '29')")
    bunshin_port_path(tmp_path).write_text("12345", encoding="utf-8")

    def unavailable(_):
        raise TimeoutError("Manager did not answer")

    monkeypatch.setattr(BunshinManagerClient, "health_sync", unavailable)
    with pytest.raises(RuntimeError, match="Could not verify"):
        cutover_bunshin_runtime(tmp_path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT schema_value FROM bunshin_v2_schema_meta").fetchone() == ("29",)


def test_cutover_with_absent_endpoints_and_long_runtime_path(tmp_path):
    root = tmp_path / ("runtime-" + "x" * 120)
    path = bunshin_db_path(root)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE bunshin_v2_schema_meta(schema_key TEXT PRIMARY KEY, schema_value TEXT)")
        db.execute("INSERT INTO bunshin_v2_schema_meta VALUES ('schema_version', '29')")

    assert cutover_bunshin_runtime(root).status == "migrated_in_place"


@pytest.mark.parametrize("root_name", ["Pal #work", "Pal ?work", "Pal %20work"])
def test_cutover_reads_current_database_at_literal_file_path(tmp_path, root_name):
    root = tmp_path / root_name
    path = bunshin_db_path(root)
    BunshinRepository(root).database.ensure_schema()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE existing_runtime(value TEXT)")
        db.execute("INSERT INTO existing_runtime VALUES ('preserve')")

    assert cutover_bunshin_runtime(root).status == "already_current"
    assert not (root / "data" / "bunshin-archive").exists()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM existing_runtime").fetchone() == ("preserve",)


def test_cutover_leaves_unreadable_database_in_place(tmp_path):
    path = bunshin_db_path(tmp_path)
    path.parent.mkdir(parents=True)
    contents = b"unreadable database fixture"
    path.write_bytes(contents)

    with pytest.raises(RuntimeError, match="Could not read Bunshin schema version"):
        cutover_bunshin_runtime(tmp_path)
    assert path.read_bytes() == contents
    assert not (tmp_path / "data" / "bunshin-archive").exists()


def test_cutover_and_standalone_manager_exclude_each_other_before_schema_access(tmp_path, monkeypatch):
    from pal.bunshin import cutover, manager_main

    path = bunshin_db_path(tmp_path)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE bunshin_v2_schema_meta(schema_key TEXT PRIMARY KEY, schema_value TEXT)")
        db.execute("INSERT INTO bunshin_v2_schema_meta VALUES ('schema_version', '29')")

    def open_runtime_database(root, runtime_db_path):
        with pytest.raises(RuntimeError, match="owned by a Manager or cutover"):
            cutover_bunshin_runtime(root)
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT schema_value FROM bunshin_v2_schema_meta").fetchone() == ("29",)

    with monkeypatch.context() as startup:
        startup.setattr(manager_main, "_open_runtime_database", open_runtime_database)
        with pytest.raises(RuntimeError, match="requires explicit cutover"):
            asyncio.run(manager_main.amain(tmp_path))

    def migrate(connection, *, migrate_in_place=False):
        sidecar = subprocess.run(
            [sys.executable, "-m", "pal.bunshin.manager_main", "--runtime-root", str(tmp_path)],
            capture_output=True, text=True, timeout=30,
        )
        assert sidecar.returncode != 0
        assert "owned by a Manager or cutover" in sidecar.stderr
        assert "requires explicit cutover" not in sidecar.stderr
        ensure_bunshin_schema(connection, migrate_in_place=migrate_in_place)

    monkeypatch.setattr(cutover, "ensure_bunshin_schema", migrate)
    assert cutover_bunshin_runtime(tmp_path).status == "migrated_in_place"


def test_plugin_cleanup_leaves_endpoint_owned_by_manager(tmp_path):
    from pal.bunshin.capabilities import BunshinManagerProvider
    from pal.bunshin.ipc import bunshin_port_path
    from pal.bunshin.paths import bunshin_runtime_ownership

    endpoint = bunshin_port_path(tmp_path)
    endpoint.parent.mkdir(parents=True)
    endpoint.write_text("12345", encoding="utf-8")
    provider = BunshinManagerProvider(tmp_path)
    with bunshin_runtime_ownership(tmp_path):
        provider._cleanup_stale_endpoint()
        assert endpoint.is_file()
    provider._cleanup_stale_endpoint()
    assert not endpoint.exists()


def test_v30_requires_cutover_before_current_checkpoint_writes(tmp_path):
    path = bunshin_db_path(tmp_path)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        ensure_bunshin_schema(db)
        db.execute("DROP TABLE bunshin_role_checkpoint_heads")
        db.execute("UPDATE bunshin_schema_meta SET schema_value='30' WHERE schema_key='schema_version'")

    with pytest.raises(RuntimeError, match="requires explicit cutover"):
        BunshinRepository(tmp_path).delivery_bindings.read_task_delivery("missing")
    assert cutover_bunshin_runtime(tmp_path).status == "migrated_in_place"
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT schema_value FROM bunshin_schema_meta WHERE schema_key='schema_version'"
        ).fetchone() == (str(BUNSHIN_SCHEMA_VERSION),)
        assert db.execute("SELECT count(*) FROM bunshin_role_checkpoint_heads").fetchone() == (0,)


@pytest.mark.parametrize("version", [29, 30, 31])
def test_cutover_waits_for_live_recorded_worker_without_manager_socket(tmp_path, version):
    import subprocess

    from pal.bunshin.workspace_resources import process_group_birth

    path = bunshin_db_path(tmp_path)
    repository = BunshinRepository(tmp_path)
    with subprocess.Popen(["sleep", "30"], start_new_session=True) as group:
        try:
            birth = process_group_birth(group.pid)
            assert birth
            lease = repository.leases.claim_lease("assignment:old", "old-attempt", metadata={
                "run_id": "old", "aggregate_id": "node-a",
                "process_group_id": group.pid, "process_group_birth": birth,
            })
            repository.leases.release_lease("assignment:old", "old-attempt", lease.fencing_token)
            with sqlite3.connect(path) as db:
                db.execute("DROP TABLE bunshin_role_checkpoint_heads")
                db.execute("UPDATE bunshin_schema_meta SET schema_value=? WHERE schema_key='schema_version'",
                           (str(version),))
                if version == 29:
                    db.execute("ALTER TABLE bunshin_schema_meta RENAME TO bunshin_v2_schema_meta")
                    db.execute("ALTER TABLE bunshin_leases RENAME TO bunshin_v2_leases")
            with pytest.raises(RuntimeError, match="recorded worker process groups"):
                cutover_bunshin_runtime(tmp_path)
            assert group.poll() is None
            meta = "bunshin_v2_schema_meta" if version == 29 else "bunshin_schema_meta"
            with sqlite3.connect(path) as db:
                assert db.execute(f"SELECT schema_value FROM {meta} WHERE schema_key='schema_version'").fetchone() == (
                    str(version),
                )
        finally:
            group.terminate()
    assert cutover_bunshin_runtime(tmp_path).status == "migrated_in_place"
