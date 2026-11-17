"""Existing workflow generations and reader pins survive the ownership upgrade."""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import sqlite3

import pytest

from pal.bunshin.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.memory_binding import (
    prepare_workflow_memory_references, reconcile_workflow_memory_references, workflow_memory_binding,
)
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.runner_components.memory_runtime import build_role_memory
from pal.foundation import utc_now
from pal.memory.contracts import L3CommitRequest, MemoryQuery
from pal.memory.embedding import HashingEmbedder
from pal.memory.service import MemoryService
from pal.memory.storage import MemoryStorage
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin


@pytest.fixture
def generation(tmp_path):
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    provider = SQLiteVecL3Plugin(MemoryService(), repository=storage.open(),
                                embedding_provider=HashingEmbedder(dimension=24))
    try:
        yield storage, provider
    finally:
        provider.close()


def store_snapshot(repository, identity, state, *, aggregate_type=AggregateType.WORKFLOW):
    current = repository.snapshots.read_snapshot(aggregate_type, identity)
    stamp = utc_now()
    snapshot = (replace(current, state=state, version=current.version + 1, updated_at=stamp)
                if current is not None else AggregateSnapshot(
                    aggregate_type=aggregate_type, aggregate_id=identity, workflow_id=identity,
                    state=state, version=1, payload={}, created_at=stamp, updated_at=stamp))
    with repository.database.write_connection() as connection:
        repository.snapshots.write_snapshot_locked(connection, current, snapshot)


def reference_rows(storage):
    with storage.connection() as connection:
        return (
            {row[0] for row in connection.execute("SELECT workflow_id FROM workflow_memory_owners")},
            dict(connection.execute("SELECT workflow_id,released FROM workflow_pins")),
        )


@pytest.mark.parametrize('configuration', ['missing_table', 'empty_table'])
@pytest.mark.parametrize('has_space', [False, True])
def test_old_pinned_generation_keeps_basic_reads_without_any_embedding_transport(
        tmp_path, generation, monkeypatch, configuration, has_space):
    storage, writer = generation
    saved = writer.commit(L3CommitRequest(kind='fact', summary='Durable workflow ownership',
        search_text='durable workflow ownership', topics=['ownership']))
    writer.refresh_indexes()
    pinned = storage.pin('old-workflow')
    candidate = storage.candidate(writer.repository)
    storage.publish_reindexed(candidate, source_generation=pinned, profile=writer.migration_identity)
    assert storage.current() != pinned
    writer.close()

    # Reconstruct the two legacy shapes: an untouched retired database has no
    # metadata table; a schema-only upgrade may have an empty constructor table.
    path = storage.path(pinned)
    with closing(sqlite3.connect(path)) as connection, connection:
        if configuration == 'missing_table':
            connection.execute('DROP TABLE memory_embedding_configuration')
        else:
            connection.execute('DELETE FROM memory_embedding_configuration')
        if not has_space:
            connection.execute('DROP TABLE memory_embedding_space')
    if not has_space:
        with storage.connection(write=True) as connection:
            connection.execute("UPDATE generation_bindings SET profile_json='{}' WHERE generation_id=?", (pinned,))
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    def forbid_transport(*args, **kwargs):
        raise AssertionError('Legacy memory must not guess an embedding constructor')

    monkeypatch.setattr('pal.plugins.l3.sqlite_vec.OllamaEmbeddingProvider', forbid_transport)
    monkeypatch.setattr('pal.bunshin.runner_components.memory_runtime.embedding_from_configuration', forbid_transport)
    role = build_role_memory(tmp_path, service=MemoryService(), workflow_id='old-workflow')
    try:
        provider = role.provider
        assert provider.repository.generation_id == pinned
        assert storage.pinned('old-workflow') == pinned
        assert provider.read_only and provider.repository.read_only
        assert provider.embedding_provider is None
        exact = provider.recall(MemoryQuery(mem_ref=saved.document_id))
        assert exact.metadata['lookup_status'] == 'found'
        assert exact.hits[0]['document_id'] == saved.document_id
        for query in (MemoryQuery(queries=['durable workflow']), MemoryQuery(topic_scope=['ownership'])):
            found = provider.recall(query)
            assert saved.document_id in {hit['document_id'] for hit in found.hits}
            assert found.metadata['candidate_sources']['vector'] == 0
            assert not found.metadata['vector_available']
            assert not found.metadata['embedding_healthy']
        assert provider.refresh_indexes()['refreshed'] == 0
        assert not provider.refresh_indexes()['vector_available']
        rejected = provider.commit(L3CommitRequest(kind='fact', summary='Must remain read-only'))
        assert rejected.status == 'unavailable'
    finally:
        role.close()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert role.provider.repository.connection_lease is None
    assert role.provider.repository.database.is_closed()


