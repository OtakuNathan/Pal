"""Durable workflow references to Memory generations, independent of Dreaming."""
from __future__ import annotations

import fcntl
import hashlib
import sqlite3
from contextlib import closing, contextmanager

from pal.bunshin.config import bunshin_db_path
from pal.bunshin.contracts import AggregateType
from pal.memory.storage import MemoryStorage


def _reference_lock(storage, workflow_id):
    directory = storage.root / "workflow_references"
    directory.mkdir(exist_ok=True)
    return (directory / hashlib.sha256(workflow_id.encode()).hexdigest()).open("a+b")


@contextmanager
def workflow_memory_binding(runtime_root, repository, workflow_id):
    storage = MemoryStorage(runtime_root)
    if not storage.catalog_path.exists():
        yield
        return
    with _reference_lock(storage, workflow_id) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        created = repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id) is None
        if created:
            storage.pin(workflow_id, managed=True)
        else:
            storage.pinned(workflow_id)
        try:
            yield
        except BaseException:
            # A lost acknowledgement can still mean the workflow committed.
            if created and repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id) is None:
                with storage.connection(write=True) as connection:
                    connection.execute("DELETE FROM workflow_memory_owners WHERE workflow_id=?", (workflow_id,))
                    connection.execute("DELETE FROM workflow_pins WHERE workflow_id=?", (workflow_id,))
            raise


def reconcile_workflow_memory_references(runtime_root):
    storage = MemoryStorage(runtime_root)
    path = bunshin_db_path(runtime_root)
    if not storage.catalog_path.exists() or not path.exists():
        return
    with storage.connection() as catalog:
        owners = catalog.execute("""SELECT p.workflow_id FROM workflow_pins p
            JOIN workflow_memory_owners o USING(workflow_id) WHERE p.released=0""").fetchall()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if not owners:
            return
        candidates = connection.execute("""SELECT aggregate_id FROM bunshin_v2_aggregate_snapshots
            WHERE aggregate_type='workflow' AND state NOT IN ('COMPLETED','REJECTED','CANCELLED')""").fetchall()
        active_workflows = {row[0] for row in candidates}
        released = False
        for (workflow_id,) in owners:
            if workflow_id in active_workflows:
                continue
            with _reference_lock(storage, workflow_id) as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue  # The creating transaction still owns this reference.
                workflow = connection.execute("""SELECT state FROM bunshin_v2_aggregate_snapshots
                    WHERE aggregate_type='workflow' AND aggregate_id=?""", (workflow_id,)).fetchone()
                if workflow is None:
                    with storage.connection(write=True) as catalog:
                        catalog.execute("DELETE FROM workflow_memory_owners WHERE workflow_id=?", (workflow_id,))
                        catalog.execute("DELETE FROM workflow_pins WHERE workflow_id=?", (workflow_id,))
                    released = True
                elif workflow[0] in ('COMPLETED', 'REJECTED', 'CANCELLED'):
                    active = connection.execute("""SELECT 1 FROM bunshin_v2_role_sessions
                        WHERE workflow_id=? AND status NOT IN ('completed','cancelled')
                        UNION ALL SELECT 1 FROM bunshin_v2_role_assignments
                        WHERE workflow_id=? AND state NOT IN ('settled','cancelled') LIMIT 1""",
                        (workflow_id, workflow_id)).fetchone()
                    if active is None:
                        storage.release(workflow_id)
                        released = True
        if released:
            storage.collect()
