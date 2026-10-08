from __future__ import annotations

import fcntl
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from pal.memory.storage import MemoryStorage


class WorkerMemoryReadLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pal-memory-read-lease-")
        self.addCleanup(self.temp.cleanup)
        self.storage = MemoryStorage(Path(self.temp.name))
        self.generation = self.storage.create_initial()
        self.storage.pin("workflow")

    def sidecars(self, path):
        return [Path(str(path) + suffix) for suffix in ("-wal", "-shm")]

    def test_closed_wal_databases_retain_sidecars_until_lease_closes(self):
        paths = [self.storage.catalog_path, self.storage.path(self.generation)]
        self.assertTrue(all(not sidecar.exists() for path in paths for sidecar in self.sidecars(path)))
        with self.storage.worker_read_lease("workflow"):
            self.assertTrue(all(sidecar.is_file() for path in paths for sidecar in self.sidecars(path)))
            readonly = MemoryStorage(Path(self.temp.name), read_only=True)
            self.assertEqual(readonly.pinned("workflow"), self.generation)
            repo = readonly.open(self.generation, read_only=True)
            try:
                self.assertEqual(repo.list_projection_rows(), [])
            finally:
                repo.close()
            # Closing transient worker reads must not retire the sidecars.
            self.assertTrue(all(sidecar.is_file() for path in paths for sidecar in self.sidecars(path)))
        self.assertTrue(all(not sidecar.exists() for path in paths for sidecar in self.sidecars(path)))

    def test_lease_is_query_only_without_a_snapshot_and_uses_exact_pin(self):
        next_repo = self.storage.open("next")
        self.storage.seal(next_repo)
        with self.storage.connection(write=True) as connection:
            connection.execute("UPDATE memory_head SET generation_id='next'")
        connections = []
        connect = sqlite3.connect

        def track(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connections.append(connection)
            return connection

        with patch("pal.memory.storage.sqlite3.connect", side_effect=track):
            with self.storage.worker_read_lease("workflow"):
                self.assertEqual(len(connections), 2)
                for connection in connections:
                    self.assertFalse(connection.in_transaction)
                    with closing(connection.execute("PRAGMA query_only")) as cursor:
                        self.assertEqual(cursor.fetchone()[0], 1)
                    with self.assertRaises(sqlite3.OperationalError):
                        connection.execute("CREATE TABLE forbidden (id INTEGER)")
                self.assertTrue(self.sidecars(self.storage.path(self.generation))[1].exists())
                self.assertFalse(self.sidecars(self.storage.path("next"))[1].exists())
                readonly = MemoryStorage(Path(self.temp.name), read_only=True)
                self.assertEqual(readonly.current(), "next")
                self.storage.set_frozen(self.generation, True)
                with self.storage.connection(write=True) as connection:
                    connection.execute("INSERT INTO deleted_memories VALUES ('synthetic', 'now')")
                    connection.execute("UPDATE memory_head SET generation_id=?", (self.generation,))
                self.assertTrue(readonly.is_frozen(self.generation))
                self.assertEqual(readonly.deleted_refs(), {"synthetic"})
                self.assertEqual(readonly.current(), self.generation)
                self.storage.set_frozen(self.generation, False)
                self.assertFalse(readonly.is_frozen(self.generation))
                self.storage.release("workflow")
                with self.assertRaisesRegex(RuntimeError, "no live memory generation pin"):
                    readonly.pinned("workflow")

    def test_generation_connection_closes_before_reader_lock(self):
        alias = Path(self.temp.name) / "runtime-alias"
        alias.symlink_to(Path(self.temp.name).resolve(), target_is_directory=True)
        self.storage = MemoryStorage(alias)
        lock_path = self.storage.path(self.generation).parent / "readers.lock"
        closed = []
        testcase = self

        class Connection(sqlite3.Connection):
            def close(self):
                with closing(self.execute("PRAGMA database_list")) as cursor:
                    path = Path(cursor.fetchone()[2])
                if path == testcase.storage.path(testcase.generation).resolve():
                    with lock_path.open("rb") as reader:
                        with testcase.assertRaises(BlockingIOError):
                            fcntl.flock(reader, fcntl.LOCK_EX | fcntl.LOCK_NB)
                closed.append(path)
                super().close()

        connect = sqlite3.connect
        with patch("pal.memory.storage.sqlite3.connect", side_effect=lambda *a, **k: connect(*a, factory=Connection, **k)):
            with self.storage.worker_read_lease("workflow"):
                with lock_path.open("rb") as reader:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(reader, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(closed, [self.storage.path(self.generation).resolve(), self.storage.catalog_path.resolve()])
        with lock_path.open("rb") as reader:
            fcntl.flock(reader, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_missing_generation_fails_without_creating_and_cleans_partial_lease(self):
        path = self.storage.path(self.generation)
        path.unlink()
        with self.assertRaises(sqlite3.OperationalError):
            with self.storage.worker_read_lease("workflow"):
                self.fail("missing generation was accepted")
        self.assertFalse(path.exists())
        self.assertTrue(all(not p.exists() for p in self.sidecars(self.storage.catalog_path)))
        with (path.parent / "readers.lock").open("rb") as reader:
            fcntl.flock(reader, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_missing_catalog_fails_without_creating(self):
        self.storage.catalog_path.unlink()
        with self.assertRaises(sqlite3.OperationalError):
            with self.storage.worker_read_lease("workflow"):
                self.fail("missing catalog was accepted")
        self.assertFalse(self.storage.catalog_path.exists())

    def test_missing_or_released_pin_fails_without_leaking_sidecars(self):
        self.storage.release("workflow")
        for workflow in ("workflow", "missing"):
            with self.assertRaisesRegex(RuntimeError, "no live memory generation pin"):
                with self.storage.worker_read_lease(workflow):
                    self.fail("missing pin was accepted")
            self.assertTrue(all(not p.exists() for p in self.sidecars(self.storage.catalog_path)))

    def test_read_only_storage_cannot_acquire_host_lease(self):
        readonly = MemoryStorage(Path(self.temp.name), read_only=True)
        with self.assertRaises(PermissionError):
            with readonly.worker_read_lease("workflow"):
                self.fail("read-only storage acquired a host lease")