@pytest.mark.parametrize('configuration', ['{invalid-json', json.dumps({'kind': 'unknown', 'parameters': {}})])
def test_present_invalid_embedding_constructor_still_fails_and_releases_reader(
        tmp_path, generation, monkeypatch, configuration):
    storage, writer = generation
    storage.pin('workflow')
    writer.repository.database.execute_sql(
        'UPDATE memory_embedding_configuration SET configuration_json=? WHERE singleton=1', (configuration,))
    opened = []
    original_open = MemoryStorage.open

    def record_open(self, *args, **kwargs):
        repository = original_open(self, *args, **kwargs)
        opened.append(repository)
        return repository

    monkeypatch.setattr(MemoryStorage, 'open', record_open)
    with pytest.raises(ValueError):
        build_role_memory(tmp_path, service=MemoryService(), workflow_id='workflow')
    assert len(opened) == 1
    assert opened[0].database.is_closed()
    assert opened[0].connection_lease is None


def test_reconciliation_adopts_only_actual_legacy_workflows_and_releases_them_when_terminal(
        tmp_path, generation):
    storage, _ = generation
    repository = BunshinRepository(tmp_path)
    states = {'old-active': 'CREATED', 'old-completed': 'COMPLETED',
              'old-rejected': 'REJECTED', 'old-cancelled': 'CANCELLED'}
    for workflow, state in states.items():
        storage.pin(workflow)  # The old writer had no managed-owner flag.
        store_snapshot(repository, workflow, state)
    storage.pin('independent-reader')
    storage.pin('task-reader')
    store_snapshot(repository, 'task-reader', 'ACTIVE', aggregate_type=AggregateType.TASK)
    assert reference_rows(storage)[0] == set()

    reconcile_workflow_memory_references(tmp_path)
    owners, pins = reference_rows(storage)
    assert owners == set(states)
    assert pins == {'old-active': 0, 'old-completed': 1, 'old-rejected': 1, 'old-cancelled': 1,
                    'independent-reader': 0, 'task-reader': 0}
    reconcile_workflow_memory_references(tmp_path)
    assert reference_rows(storage) == (owners, pins)

    store_snapshot(repository, 'old-active', 'COMPLETED')
    reconcile_workflow_memory_references(tmp_path)
    owners, pins = reference_rows(storage)
    assert owners == set(states)
    assert all(pins[workflow] == 1 for workflow in states)
    assert storage.pinned('independent-reader') == storage.current()
    assert storage.pinned('task-reader') == storage.current()


def test_legacy_owner_backfill_respects_the_workflow_creation_lock(tmp_path, generation):
    storage, _ = generation
    repository = BunshinRepository(tmp_path)
    storage.pin('old-workflow')
    store_snapshot(repository, 'old-workflow', 'COMPLETED')
    with workflow_memory_binding(tmp_path, repository, 'old-workflow'):
        reconcile_workflow_memory_references(tmp_path)
        assert reference_rows(storage) == (set(), {'old-workflow': 0})
    reconcile_workflow_memory_references(tmp_path)
    assert reference_rows(storage) == ({'old-workflow'}, {'old-workflow': 1})


