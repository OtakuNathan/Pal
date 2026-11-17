"""User-visible sleep ordering and recovery through the common generation owner."""
import asyncio
import sqlite3
from unittest.mock import AsyncMock

from peewee import SqliteDatabase

from pal.control.contracts import ControlRoute
from pal.control.runtime_contracts import CORE_CONTROL
from pal.memory.generation_maintenance import BACKEND_SLEEP_NOTICE
from pal.memory.swap_store import SwapStore
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin
from tests.test_memory_swap import swap_runtime, settled, fact


def test_partial_binding_cannot_wake_before_selection_and_retains_failed_closes(swap_runtime, monkeypatch):
    core, memory, storage, _, target, command = swap_runtime
    previous = target.repository
    lease = previous.connection_lease
    database = previous.database
    physical_close = SqliteDatabase._close
    def unavailable(self, connection):
        if self is database:
            raise sqlite3.OperationalError('old generation close unavailable')
        physical_close(self, connection)
    monkeypatch.setattr(SqliteDatabase, '_close', unavailable)
    async def scenario():
        job = (await command('swap', name='replacement'))['state']['job_id']
        failed = await settled(command, job)
        assert failed['state']['phase'] == 'committed'
        assert core.state.memory_maintenance
        assert storage.active_provider_id() == 'replacement'
        assert memory.l3_selector.active_provider_id != 'replacement'
        assert not memory.is_l3_generation_bound('replacement', storage.current())
        assert not lease.closed
        assert previous in target._retired_repositories
        monkeypatch.setattr(SqliteDatabase, '_close', physical_close)
        await command('swap-resume', job_id=job)
        result = await settled(command, job)
        assert result['state']['phase'] == 'complete', result
        assert memory.is_l3_generation_bound('replacement', storage.current())
        assert not core.state.memory_maintenance
        assert lease.closed
        assert not target._retired_repositories
    try:
        asyncio.run(scenario())
    finally:
        monkeypatch.setattr(SqliteDatabase, '_close', physical_close)
        target.close()


def test_failed_binding_preparation_preserves_original_repository_and_recovers(swap_runtime, monkeypatch):
    core, memory, storage, _, target, command = swap_runtime
    previous = target.repository
    load = SQLiteVecL3Plugin._load_embedding_space
    def fail_candidate(self, **kwargs):
        if self.repository.generation_id != previous.generation_id:
            raise OSError('binding configuration unavailable')
        load(self, **kwargs)
    monkeypatch.setattr(SQLiteVecL3Plugin, '_load_embedding_space', fail_candidate)
    async def scenario():
        job = (await command('swap', name='replacement'))['state']['job_id']
        failed = await settled(command, job)
        assert failed['state']['phase'] == 'committed'
        assert target.repository is previous
        assert core.state.memory_maintenance
        assert not memory.is_l3_generation_bound('replacement', storage.current())
        monkeypatch.setattr(SQLiteVecL3Plugin, '_load_embedding_space', load)
        await command('swap-resume', job_id=job)
        assert (await settled(command, job))['state']['phase'] == 'complete'
        assert memory.is_l3_generation_bound('replacement', storage.current())
        assert not core.state.memory_maintenance
    asyncio.run(scenario())


def test_backend_change_delivers_notice_before_sleep(swap_runtime, monkeypatch):
    core, _, storage, source, _, command = swap_runtime
    source.commit(fact('keep this memory'))
    host = core.context.require_port(CORE_CONTROL)
    events = []
    monkeypatch.setattr(host, 'last_user_route', lambda: ControlRoute('synthetic', 'stdio'))
    async def deliver(delivery, *, require_provider, wait):
        assert delivery.text == BACKEND_SLEEP_NOTICE
        assert require_provider and wait
        assert not source.repository.frozen
        events.append('notice')
        return True
    monkeypatch.setattr(host, 'deliver', deliver)
    monkeypatch.setattr(host, 'flush', AsyncMock())
    begin = host.begin_memory_sleep
    def sleep(token, **kwargs):
        events.append('sleep')
        begin(token, **kwargs)
    monkeypatch.setattr(host, 'begin_memory_sleep', sleep)
    async def scenario():
        started = await command('swap', name='replacement')
        assert started['message'] == BACKEND_SLEEP_NOTICE
        result = await settled(command, started['state']['job_id'])
        assert result['state']['phase'] == 'complete', result
        assert events == ['notice', 'sleep']
        assert not core.memory_maintenance_busy()
    asyncio.run(scenario())


