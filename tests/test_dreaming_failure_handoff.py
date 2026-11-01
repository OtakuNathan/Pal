"""Silent dreaming failures reuse durable once tasks and resident L1; no network."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from pal.core import PalCore
from pal.foundation import PalV2Database, EventEnvelope
from pal.memory import MemoryService
from pal.memory.contracts import L3CommitRequest
from pal.memory.dreaming.contracts import DreamingConfig
from pal.memory.dreaming.runtime import DreamingEventSource
from pal.memory.dreaming.service import DreamingService
from pal.memory.embedding import HashingEmbedder
from pal.memory.storage import MemoryStorage
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin
from pal.proactive import ProactiveManager, ProactiveRepository, ProactiveRunner
from pal.shared.proactive_protocol import SILENT_ONCE_SCHEDULER
from pal.proactive.models import ProactiveDefinitionModel, ProactiveRunModel
from tests.test_memory_dreaming import ReviewingLLM


@pytest.fixture
def env(tmp_path):
    db = PalV2Database(tmp_path / "runtime.sqlite3")
    db.initialize([ProactiveDefinitionModel, ProactiveRunModel])
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    provider = SQLiteVecL3Plugin(service=MemoryService(), repository=storage.open(), embedder=HashingEmbedder())
    request = L3CommitRequest(kind="fact", title="Explicit APIs", summary="Prefer explicit public APIs.",
                             search_text="explicit public APIs", topics=["API"])
    provider.commit(request); provider.commit(replace(request, title="API design"))
    core = PalCore()
    manager = ProactiveManager(repository=ProactiveRepository())
    core.context.port_registry.register(SILENT_ONCE_SCHEDULER, manager)
    service = DreamingService(storage=storage, provider=provider, llm=ReviewingLLM(fail_review=True), core=core)
    yield core, manager, service
    provider.repository.close()
    core.context.execution_runtime.shutdown()
    db.close()


def fail(service):
    async def run():
        with patch.object(service, "_resolve_main_provider"):
            return await service.run()
    return asyncio.run(run())


def test_failure_is_durable_before_once_task_and_does_not_resolve_old_route(env):
    core, manager, service = env
    head = service.storage.current()
    original = manager.ensure_silent_once
    def register(**kwargs):
        # Registration happens only after recovery, with the full original
        # failure already durable in both the catalog and private JSON log.
        assert core.state.memory_maintenance is False
        status = service.status()
        assert status["status"] == "failed"
        log = Path(status["report"]["failure_log"])
        assert json.loads(log.read_text())["run_id"] == status["run_id"]
        assert log.stat().st_mode & 0o777 == 0o600
        return original(**kwargs)
    with patch.object(manager, "ensure_silent_once", side_effect=register), \
         patch.object(core, "memory_notification_route", side_effect=AssertionError("must not use stale routes")), \
         patch.object(core, "deliver_memory_notice_async", side_effect=AssertionError("must not send notifications")):
        result = fail(service)
    assert result["status"] == "failed"
    assert service.storage.current() == head
    assert not service.provider.repository.frozen
    task, = manager.registered.values()
    assert task.out_channel_id == "null" and not task.out_reply_target
    assert task.schedule["cadence"] == "once"
    assert result["run_id"] in task.method
    assert "review endpoint failed" in task.method
    assert "NOT been notified" in task.method
    assert service.status()["report"]["failure_reminder"]["scheduled"] is True


def test_restart_recovers_logged_failure_when_scheduler_was_unavailable(env):
    core, manager, service = env
    del core.context.port_registry[SILENT_ONCE_SCHEDULER.name]
    result = fail(service)
    assert not manager.registered
    assert service.status()["report"]["failure_reminder"] == {"scheduled": False}
    restarted_manager = ProactiveManager(repository=ProactiveRepository())
    core.context.port_registry.register(SILENT_ONCE_SCHEDULER, restarted_manager)
    restarted = DreamingService(storage=service.storage, provider=service.provider, llm=service.llm, core=core)
    # The automatic dreaming schedule is disabled, but pending failure handoff
    # still reconciles through the same registered event source.
    assert restarted.config.enabled is False
    assert DreamingEventSource(restarted).prepare(core.context) is False
    task, = restarted_manager.registered.values()
    assert result["run_id"] in task.proactive_id
    persisted, = ProactiveRepository().list_definitions()
    assert persisted.next_due_at_utc is not None
    assert persisted.definition.out_channel_id == "null"


def test_once_cursor_survives_restart_and_completed_task_is_not_rearmed(env):
    core, manager, service = env
    result = fail(service)
    task_id, = manager.registered
    stored, = ProactiveRepository().list_definitions()
    restarted = ProactiveManager(repository=ProactiveRepository())
    restarted.hydrate(stored.definition, next_due_at_utc=stored.next_due_at_utc)
    due = restarted.enqueue_due_triggers(now_utc=datetime.now(timezone.utc)+timedelta(seconds=1))
    assert len(due) == 1
    restarted.mark_run_completed(task_id)
    # Model a crash after creating the task but before updating the dreaming
    # report. Existing completed once definitions must not be overwritten.
    report = service.status()["report"]
    report["failure_reminder"] = {"scheduled": False}
    with service.storage.connection(write=True) as connection:
        connection.execute("UPDATE dreaming_runs SET report_json=? WHERE run_id=?", (json.dumps(report), result["run_id"]))
    core.context.port_registry.register(SILENT_ONCE_SCHEDULER, restarted)
    service.reconcile_failure_reminders()
    completed, = ProactiveRepository().list_definitions()
    assert completed.next_due_at_utc is None
    assert completed.last_run_at_utc is not None
    assert not restarted.enqueue_due_triggers(now_utc=datetime.now(timezone.utc)+timedelta(days=1))


def test_restart_interruption_records_failure_without_touching_published_memory(env):
    core, manager, service = env
    head = service.storage.current()
    run_id = service.create_run()
    service._phase(run_id, "refining", {"outcome": "failed", "processed_groups": 1})
    service.recover_interrupted_runs()
    assert service.status()["status"] == "failed"
    assert "host restart" in service.status()["report"]["error"]
    assert Path(service.status()["report"]["failure_log"]).is_file()
    service.reconcile_failure_reminders()
    assert len(manager.registered) == 1
    service.recover_interrupted_runs(); service.reconcile_failure_reminders()
    assert len(manager.registered) == 1
    assert service.storage.current() == head


def test_silent_proactive_result_survives_l1_restore_and_enters_next_chat(tmp_path):
    from types import SimpleNamespace
    from pal.channel import ChannelRuntime
    from pal.channel.contracts import EndpointConfig
    from pal.plugins_builtin.null_channel.endpoint import NullChannelEndpoint
    from pal.proactive import register_with_core as register_proactive
    from pal.proactive.handler import ProactiveTriggerHandler
    from pal.memory.dreaming.contracts import DreamingFailureNotice
    from pal.runtime_app import PalRuntimeApp
    from pal.shared import EventKind, ProactiveTriggerEvent
    from tests.test_proactive_context_isolation import setup_core, begin, prompt
    from tests.test_v3_n3_vertical_trace import _runtime, CapturingTransport
    from tests.test_resident_checkpoint import _build_app

    async def run():
        core, memory, _ = setup_core(tmp_path)
        transport = CapturingTransport(["Pending notice: dreaming failed; user has not been told. See failure.json."])
        core.context.port_registry['llm:llm'] = _runtime(transport)
        channel = ChannelRuntime()
        sink = NullChannelEndpoint(endpoint=EndpointConfig('null', 'null', '/dev/null'))
        channel.register_endpoint(sink)
        core.context.port_registry['channel:channel'] = channel
        manager = ProactiveManager()
        register_proactive(core.context, manager)
        notice = DreamingFailureNotice('dream-fixture', '2026-11-01T00:00:00+00:00',
                                       'review failed', '/private/failure.json', False, True)
        task_id = core.schedule_memory_failure_reminder(notice)
        handler = ProactiveTriggerHandler(manager, ProactiveRunner())
        handler.handle(EventEnvelope(event_kind=EventKind.PROACTIVE_TRIGGER, source_kind='proactive',
            payload=ProactiveTriggerEvent(task_id, 'scheduled')), core.context)
        assert handler.tasks
        await asyncio.gather(*tuple(handler.tasks))
        assert handler.runner.results[-1].get('status') != 'failed', handler.runner.results
        assert len(transport.captured) == 1
        assert 'dream-fixture' in repr(memory.history.turns)
        assert 'Pending notice: dreaming failed' in repr(memory.history.turns)
        assert manager.schedule_engine.next_due_at(task_id) is None
        channel.flush_outbox()
        assert not sink.outbox
        # Use the resident checkpoint owner rather than introducing any special
        # failure persistence or notification-consumption store.
        app = PalRuntimeApp(handle=SimpleNamespace(configuration_service=None, core=core, memory_service=memory,
            registration=SimpleNamespace(runtime=SimpleNamespace(runtime_root=tmp_path))))
        await app._publish_checkpoint_async()
        restored = _build_app(tmp_path)
        await restored._restore_checkpoint_async()
        assert restored.last_checkpoint_status == 'restored', restored.last_checkpoint_error
        resumed = restored.handle.core
        next_chat = begin(resumed, restored.handle.memory_service, 'next-user', 'What happened?')
        request = prompt(resumed, next_chat)
        assert 'Pending notice: dreaming failed' in repr(request.messages)
        assert 'dream-fixture' in repr(request.messages)
        core.context.execution_runtime.shutdown()
        resumed.context.execution_runtime.shutdown()
    asyncio.run(run())
