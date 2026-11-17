"""Actual web runtime requests retain their resources through Manager shutdown."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from unittest.mock import AsyncMock

import pytest

from pal.bunshin.manager import BunshinManager, BunshinRunState
from pal.shared import BunshinInvocationPack
from pal.foundation.sqlite_database import OwnedSqliteDatabase
from pal.web_search.contracts import WebSearchQueryResult


def add_run(manager):
    run = BunshinRunState(bunshin_id='web-session', run_id='web-run',
        pack=BunshinInvocationPack(invocation_id='web-invocation', allowed_capabilities=['op_web_search']))
    manager.runs[run.run_id] = run
    return run


@pytest.mark.parametrize('failure_at', ['database', 'web_fetch'])
def test_failed_shutdown_retries_only_failed_resources(tmp_path, monkeypatch, failure_at):
    manager = BunshinManager(tmp_path)
    monkeypatch.setattr(manager.semantic_orchestrator, 'stop_background_workers', AsyncMock())
    calls = []
    async def scenario():
        bundle = await manager._host_tool_runtime_bundle()
        database = bundle.database.peewee_db
        connection = await asyncio.to_thread(database.connection)
        callbacks = {}
        for name in ('execution', 'web_fetch'):
            module = bundle.context.module_registry.require(name)
            callback = module.shutdown_async
            callbacks[name] = callback
            async def shutdown(name=name, callback=callback):
                calls.append(name)
                if name == failure_at and calls.count(name) == 1:
                    raise OSError('transient shutdown failure')
                await callback()
            monkeypatch.setattr(module, 'shutdown_async', shutdown)
        close = bundle.database.close
        def close_database():
            calls.append('database')
            if failure_at == 'database' and calls.count('database') == 1:
                raise OSError('transient shutdown failure')
            close()
        monkeypatch.setattr(bundle.database, 'close', close_database)
        try:
            with pytest.raises(ExceptionGroup):
                await manager.close_all()
            assert manager._host_tool_bundle is bundle
            await manager.close_all()
            await manager.close_all()
            assert manager._host_tool_bundle is None
            assert calls.count(failure_at) == 2
            for name in ('execution', 'web_fetch', 'database'):
                if name != failure_at:
                    assert calls.count(name) == 1
            with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
                connection.execute('SELECT 1')
        finally:
            bundle.database.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel_request,cancel_shutdown', [(False, False), (True, False), (False, True)])
def test_shutdown_drains_concurrent_requests_before_closing_the_database(tmp_path, monkeypatch, cancel_request, cancel_shutdown):
    manager = BunshinManager(tmp_path)
    run = add_run(manager)
    monkeypatch.setattr(manager.semantic_orchestrator, 'stop_background_workers', AsyncMock())
    entered, release, database_closed = threading.Event(), threading.Event(), threading.Event()
    count_lock = threading.Lock()
    observations = []
    count = 0
    async def scenario():
        nonlocal count
        bundle = await manager._host_tool_runtime_bundle()
        close_database = bundle.database.close
        def close():
            database_closed.set()
            close_database()
        monkeypatch.setattr(bundle.database, 'close', close)
        service = bundle.context.module_registry.require('web_search').introspection_provider.service
        def query(request):
            nonlocal count
            with count_lock:
                count += 1
                if count == 2:
                    entered.set()
            assert release.wait(5)
            observations.append(database_closed.is_set())
            return WebSearchQueryResult()
        monkeypatch.setattr(service, 'query', query)
        calls = [asyncio.create_task(manager.web_broker_search(
            {'run_id': run.run_id, 'args': {'query': 'lifecycle'}})) for _ in range(2)]
        closing = None
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            if cancel_request:
                calls[0].cancel()
                await asyncio.sleep(0)
                calls[0].cancel()
            run.status = 'completed'
            closing = asyncio.create_task(manager.close_all())
            await asyncio.sleep(0.02)
            if cancel_shutdown:
                closing.cancel()
                await asyncio.sleep(0)
                closing.cancel()
                await asyncio.sleep(0.01)
            assert manager._host_tools_closing
            assert not closing.done()
            assert not database_closed.is_set()
            with pytest.raises(RuntimeError, match='closing'):
                await manager.web_broker_search({'run_id': run.run_id, 'args': {'query': 'too late'}})
        finally:
            release.set()
            results = await asyncio.gather(*calls, return_exceptions=True)
            run.status = 'completed'
            if closing is not None:
                if cancel_shutdown:
                    with pytest.raises(asyncio.CancelledError):
                        await closing
                else:
                    await closing
            await manager.close_all()
        assert observations == [False, False]
        if cancel_request:
            assert isinstance(results[0], asyncio.CancelledError)
        else:
            assert results[0]['ok']
        assert results[1]['ok']
        assert database_closed.is_set()
        assert manager._host_tool_bundle is None
        with pytest.raises(RuntimeError, match='closing'):
            await manager.web_broker_search({'run_id': run.run_id, 'args': {'query': 'closed'}})
        with pytest.raises(RuntimeError, match='closing'):
            await manager._host_tool_runtime_bundle()
        assert manager._host_tool_bundle is None
    asyncio.run(scenario())


def test_runtime_close_finishes_once_through_repeated_cancellation(tmp_path, monkeypatch):
    manager = BunshinManager(tmp_path)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        bundle = await manager._host_tool_runtime_bundle()
        module = bundle.context.module_registry.require('web_fetch')
        shutdown = module.shutdown_async
        calls = []
        database_closed = asyncio.Event()
        close_database = bundle.database.close
        def close():
            database_closed.set()
            close_database()
        monkeypatch.setattr(bundle.database, 'close', close)
        async def blocked_shutdown():
            calls.append('close')
            entered.set()
            await release.wait()
            await shutdown()
        monkeypatch.setattr(module, 'shutdown_async', blocked_shutdown)
        closing = asyncio.create_task(bundle.close())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(0.01)
            assert not closing.done()
            assert not database_closed.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await bundle.close()
        assert calls == ['close']
        assert database_closed.is_set()
        await manager.close_all()
    asyncio.run(scenario())


def test_admitted_request_finishes_lazy_construction_after_shutdown_starts(tmp_path, monkeypatch):
    from pal.bunshin.runner_components import host_web_runtime
    manager = BunshinManager(tmp_path)
    run = add_run(manager)
    monkeypatch.setattr(manager.semantic_orchestrator, 'stop_background_workers', AsyncMock())
    started, release, closed = threading.Event(), threading.Event(), threading.Event()
    created = []
    build = host_web_runtime.build_host_web_runtime
    def paused_build(root):
        bundle = build(root)
        created.append(bundle)
        started.set()
        assert release.wait(5)
        return bundle
    monkeypatch.setattr(host_web_runtime, 'build_host_web_runtime', paused_build)
    async def scenario():
        request = asyncio.create_task(manager.web_broker_search(
            {'run_id': run.run_id, 'args': {'query': 'already admitted'}}))
        closing = None
        try:
            assert await asyncio.to_thread(started.wait, 3)
            bundle = created[0]
            close_database = bundle.database.close
            def close():
                closed.set()
                close_database()
            monkeypatch.setattr(bundle.database, 'close', close)
            service = bundle.context.module_registry.require('web_search').introspection_provider.service
            monkeypatch.setattr(service, 'query', lambda request: WebSearchQueryResult())
            run.status = 'completed'
            closing = asyncio.create_task(manager.close_all())
            await asyncio.sleep(0.02)
            assert not closing.done()
            assert manager._host_tool_bundle is None
            assert not closed.is_set()
        finally:
            release.set()
            result = await request
            run.status = 'completed'
            if closing is not None:
                await closing
            await manager.close_all()
        assert result['ok']
        assert len(created) == 1
        assert closed.is_set()
        assert manager._host_tool_bundle is None
    asyncio.run(scenario())


def test_shutdown_physically_closes_connections_opened_by_real_web_sql(tmp_path, monkeypatch):
    manager = BunshinManager(tmp_path)
    run = add_run(manager)
    monkeypatch.setattr(manager.semantic_orchestrator, 'stop_background_workers', AsyncMock())
    connections = []
    connect = OwnedSqliteDatabase._connect
    def record_connection(self):
        connection = connect(self)
        if str(self.database) == str(tmp_path / 'pal.sqlite3'):
            connections.append((threading.get_ident(), connection))
        return connection
    monkeypatch.setattr(OwnedSqliteDatabase, '_connect', record_connection)
    async def scenario():
        try:
            # The unconfigured provider path still reads actual settings and
            # provider tables on the execution worker, without network access.
            await manager.web_broker_search({'run_id': run.run_id, 'args': {'query': 'connection ownership'}})
        finally:
            run.status = 'completed'
            await manager.close_all()
        assert len(connections) >= 2
        assert len({thread for thread, _ in connections}) >= 2
        for _, connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
                connection.execute('SELECT 1')
    try:
        asyncio.run(scenario())
    finally:
        for _, connection in connections:
            connection.close()