def test_failed_notice_keeps_original_generation_and_reopens_admission(swap_runtime, monkeypatch):
    core, _, storage, source, _, command = swap_runtime
    original = storage.current()
    host = core.context.require_port(CORE_CONTROL)
    monkeypatch.setattr(host, 'last_user_route', lambda: ControlRoute('synthetic', 'stdio'))
    monkeypatch.setattr(host, 'deliver', AsyncMock(return_value=False))
    sleep = AsyncMock()
    monkeypatch.setattr(host, 'begin_memory_sleep', sleep)
    async def scenario():
        job = (await command('swap', name='replacement'))['state']['job_id']
        result = await settled(command, job)
        assert result['status'] == 'error'
        assert 'sleep notice' in result['state']['error']
        assert storage.current() == original
        assert not source.repository.frozen
        assert not core.memory_maintenance_busy()
        sleep.assert_not_called()
    asyncio.run(scenario())


def test_committed_mount_failure_stays_asleep_until_same_job_recovers(swap_runtime, monkeypatch):
    core, memory, storage, source, _, command = swap_runtime
    original = storage.current()
    source.commit(fact('must survive binding failure'))
    adopt = memory.adopt_l3_generation
    def fail(*args):
        raise OSError('new connection unavailable')
    monkeypatch.setattr(memory, 'adopt_l3_generation', fail)
    async def scenario():
        job = (await command('swap', name='replacement'))['state']['job_id']
        failed = await settled(command, job)
        assert failed['state']['phase'] == 'committed', failed
        current = storage.current()
        assert current != original
        assert core.state.memory_maintenance
        assert not source.repository.owns_write_fence
        monkeypatch.setattr(memory, 'adopt_l3_generation', adopt)
        await command('swap-resume', job_id=job)
        result = await settled(command, job)
        assert result['state']['phase'] == 'complete', result
        assert storage.current() == current
        assert not core.state.memory_maintenance
    asyncio.run(scenario())


def test_publication_rejects_content_changed_after_verification(swap_runtime, monkeypatch):
    _, _, storage, source, _, command = swap_runtime
    original = storage.current()
    saved = source.commit(fact('approved original content'))
    publish = SwapStore.publish
    def tamper(store, job, source_repository, candidate):
        with candidate.write_transaction():
            candidate.database.execute_sql('UPDATE memory_facts SET summary=?', ('unreviewed change',))
        publish(store, job, source_repository, candidate)
    monkeypatch.setattr(SwapStore, 'publish', tamper)
    async def scenario():
        job = (await command('swap', name='replacement'))['state']['job_id']
        result = await settled(command, job)
        assert result['status'] == 'error'
        assert storage.current() == original
        assert source.repository.get_document(saved.document_id)['summary'] == 'approved original content'
    asyncio.run(scenario())


def test_dreaming_uses_the_new_active_backend_after_a_swap(swap_runtime):
    from pal.memory.dreaming.service import DreamingService
    from tests.test_memory_dreaming import ReviewingLLM
    from dataclasses import replace
    core, memory, storage, source, target, command = swap_runtime
    request = fact('Nathan prefers explicit public APIs', topics=['API'])
    source.commit(request)
    source.commit(replace(request, title='API design'))
    dreaming = DreamingService(storage=storage, provider=source, memory=memory,
        llm=ReviewingLLM(), host=core.context.require_port(CORE_CONTROL))
    async def scenario():
        job = (await command('swap', name='replacement'))['state']['job_id']
        assert (await settled(command, job))['state']['phase'] == 'complete'
        result = await dreaming.run()
        assert result['status'] == 'completed', result
        assert dreaming.provider is target
        assert storage.active_provider_id() == 'replacement'
        with storage.connection() as catalog:
            rows = catalog.execute("SELECT state_json FROM memory_swap_jobs WHERE candidate_generation=?", (storage.current(),)).fetchall()
        assert len(rows) == 1
        import json
        assert json.loads(rows[0][0])['dreaming_run_id'] == result['run_id']
    asyncio.run(scenario())


