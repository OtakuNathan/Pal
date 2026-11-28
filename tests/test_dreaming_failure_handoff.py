"""Silent dreaming failures reuse durable once tasks and resident L1; no network."""
from __future__ import annotations
from tests.core_factory import build_core

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from pal.core.runtime import PalCore
from pal.foundation import PalV2Database, EventEnvelope
from pal.memory import MemoryService
from pal.memory.contracts import L3CommitRequest
from pal.memory.dreaming.failure_reminders import schedule_failure_reminder
from pal.memory.dreaming.contracts import DreamingConfig
from pal.memory.dreaming.runtime import DreamingEventSource
from pal.memory.dreaming.service import DreamingService
from pal.memory.embedding import HashingEmbedder
from pal.memory.storage import MemoryStorage
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin
from pal.proactive import ProactiveManager, ProactiveRepository
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
    core = build_core()
    manager = ProactiveManager(repository=ProactiveRepository())
    core.context.port_registry.register(SILENT_ONCE_SCHEDULER, manager)
    service = DreamingService(storage=storage, provider=provider, llm=ReviewingLLM(fail_review=True), host=core.control_runtime)
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
    restarted = DreamingService(storage=service.storage, provider=service.provider, llm=service.llm, host=core.control_runtime)
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
    run, = restarted.repository.pending_background_runs()
    restarted.repository.set_background_state(run.proactive_run_id, "completed", workflow_id="wf", result="notice prepared")
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
    from pal.foundation.background_inbox import BackgroundInbox
    from pal.memory.dreaming.contracts import DreamingFailureNotice
    from pal.proactive import register_with_core as register_proactive
    from pal.shared.background_protocol import BACKGROUND_WORKFLOWS
    from tests.test_btw import Model, Output
    from tests.test_interjection_injection import _make_envelope
    from tests.test_resident_checkpoint import _build_app

    async def run():
        app = _build_app(tmp_path)
        core = app.handle.core
        memory = app.handle.memory_service
        resident_model = Model()
        core.context.port_registry["llm:llm"] = resident_model
        manager = ProactiveManager(repository=ProactiveRepository())
        handle = register_proactive(core.context, manager)
        inbox = BackgroundInbox(tmp_path)
        requests = []

        class Backend:
            async def start_scheduled_run(self, request):
                requests.append(request)
                return {"workflow_id": "notice-workflow"}

            async def scheduled_run_status(self, workflow_id):
                assert workflow_id == "notice-workflow"
                inbox.publish(workflow_id, {
                    "result_text": "Pending notice: dreaming failed; user has not been told.",
                    "task_name": "dream-fixture",
                }, delivery_status="discarded")
                return {"workflow": {"workflow_state": "COMPLETED",
                                     "delivery": {"result_text": "Pending notice: dreaming failed"}}}

        core.context.port_registry.register(BACKGROUND_WORKFLOWS, Backend())
        notice = DreamingFailureNotice("dream-fixture", "2026-11-01T00:00:00+00:00",
                                       "review failed", "/private/failure.json", False, True)
        task_id = schedule_failure_reminder(core.control_runtime, notice)
        source, = handle.event_sources
        assert source.prepare(core.context)
        event, = source.drain(core.context)
        handler, = handle.event_handlers[event.event_kind]
        await handler.handle(event, core.context)
        assert requests[0]["discard_output"] is True
        assert requests[0]["delivery_binding"] == {}
        await handler.handle(event, core.context)
        assert manager.repository.latest_run(task_id).status == "completed"
        assert not resident_model.requests
        assert not memory.history.turns
        assert manager.schedule_engine.next_due_at(task_id) is None
        assert len(inbox.pending()) == 1
        await app._publish_checkpoint_async()

        restored = _build_app(tmp_path)
        await restored._restore_checkpoint_async()
        assert restored.last_checkpoint_status == "restored", restored.last_checkpoint_error
        resumed = restored.handle.core
        model = Model()
        resumed.context.port_registry["llm:llm"] = model
        resumed.context.port_registry["channel:channel"] = Output()
        await resumed.schedule_channel_turn_async(_make_envelope(turn_id="next-user", text="What happened?"))
        await asyncio.wait_for(resumed.run_until_idle_async(max_iterations=128), 5)
        assert model.requests
        assert "Pending notice: dreaming failed" in repr(model.requests[0].messages)
        assert "dream-fixture" in repr(model.requests[0].messages)
        assert not inbox.pending()
        assert len(restored.handle.memory_service.history.turns) == 1
        assert sum(m.message_id == "background-result:notice-workflow"
                   for t in restored.handle.memory_service.history.turns for m in t.messages) == 1
        await restored._publish_checkpoint_async()
        again = _build_app(tmp_path)
        await again._restore_checkpoint_async()
        assert not inbox.pending()
        assert sum(m.message_id == "background-result:notice-workflow"
                   for t in again.handle.memory_service.history.turns for m in t.messages) == 1
        core.context.execution_runtime.shutdown()
        resumed.context.execution_runtime.shutdown()
        again.handle.core.context.execution_runtime.shutdown()

    db = PalV2Database(tmp_path / "runtime.sqlite3")
    db.initialize([ProactiveDefinitionModel, ProactiveRunModel])
    try:
        asyncio.run(run())
    finally:
        db.close()
