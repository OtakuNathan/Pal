"""Workflow-owned memory pins; worker processes only read these bindings."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from pal.bunshin.config import bunshin_db_path


@contextmanager
def workflow_memory_binding(runtime_root, repository, workflow_id):
    from pal.memory.storage import MemoryStorage
    from pal.bunshin.v2.contracts import AggregateType
    storage = MemoryStorage(runtime_root)
    created = storage.catalog_path.exists() and repository.read_snapshot(AggregateType.WORKFLOW, workflow_id) is None
    if created:
        storage.pin(workflow_id)
    try:
        yield
    except BaseException:
        # A lost dispatch acknowledgement may still mean a committed workflow.
        # Drop only a reservation that never acquired a durable logical owner.
        if created and repository.read_snapshot(AggregateType.WORKFLOW, workflow_id) is None:
            with storage.connection(write=True) as connection:
                connection.execute("DELETE FROM workflow_pins WHERE workflow_id=?", (workflow_id,))
        raise


def initialize_existing_workflow_pins(runtime_root, storage):
    with storage.connection() as catalog:
        if catalog.execute("SELECT 1 FROM memory_settings WHERE key='workflow_pins_initialized'").fetchone():
            return
    path = bunshin_db_path(runtime_root)
    if path.exists():
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='bunshin_v2_aggregate_snapshots'").fetchone():
                for row in connection.execute("SELECT aggregate_id FROM bunshin_v2_aggregate_snapshots WHERE aggregate_type='workflow'"):
                    storage.pin(str(row[0]))
        finally:
            connection.close()
    with storage.connection(write=True) as catalog:
        catalog.execute("INSERT OR REPLACE INTO memory_settings VALUES ('workflow_pins_initialized','1')")


def release_retired_workflow_pins(runtime_root, storage):
    path = bunshin_db_path(runtime_root)
    if not path.exists():
        return
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"bunshin_v2_aggregate_snapshots", "bunshin_v2_role_assignments", "bunshin_v2_role_sessions"} <= tables:
            return
        # Workflow terminal states have no reactivation transition. Still wait
        # for all logical role sessions and assignments to become terminal;
        # actual surviving readers additionally hold a generation file lease.
        rows = connection.execute("""SELECT aggregate_id FROM bunshin_v2_aggregate_snapshots w
            WHERE aggregate_type='workflow' AND state IN ('COMPLETED','REJECTED','CANCELLED')
            AND NOT EXISTS (SELECT 1 FROM bunshin_v2_role_sessions s WHERE s.workflow_id=w.aggregate_id AND s.status NOT IN ('completed','cancelled'))
            AND NOT EXISTS (SELECT 1 FROM bunshin_v2_role_assignments a WHERE a.workflow_id=w.aggregate_id AND a.state NOT IN ('settled','cancelled'))""").fetchall()
        for row in rows:
            storage.release(str(row[0]))
    finally:
        connection.close()