def test_index_recovery_cannot_release_a_dreaming_admission_still_in_use(swap_runtime, monkeypatch):
    import threading
    from dataclasses import replace
    from pal.memory.dreaming.service import DreamingService
    from pal.memory.swap import MemorySwapManager
    from tests.test_memory_dreaming import ReviewingLLM
    core, memory, storage, source, _, _ = swap_runtime
    request = fact('Nathan prefers explicit public APIs', topics=['API'])
    source.commit(request)
    source.commit(replace(request, title='API design'))
    old = storage.current()
    collecting, release, indexed = threading.Event(), threading.Event(), threading.Event()
    collect = storage.collect
    finish = source.finish_indexing
    def blocked_collect(**kwargs):
        if storage.current() != old and storage.is_frozen(old):
            collecting.set()
            assert release.wait(5)
        return collect(**kwargs)
    def finish_indexing(generation):
        finish(generation)
        indexed.set()
    monkeypatch.setattr(storage, 'collect', blocked_collect)
    monkeypatch.setattr(source, 'finish_indexing', finish_indexing)
    manager = MemorySwapManager(memory, core.context.port_registry.observe(CORE_CONTROL))
    tasks = []
    def recover():
        tasks.append(asyncio.create_task(manager.recover_pending()))
    monkeypatch.setattr(memory, 'on_generation_work', recover)
    dreaming = DreamingService(storage=storage, provider=source, memory=memory,
        llm=ReviewingLLM(), host=core.context.require_port(CORE_CONTROL))
    async def scenario():
        running = asyncio.create_task(dreaming.run())
        try:
            assert await asyncio.to_thread(collecting.wait, 3)
            assert await asyncio.to_thread(indexed.wait, 3)
            assert core.state.memory_maintenance
            assert storage.is_frozen(old)
        finally:
            release.set()
            result = await running
            await asyncio.gather(*tasks)
            await manager.close()
        assert result['status'] == 'completed', result
        assert not core.state.memory_maintenance
    asyncio.run(scenario())


def test_old_maintenance_cannot_release_a_new_owner_after_recovery(swap_runtime, monkeypatch):
    import threading
    from pal.memory.generation_maintenance import GenerationMaintenance
    from pal.memory.swap import MemorySwapManager
    from pal.memory.swap_state import SwapEvent
    core, memory, storage, source, target, _ = swap_runtime
    original = source.repository
    thaw = original.thaw
    unlocked, finish_thaw = threading.Event(), threading.Event()
    async def scenario():
        store = SwapStore(storage)
        job = store.create(source.provider_id, target.provider_id, target.migration_identity,
                           source_generation=storage.current())
        store.advance(job, SwapEvent.SNAPSHOT)
        candidate = store.capture(job)
        target.prepare_generation(candidate)
        store.advance(job, SwapEvent.COPIED)
        store.catch_up(job, candidate)
        host = core.context.require_port(CORE_CONTROL)
        first = GenerationMaintenance(storage=storage, source=original, host=host,
            bind=lambda: memory.adopt_l3_generation(target.provider_id, storage.current()),
            bound=lambda: target.repository.generation_id == storage.current(), component=job.component)
        await first.enter()
        store.catch_up(job, candidate)
        store.verify(job, original, candidate)
        await first.publish(store, job, candidate)
        first_token = first.admission_token
        def paused_thaw():
            thaw()
            unlocked.set()
            assert finish_thaw.wait(5)
        monkeypatch.setattr(original, 'thaw', paused_thaw)
        closing = asyncio.create_task(first.close())
        second = None
        try:
            assert await asyncio.to_thread(unlocked.wait, 3)
            recovery = MemorySwapManager(memory, core.context.port_registry.observe(CORE_CONTROL))
            await recovery._index(store, store.load(job.job_id))
            assert not core.state.memory_maintenance
            second = GenerationMaintenance(storage=storage, source=target.repository, host=host,
                bind=lambda: None, bound=lambda: True, component='second-maintenance')
            await second.enter()
            assert second.admission_token is not first_token
            finish_thaw.set()
            await closing
            assert second.source.owns_write_fence
            assert core.state.memory_maintenance
            assert host.snapshot().memory_maintenance_token is second.admission_token
            assert core.state.memory_maintenance_component == 'second-maintenance'
        finally:
            finish_thaw.set()
            await closing
            if second is not None:
                await second.close()
            candidate.close()
    asyncio.run(scenario())
