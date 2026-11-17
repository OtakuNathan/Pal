"""Database owners release physical connections across persistent worker threads."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import sqlite3
import threading

from peewee import OperationalError, SqliteDatabase
import pytest

from pal.foundation.persistence import PalV2Database
from pal.memory.storage import MemoryStorage


def assert_closed(connection):
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize("read_only", [False, True])
def test_owner_close_releases_all_threads_and_workers_can_reconnect(tmp_path, read_only):
    path = tmp_path / "pal.sqlite3"
    prepared = PalV2Database(path)
    prepared.initialize([])
    prepared.close()
    owner = PalV2Database(path, read_only=read_only)
    database = owner.peewee_db
    try:
        with ExitStack() as cleanup:
            workers = [cleanup.enter_context(ThreadPoolExecutor(max_workers=1)) for _ in range(2)]
            workers[0].submit(owner.initialize, []).result(timeout=5)
            connections = [worker.submit(database.connection).result(timeout=5) for worker in workers]
            assert connections[0] is not connections[1]
            owner.close()  # This thread never opened a connection.
            for connection in connections:
                assert_closed(connection)
            reopened = []
            for worker, previous in zip(workers, connections):
                assert worker.submit(database.is_closed).result(timeout=5)
                assert worker.submit(database.connect, reuse_if_open=True).result(timeout=5)
                current = worker.submit(database.connection).result(timeout=5)
                reopened.append(current)
                assert current is not previous
                assert worker.submit(lambda: database.execute_sql("SELECT 1").fetchone()).result(timeout=5) == (1,)
            owner.close()
            for connection in reopened:
                assert_closed(connection)
            for worker in workers:
                assert worker.submit(database.is_closed).result(timeout=5)
    finally:
        owner.close()


def test_memory_repository_close_releases_workers_before_its_reader_lease(tmp_path):
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    repository = storage.open()
    database = repository.database
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            connection = worker.submit(database.connection).result(timeout=5)
            lease = repository.connection_lease
            repository.close()
            assert_closed(connection)
            assert worker.submit(database.is_closed).result(timeout=5)
            assert lease.closed
            assert repository.connection_lease is None
    finally:
        repository.close()


def test_failed_close_keeps_ownership_and_reader_lease_until_retry(tmp_path, monkeypatch):
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    repository = storage.open()
    database = repository.database
    main_connection = database.connection()
    physical_close = SqliteDatabase._close
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            connection = worker.submit(database.connection).result(timeout=5)
            lease = repository.connection_lease
            failures = [main_connection]
            def fail_once(self, target):
                if failures and target is failures[0]:
                    failures.pop()
                    raise sqlite3.OperationalError("injected close failure")
                physical_close(self, target)
            monkeypatch.setattr(SqliteDatabase, "_close", fail_once)
            with pytest.raises(ExceptionGroup, match="connection cleanup failed"):
                repository.close()
            assert main_connection.execute("SELECT 1").fetchone() == (1,)
            assert_closed(connection)
            assert worker.submit(database.is_closed).result(timeout=5)
            assert repository.connection_lease is lease
            assert not lease.closed
            repository.close()
            assert_closed(main_connection)
            assert lease.closed
    finally:
        repository.close()


def test_retrying_a_failed_thread_close_preserves_its_new_connection(tmp_path, monkeypatch):
    owner = PalV2Database(tmp_path / "pal.sqlite3")
    database = owner.peewee_db
    physical_close = SqliteDatabase._close
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            previous = worker.submit(database.connection).result(timeout=5)
            def fail_once(self, connection):
                monkeypatch.setattr(SqliteDatabase, "_close", physical_close)
                raise sqlite3.OperationalError("injected thread close failure")
            monkeypatch.setattr(SqliteDatabase, "_close", fail_once)
            with pytest.raises(OperationalError, match="injected thread close failure"):
                worker.submit(database.close).result(timeout=5)
            current = worker.submit(database.connection).result(timeout=5)
            assert current is not previous
            def fail_current_once(self, connection):
                if connection is current:
                    monkeypatch.setattr(SqliteDatabase, "_close", physical_close)
                    raise sqlite3.OperationalError("injected new connection close failure")
                physical_close(self, connection)
            monkeypatch.setattr(SqliteDatabase, "_close", fail_current_once)
            with pytest.raises(ExceptionGroup, match="connection cleanup failed"):
                owner.close()
            assert_closed(previous)
            assert not worker.submit(database.is_closed).result(timeout=5)
            assert worker.submit(database.connection).result(timeout=5) is current
            assert worker.submit(lambda: database.execute_sql("SELECT 1").fetchone()).result(timeout=5) == (1,)
            owner.close()
            assert_closed(current)
            assert worker.submit(database.is_closed).result(timeout=5)
    finally:
        owner.close()


def test_owner_close_waits_for_a_connection_being_initialized(tmp_path, monkeypatch):
    owner = PalV2Database(tmp_path / "pal.sqlite3")
    database = owner.peewee_db
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()
    initialize = database._initialize_connection
    def paused_initialize(connection):
        entered.set()
        assert release.wait(5)
        initialize(connection)
    monkeypatch.setattr(database, "_initialize_connection", paused_initialize)
    def close():
        closing.set()
        owner.close()
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            opened = workers.submit(database.connection)
            stopped = None
            try:
                assert entered.wait(3)
                stopped = workers.submit(close)
                assert closing.wait(3)
                assert not stopped.done()
            finally:
                release.set()
                connection = opened.result(timeout=5)
                if stopped is not None:
                    stopped.result(timeout=5)
            assert_closed(connection)
    finally:
        owner.close()


def test_owner_close_rejects_a_worker_transaction_without_partial_cleanup(tmp_path):
    owner = PalV2Database(tmp_path / "pal.sqlite3")
    database = owner.peewee_db
    main_connection = database.connection()
    entered, release = threading.Event(), threading.Event()
    def transaction():
        with database.atomic():
            entered.set()
            assert release.wait(5)
            assert database.execute_sql("SELECT 1").fetchone() == (1,)
        return database.connection()
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            active = worker.submit(transaction)
            try:
                assert entered.wait(3)
                with pytest.raises(OperationalError, match="transaction is open"):
                    owner.close()
                assert main_connection.execute("SELECT 1").fetchone() == (1,)
            finally:
                release.set()
                connection = active.result(timeout=5)
            owner.close()
            assert_closed(main_connection)
            assert_closed(connection)
    finally:
        owner.close()
