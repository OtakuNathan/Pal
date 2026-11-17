"""Construction failures release the actual SQLite handles and generation leases."""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import sqlite3

import pytest

from pal.memory.repository import MemoryDurableRepository
from pal.memory.storage import MemoryStorage
from pal.memory.service import MemoryService
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin
from pal.plugins_builtin.sqlite_vec_l3.runtime import SQLiteVecL3BuiltinBundle


def assert_released(repository, connection, lease):
    with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
        connection.execute('SELECT 1')
    if lease is not None:
        assert lease.closed
        assert repository.connection_lease is None


def test_failed_schema_initialization_closes_the_database_and_reader_lease(tmp_path, monkeypatch):
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    owned = []
    ensure = MemoryDurableRepository.ensure_schema
    def fail_after_schema(self):
        ensure(self)
        owned.append((self, self.database.connection(), self.connection_lease))
        raise OSError('schema preparation failed')
    monkeypatch.setattr(MemoryDurableRepository, 'ensure_schema', fail_after_schema)
    with pytest.raises(OSError, match='schema preparation failed'):
        storage.open()
    assert_released(*owned[0])


def test_failed_candidate_registration_does_not_create_files_or_a_reader(tmp_path, monkeypatch):
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    source = storage.open()
    owned = []
    open_repository = storage.open
    connection = storage.connection
    def observe(*args, **kwargs):
        repository = open_repository(*args, **kwargs)
        owned.append((repository, repository.database.connection(), repository.connection_lease))
        return repository
    @contextmanager
    def fail_registration(*, write=False):
        if write:
            raise OSError('catalog registration failed')
        with connection() as catalog:
            yield catalog
    monkeypatch.setattr(storage, 'open', observe)
    monkeypatch.setattr(storage, 'connection', fail_registration)
    try:
        with pytest.raises(OSError, match='catalog registration failed'):
            storage.candidate(source)
        assert not owned
        assert [path.name for path in (storage.root / 'generations').iterdir()] == ['initial']
    finally:
        source.close()


def test_interrupted_candidate_copy_remains_catalog_owned_and_can_be_collected(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    source = storage.open()
    snapshot = storage.snapshot
    def interrupted(*args):
        snapshot(*args)
        raise OSError('copy interrupted after writing files')
    monkeypatch.setattr(storage, 'snapshot', interrupted)
    try:
        with pytest.raises(OSError, match='copy interrupted'):
            storage.candidate(source)
        with storage.connection() as catalog:
            private, = catalog.execute('SELECT generation_id FROM generations WHERE published_at IS NULL').fetchall()
        assert storage.path(private[0]).exists()
        assert storage.collect(now=datetime.now(timezone.utc) + timedelta(days=8)) == [private[0]]
        assert not storage.path(private[0]).parent.exists()
    finally:
        source.close()


def test_interrupted_swap_copy_remains_owned_until_the_job_is_retired(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from pal.memory import change_capture
    from pal.memory.swap_store import SwapStore
    from pal.memory.swap_state import SwapEvent
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    store = SwapStore(storage)
    job = store.create('sqlite_vec_l3', 'sqlite_vec_l3', {}, source_generation=storage.current())
    store.advance(job, SwapEvent.SNAPSHOT)
    snapshot = change_capture.snapshot
    def interrupted(*args):
        snapshot(*args)
        raise OSError('swap copy interrupted after writing files')
    monkeypatch.setattr(change_capture, 'snapshot', interrupted)
    with pytest.raises(OSError, match='swap copy interrupted'):
        store.capture(job)
    assert store.load(job.job_id).candidate_generation == job.candidate_generation
    assert storage.path(job.candidate_generation).exists()
    later = datetime.now(timezone.utc) + timedelta(days=8)
    assert storage.collect(now=later) == []
    store.advance(job, SwapEvent.ABANDON)
    assert storage.collect(now=later) == [job.candidate_generation]


def test_builtin_provider_failure_before_publication_closes_its_repository(tmp_path, monkeypatch):
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    owned = []
    def fail_configuration(self, **kwargs):
        owned.append((self.repository, self.repository.database.connection(), self.repository.connection_lease))
        raise OSError('provider configuration failed')
    monkeypatch.setattr(SQLiteVecL3Plugin, '_load_embedding_space', fail_configuration)
    scope = SimpleNamespace(core_context=SimpleNamespace(require_port=lambda name: MemoryService()),
                            defer=lambda callback: None)
    with pytest.raises(OSError, match='provider configuration failed'):
        SQLiteVecL3BuiltinBundle(runtime_root=tmp_path).start(scope)
    assert_released(*owned[0])


def test_dreaming_preparation_failure_closes_the_private_database(tmp_path, monkeypatch):
    from pal.memory.dreaming.service import DreamingService
    from pal.memory.embedding import HashingEmbedder
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    provider = SQLiteVecL3Plugin(MemoryService(), repository=storage.open(), embedding_provider=HashingEmbedder())
    owned = []
    def fail_configuration(self, **kwargs):
        owned.append((self.repository, self.repository.database.connection(), self.repository.connection_lease))
        raise OSError('private provider configuration failed')
    monkeypatch.setattr(SQLiteVecL3Plugin, '_load_embedding_space', fail_configuration)
    dreaming = DreamingService(storage=storage, provider=provider, llm=None)
    try:
        with pytest.raises(OSError, match='private provider configuration failed'):
            dreaming._prepare('preparation', 'review')
        assert_released(*owned[0])
    finally:
        provider.close()


def test_invalid_dreaming_configuration_releases_its_unpublished_timer_scope(tmp_path):
    from pal.memory.dreaming.runtime import register_dreaming
    from tests.core_factory import build_core
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    with storage.connection(write=True) as catalog:
        catalog.execute("INSERT INTO memory_settings VALUES ('dreaming_config','invalid JSON')")
    core = build_core()
    scopes = set(core.main_loop.timers.scopes)
    try:
        with pytest.raises(ValueError):
            register_dreaming(core.context, SimpleNamespace(repository=SimpleNamespace(catalog=storage)), None, tmp_path)
        assert set(core.main_loop.timers.scopes) == scopes
        assert core.context.module_registry.get('memory.dreaming') is None
    finally:
        core.close()
        core.context.execution_runtime.shutdown()
