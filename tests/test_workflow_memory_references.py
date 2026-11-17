"""Workflow-owned generation retention and failure cleanup with real SQLite stores."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from pal.bunshin.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.manager import BunshinManager
from pal.bunshin.memory_binding import workflow_memory_binding, reconcile_workflow_memory_references
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.runner_components import runtime_build
from pal.foundation import utc_now
from pal.memory.contracts import L3CommitRequest
from pal.memory.embedding import HashingEmbedder
from pal.memory.service import MemoryService
from pal.memory.storage import MemoryStorage
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin


@pytest.fixture
def generation(tmp_path):
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    repository = storage.open()
    provider = SQLiteVecL3Plugin(MemoryService(), repository=repository, embedding_provider=HashingEmbedder(dimension=24))
    yield storage, provider
    provider.repository.close()


def store_workflow(repository, workflow_id, state):
    stamp = utc_now()
    with repository.database.write_connection() as connection:
        repository.snapshots.write_snapshot_locked(connection, None, AggregateSnapshot(
            aggregate_type=AggregateType.WORKFLOW, aggregate_id=workflow_id, workflow_id=workflow_id,
            state=state, version=1, payload={}, created_at=stamp, updated_at=stamp))


def test_workflow_borrows_its_generation_without_copying_and_observes_same_generation_writes(tmp_path, generation):
    storage, provider = generation
    repository = BunshinRepository(tmp_path)
    with workflow_memory_binding(tmp_path, repository, 'workflow'):
        store_workflow(repository, 'workflow', 'CREATED')
    memory = runtime_build.build_role_memory(tmp_path, service=MemoryService(), workflow_id='workflow')
    try:
        assert Path(memory.provider.repository.database.database.removeprefix('file:').removesuffix('?mode=ro')) == storage.path(storage.current())
        saved = provider.commit(L3CommitRequest(kind='fact', summary='Added after binding', search_text='after binding'))
        assert memory.provider.repository.get_document(saved.document_id) is not None
        assert memory.provider.embedding_provider.dimension == 24
        assert memory.provider.read_only
        assert not (storage.root / 'workflow_snapshots').exists()
    finally:
        memory.close()


def test_terminal_workflow_releases_without_dreaming_and_preserves_generic_reader_pins(tmp_path, generation):
    storage, _ = generation
    repository = BunshinRepository(tmp_path)
    with workflow_memory_binding(tmp_path, repository, 'workflow'):
        store_workflow(repository, 'workflow', 'COMPLETED')
    storage.pin('independent-reader')
    reconcile_workflow_memory_references(tmp_path)
    with pytest.raises(RuntimeError, match='no live'):
        storage.pinned('workflow')
    assert storage.pinned('independent-reader') == storage.current()


def test_reconciliation_cannot_release_a_reference_still_being_registered(tmp_path, generation):
    storage, _ = generation
    repository = BunshinRepository(tmp_path)
    with workflow_memory_binding(tmp_path, repository, 'workflow'):
        reconcile_workflow_memory_references(tmp_path)
        assert storage.pinned('workflow') == storage.current()
        store_workflow(repository, 'workflow', 'CREATED')
    reconcile_workflow_memory_references(tmp_path)
    assert storage.pinned('workflow') == storage.current()


def test_crashed_registration_is_reconciled_without_leaving_a_generation_reference(tmp_path, generation):
    storage, _ = generation
    BunshinRepository(tmp_path).database.ensure_schema()
    script = '''import os,sys
from pathlib import Path
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.memory_binding import workflow_memory_binding
root=Path(sys.argv[1])
with workflow_memory_binding(root,BunshinRepository(root),'interrupted'):
    os._exit(23)
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 23, result.stderr
    reconcile_workflow_memory_references(tmp_path)
    with pytest.raises(RuntimeError, match='no live'):
        storage.pinned('interrupted')


def test_failed_role_construction_closes_the_generation_reader(tmp_path, generation, monkeypatch):
    storage, _ = generation
    storage.pin('workflow')
    owned = []
    original = runtime_build.build_role_memory
    def remember(*args, **kwargs):
        result = original(*args, **kwargs)
        owned.append(result)
        return result
    monkeypatch.setattr(runtime_build, 'build_role_memory', remember)
    monkeypatch.setattr('pal.execution.worker_extensions.activate_worker_extension', lambda *args, **kwargs: None)
    def fail(*args):
        raise RuntimeError('publication failed')
    monkeypatch.setattr(runtime_build, 'register_l3_with_core', fail)
    with pytest.raises(RuntimeError, match='publication failed'):
        runtime_build.build_slim_bunshin_runtime(tmp_path, llm_authority='none', memory_workflow_id='workflow')
    assert owned[0].provider.repository.database.is_closed()
    assert owned[0].provider.repository.connection_lease is None


def test_manager_workflow_creation_does_not_block_its_loop_and_drains_cancellation(tmp_path, monkeypatch):
    manager = BunshinManager(tmp_path)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def create(params):
        started.set()
        assert release.wait(5)
        finished.set()
        return {'status': 'created'}
    monkeypatch.setattr(manager.workflow_service, 'start_workflow', create)
    async def scenario():
        task = asyncio.create_task(manager._call_method('v2_start_workflow', {}))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        await manager.close_all()
    asyncio.run(scenario())


def test_manager_builds_one_real_web_runtime_without_workflow_memory(tmp_path, generation, monkeypatch):
    manager = BunshinManager(tmp_path)
    async def scenario():
        first, second = await asyncio.gather(manager._host_tool_runtime_bundle(), manager._host_tool_runtime_bundle())
        try:
            assert first is second
            assert set(first.context.module_registry.modules) == {'execution', 'web_search', 'web_fetch'}
            aliases = first.execution_runtime.registry_generation.direct_aliases
            assert aliases['search_web'].canonical_path == 'op_web_search'
            assert aliases['read_browser_page'].canonical_path == 'op_browser_read'
        finally:
            await manager.close_all()
    asyncio.run(scenario())