@pytest.fixture
def legacy_catalog(generation):
    storage, writer = generation
    writer.commit(L3CommitRequest(kind='fact', summary='Existing generation remains unchanged'))
    storage.pin('old-workflow')
    storage.pin('independent-reader')
    storage.set_frozen(storage.current(), True)
    writer.close()
    with storage.connection(write=True) as connection:
        connection.execute('DROP TABLE workflow_memory_owners')
        connection.execute('DROP TABLE generation_bindings')
    return storage


def catalog_authority_rows(storage):
    with storage.connection() as connection:
        return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY 1')]
                for table in ('memory_head', 'generations', 'workflow_pins', 'memory_settings')}


def test_manager_upgrades_existing_catalog_once_before_recovery_and_reconciliation(
        tmp_path, legacy_catalog, monkeypatch):
    from pal.bunshin.manager import BunshinManager

    storage = legacy_catalog
    manager = BunshinManager(tmp_path)
    store_snapshot(manager.workflow_service.repository, 'old-workflow', 'CREATED')
    before = catalog_authority_rows(storage)
    path = storage.path(storage.current())
    generation_bytes = path.read_bytes()
    events = []

    class StartupChecked(Exception):
        pass

    def prepare(root):
        events.append('prepare')
        prepare_workflow_memory_references(root)

    def recover(_recovery):
        events.append('recover')
        assert catalog_authority_rows(storage) == before
        assert path.read_bytes() == generation_bytes
        assert reference_rows(storage)[0] == set()
        assert storage.provider_for_generation(storage.current()) == 'sqlite_vec_l3'
        assert storage.generation_profile(storage.current()) == {}
        return {}

    def reconcile(root):
        events.append('reconcile')
        reconcile_workflow_memory_references(root)
        raise StartupChecked

    monkeypatch.setattr('pal.bunshin.memory_binding.prepare_workflow_memory_references', prepare)
    monkeypatch.setattr('pal.bunshin.manager.BunshinRecovery.recover', recover)
    monkeypatch.setattr('pal.bunshin.memory_binding.reconcile_workflow_memory_references', reconcile)

    async def scenario():
        try:
            with pytest.raises(StartupChecked):
                await manager.run()
        finally:
            await manager.close_all()

    asyncio.run(scenario())
    assert events == ['prepare', 'recover', 'reconcile']
    assert reference_rows(storage) == ({'old-workflow'}, {'old-workflow': 0, 'independent-reader': 0})
    assert catalog_authority_rows(storage) == before
    assert path.read_bytes() == generation_bytes


def test_manager_memory_preparation_leaves_absent_catalog_absent(tmp_path):
    prepare_workflow_memory_references(tmp_path)
    assert not MemoryStorage(tmp_path).root.exists()


@pytest.mark.parametrize('blocked', ['incomplete_migration', 'missing_head', 'unknown_generation'])
def test_manager_memory_preparation_rejects_unready_storage_before_catalog_upgrade(
        tmp_path, legacy_catalog, blocked):
    storage = legacy_catalog
    generation_bytes = storage.path(storage.current()).read_bytes()
    if blocked == 'incomplete_migration':
        (storage.root / 'embedding-migration.json').write_text(json.dumps({'stage': 'indexing'}))
        message = 'Embedding migration is incomplete'
    else:
        with storage.connection(write=True) as connection:
            if blocked == 'missing_head':
                connection.execute('DELETE FROM memory_head')
            else:
                connection.execute("UPDATE memory_head SET generation_id='missing-generation'")
        message = 'existing published Memory generation'
    before = catalog_authority_rows(storage)
    with pytest.raises(RuntimeError, match=message):
        prepare_workflow_memory_references(tmp_path)
    assert catalog_authority_rows(storage) == before
    with storage.connection() as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name IN "
                                      "('workflow_memory_owners','generation_bindings')").fetchone()
    assert storage.path('initial').read_bytes() == generation_bytes
    assert not storage.path('missing-generation').parent.exists()
