from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pal.bunshin.contracts import AggregateType
from pal.bunshin.external_products import submit_external_product
from pal.bunshin.managed_workspace import capture_managed_head, migrate_proactive_data, promote_managed_candidate
from pal.bunshin.orchestration import BunshinOutboxProcessor
from pal.bunshin.service import BunshinWorkflowService
from pal.bunshin.semantic_orchestration import SemanticOrchestrator
from pal.bunshin.workspace_git import _git
from pal.foundation.background_inbox import BackgroundInbox


def commit(repo: Path, filename: str, value: str) -> str:
    (repo / filename).write_text(value)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", filename)
    return _git(repo, "rev-parse", "HEAD").strip()


def test_managed_promotion_preserves_foreground_and_is_idempotent(tmp_path):
    repo = tmp_path / "canonical"
    base = capture_managed_head(tmp_path, repo)
    clone = tmp_path / "worker"
    _git(tmp_path, "clone", str(repo), str(clone))
    candidate = commit(clone, "report.md", "background")
    (repo / "foreground.md").write_text("foreground")
    target = promote_managed_candidate(tmp_path, repo, clone, base, candidate, "run-1")
    assert (repo / "report.md").read_text() == "background"
    assert (repo / "foreground.md").read_text() == "foreground"
    (repo / "next.md").write_text("later")
    assert promote_managed_candidate(tmp_path, repo, clone, base, candidate, "run-1") == target
    assert (repo / "next.md").read_text() == "later"


def test_managed_conflict_keeps_both_commits(tmp_path):
    repo = tmp_path / "canonical"
    capture_managed_head(tmp_path, repo)
    base = commit(repo, "report.md", "base")
    clone = tmp_path / "worker"
    _git(tmp_path, "clone", str(repo), str(clone))
    candidate = commit(clone, "report.md", "background")
    (repo / "report.md").write_text("foreground")
    with pytest.raises(RuntimeError, match="conflict"):
        promote_managed_candidate(tmp_path, repo, clone, base, candidate, "run-2")
    assert (repo / "report.md").read_text() == "foreground"
    assert _git(repo, "show", candidate + ":report.md") == "background"


def test_migration_preserves_business_data_and_is_idempotent(tmp_path):
    data = tmp_path / "data"
    (data / "english_learning").mkdir(parents=True)
    (data / "english_learning" / "notes.md").write_text("notes")
    (data / "conversation-history").mkdir()
    (data / "conversation-history" / "history.jsonl").write_text("{}\n")
    migrate_proactive_data(tmp_path)
    migrate_proactive_data(tmp_path)
    assert (data / "english_learning").is_symlink()
    assert (tmp_path / "proactive" / "english_learning" / "notes.md").read_text() == "notes"
    assert (data / "conversation-history" / "history.jsonl").is_file()
    assert (tmp_path / "backups" / "proactive-workspace-v32" / "english_learning" / "notes.md").read_text() == "notes"


def test_inbox_is_durable_and_deduplicated(tmp_path):
    inbox = BackgroundInbox(tmp_path)
    inbox.publish("wf", {"result_text": "hello"}, delivery_status="pending")
    inbox.update_delivery("wf", "delivered")
    inbox.publish("wf", {"result_text": "hello"}, delivery_status="pending")
    assert BackgroundInbox(tmp_path).pending() == [{"workflow_id": "wf", "result_text": "hello", "delivery_status": "delivered"}]
    inbox.acknowledge(["wf"])
    inbox.publish("wf", {"result_text": "hello"}, delivery_status="pending")
    assert BackgroundInbox(tmp_path).pending() == []


def test_external_direct_product_without_worker_delivers_actual_commit(tmp_path):
    service = BunshinWorkflowService(tmp_path / "runtime")
    route = {"channel_id": "null", "channel_kind": "null", "reply_target": {"sink": "null"}}
    response = service.start_workflow({"title": "research", "profile": "general.generic",
        "workspace": {"kind": "managed_git"}, "goal": "Write a note", "execution_mode": "direct",
        "role_overrides": {"implementation": None}, "task_spec": {"deliverable_paths": ["note.md"]},
        "delivery_binding": route})
    workflow_id = response["workflow_id"]
    events = []
    processor = BunshinOutboxProcessor(service, publish_workflow_event=events.append, semantic_effects=SemanticOrchestrator(service))

    async def drain():
        for _ in range(30):
            result = await processor.process_once(limit=20)
            assert result["failed"] == 0
            if not result["claimed"]:
                break

    asyncio.run(drain())
    node = next(n for n in service.repository.queries.list_workflow_snapshots(workflow_id)
                if n.aggregate_type == AggregateType.DAG_NODE_RUN)
    assert node.state == "WAITING_PRODUCT"
    assert service.repository.cycles.read_graph_execution(workflow_id=workflow_id).cycles["repository"].active_assignment is None
    canonical = service.runtime_root / "data" / "bunshin" / "general" / "research"
    clone = tmp_path / "external"
    _git(tmp_path, "clone", str(canonical), str(clone))
    candidate = commit(clone, "note.md", "actual result")
    payload = {"workflow_id": workflow_id, "node_run_id": node.aggregate_id,
               "generation": node.payload["graph_generation"], "submission_id": "external-1",
               "repository": str(clone), "commit_sha": candidate, "result_text": "Note written"}
    submit_external_product(service.repository, service.artifacts, payload)
    submit_external_product(service.repository, service.artifacts, payload)
    asyncio.run(drain())
    workflow = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
    assert workflow.state == "COMPLETED"
    delivery = service.artifacts.read_json(workflow.payload["result_artifact_ref"])
    assert delivery["verification_status"] == "not_required"
    assert delivery["commit_sha"] == candidate
    assert (canonical / "note.md").read_text() == "actual result"
    handoff, = BackgroundInbox(service.runtime_root).pending()
    assert handoff["result_text"] == "Note written"
    assert handoff["delivery_status"] == "not_requested"
    assert workflow.payload["channel_delivery_requested"] is False
    terminals = [event for event in events if event.get("status") == "completed"]
    assert terminals
    assert all(event["channel_delivery_requested"] is False and "attachments" not in event for event in terminals)
    assert service.workflow_status(workflow_id)["result_recipient"] == "resident"
    manifest = service.artifacts.read_json(node.payload["architecture_manifest_ref"])
    assert manifest["result_recipient"] == "resident"
    from pal.bunshin.contract_runtime import ContractArtifactAccess
    from pal.bunshin.work_views import UnitWorkViewBuilder
    view = UnitWorkViewBuilder(ContractArtifactAccess(service.artifacts, service.repository)).build(node)
    assert service.artifacts.read_json(view)["result_recipient"] == "resident"
    cycle = service.repository.cycles.read_graph_execution(workflow_id=workflow_id).cycles["repository"]
    assert cycle.last_verdict is None


def test_scheduled_occurrence_and_cursor_are_one_durable_admission(tmp_path):
    from datetime import datetime, timezone, timedelta
    from pal.foundation import PalV2Database
    from pal.proactive.models import ProactiveDefinitionModel, ProactiveRunModel
    from pal.proactive.repository import ProactiveRepository
    from pal.proactive.runtime import ProactiveManager
    from pal.proactive.contracts import ProactiveDefinition

    db = PalV2Database(tmp_path / "pal.sqlite3")
    db.initialize([ProactiveDefinitionModel, ProactiveRunModel])
    try:
        repository = ProactiveRepository()
        manager = ProactiveManager(repository=repository)
        reference = datetime.now(timezone.utc)
        definition = ProactiveDefinition("daily", "write a note", schedule={"cadence": "once",
            "run_at_utc": (reference - timedelta(seconds=2)).isoformat()})
        due = (reference - timedelta(seconds=2)).isoformat()
        manager.hydrate(definition, next_due_at_utc=due, persist=True)
        manager.enqueue_due_triggers(now_utc=reference)
        runs = repository.pending_background_runs()
        assert len(runs) == 1
        assert repository.list_definitions()[0].next_due_at_utc is None
        restarted = ProactiveManager(repository=repository)
        stored = repository.list_definitions()[0]
        restarted.hydrate(stored.definition, next_due_at_utc=stored.next_due_at_utc)
        restarted.enqueue_due_triggers(now_utc=reference)
        assert [run.proactive_run_id for run in repository.pending_background_runs()] == [runs[0].proactive_run_id]
        repository.queue_background_run(manager.pending_triggers[0], definition, next_due=None)
        assert len(repository.pending_background_runs()) == 1
    finally:
        db.close()


def test_inbox_explicit_consumption_can_precede_publication(tmp_path):
    inbox = BackgroundInbox(tmp_path)
    inbox.acknowledge(["wf"])
    inbox.publish("wf", {"result_text": "actual result"}, delivery_status="pending")
    assert inbox.pending() == []
    assert inbox.begin_delivery("wf")
    assert not inbox.begin_delivery("wf")
    inbox.update_delivery("wf", "unknown")
    assert not inbox.begin_delivery("wf")


def test_external_plan_waits_without_worker(tmp_path):
    service = BunshinWorkflowService(tmp_path / "runtime")
    response = service.start_workflow({"title": "external plan", "profile": "general.generic",
        "workspace": {"kind": "new_project", "repo_path": str(tmp_path / "source")},
        "goal": "Plan an ordinary document", "execution_mode": "planned",
        "role_overrides": {"architect": None, "reviewer": None, "implementation": None, "verifier": None},
        "task_spec": {"authoritative_text": "Plan a document."},
        "delivery_binding": {"channel_id": "null", "channel_kind": "null", "reply_target": {"sink": "null"}}})
    processor = BunshinOutboxProcessor(service, semantic_effects=SemanticOrchestrator(service))

    async def drain():
        for _ in range(10):
            result = await processor.process_once(limit=20)
            assert result["failed"] == 0
            if not result["claimed"]:
                break
    asyncio.run(drain())
    cycle = service.repository.cycles.read_plan_cycle(workflow_id=response["workflow_id"])
    assert cycle.state.value == "WAITING_PRODUCT"
    assert cycle.active_assignment is None
    revision = next(n for n in service.repository.queries.list_workflow_snapshots(response["workflow_id"])
                    if n.aggregate_type == AggregateType.ARCHITECTURE_REVISION)
    assert revision.state == "WAITING_PRODUCT"
    from pal.bunshin.architecture_compilation import ArchitectureTemplateCompiler
    from pal.bunshin.external_plan import submit_external_plan
    contract = ArchitectureTemplateCompiler().compile("general.v1").example
    supplied = service.artifacts.put_json({"contract_schema": "general.v1", "contract": contract,
        "requirements_ref": revision.payload["requirements_ref"]}, artifact_type="ContractArtifact")
    payload = {"workflow_id": response["workflow_id"], "node_run_id": revision.aggregate_id,
               "generation": cycle.generation, "submission_id": "plan-1", "artifact_ref": supplied.to_dict()}
    submit_external_plan(service, payload)
    submit_external_plan(service, payload)
    asyncio.run(drain())
    cycle = service.repository.cycles.read_plan_cycle(workflow_id=response["workflow_id"])
    assert cycle.state.value == "HUMAN_REVIEW"
    assert cycle.last_verdict is None
    assert cycle.product_ref != supplied.sha256  # Manager compiles the actual graph.
    service.submit_human_decision({"workflow_id": response["workflow_id"], "actor": "pal",
                                   "decision": "accept", "source_channel": "null"})
    asyncio.run(drain())
    node = next(n for n in service.repository.queries.list_workflow_snapshots(response["workflow_id"])
                if n.aggregate_type == AggregateType.DAG_NODE_RUN)
    assert node.state == "WAITING_PRODUCT"
    external = tmp_path / "external-document"
    external.mkdir()
    (external / "report.md").write_text("Actual external report")
    submission = {"workflow_id": response["workflow_id"], "node_run_id": node.aggregate_id,
                  "generation": node.payload["graph_generation"], "submission_id": "external-report",
                  "repository": str(external)}
    submit_external_product(service.repository, service.artifacts, submission)
    asyncio.run(drain())
    workflow = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, response["workflow_id"])
    assert workflow.state == "COMPLETED"


def test_background_handoff_checkpoint_ack_loss_does_not_duplicate(tmp_path):
    from types import SimpleNamespace
    from pal.core.turn_executor import TurnExecutor
    from pal.memory.service import MemoryService
    from pal.memory.runtime_state import MemoryRuntimeStatePort

    inbox = BackgroundInbox(tmp_path)
    inbox.publish("wf", {"result_text": "background result"}, delivery_status="delivered")
    memory = MemoryService()
    memory.begin_l1_turn("user", user_text="Hello")
    prefix = memory.active_l1_turn("user").messages
    saved = {}

    class CrashAfterCheckpoint:
        async def checkpoint_async(self, reason):
            saved.update(MemoryRuntimeStatePort(memory).snapshot_state())
            raise RuntimeError("crash after checkpoint before inbox acknowledgement")

    executor = SimpleNamespace(
        context=SimpleNamespace(execution_runtime=SimpleNamespace(runtime_root=tmp_path),
                                port_registry=SimpleNamespace(get=lambda _: CrashAfterCheckpoint())),
        memory=SimpleNamespace(current=memory),
    )
    continuation = SimpleNamespace(turn_id="user", llm_round_index=0)
    assembly = SimpleNamespace(turn_kind="chat", core_mode="default")
    with pytest.raises(RuntimeError, match="crash"):
        asyncio.run(TurnExecutor._append_background_handoffs(executor, continuation, assembly))
    assert memory.active_l1_turn("user").messages[:len(prefix)] == prefix
    assert inbox.pending()
    restored = MemoryService()
    port = MemoryRuntimeStatePort(restored)
    port.install_prepared_state(port.prepare_restore_state(saved))
    count = len(restored.active_l1_turn("user").messages)

    class Saved:
        async def checkpoint_async(self, reason):
            return True

    executor.memory.current = restored
    executor.context.port_registry.get = lambda _: Saved()
    asyncio.run(TurnExecutor._append_background_handoffs(executor, continuation, assembly))
    assert len(restored.active_l1_turn("user").messages) == count
    assert inbox.pending() == []


@pytest.mark.parametrize("crash_at", ["before_advance", "after_advance"])
def test_git_promotion_recovers_exact_target_after_interruption(tmp_path, monkeypatch, crash_at):
    import pal.bunshin.managed_workspace as managed
    repo = tmp_path / "canonical"
    base = capture_managed_head(tmp_path, repo)
    clone = tmp_path / "worker"
    _git(tmp_path, "clone", str(repo), str(clone))
    candidate = commit(clone, "result.md", "result")
    original_reconcile = managed._reconcile
    original_write = managed.atomic_write_json

    def fail_reconcile(path):
        if crash_at == "before_advance" and managed._marker(path).exists():
            raise RuntimeError("injected crash")
        return original_reconcile(path)

    def fail_write(path, payload):
        if crash_at == "after_advance" and path.name.startswith("pal-delivery-"):
            raise RuntimeError("injected crash")
        return original_write(path, payload)

    monkeypatch.setattr(managed, "_reconcile", fail_reconcile)
    monkeypatch.setattr(managed, "atomic_write_json", fail_write)
    with pytest.raises(RuntimeError, match="injected crash"):
        promote_managed_candidate(tmp_path, repo, clone, base, candidate, "workflow")
    assert managed._marker(repo).exists()
    monkeypatch.setattr(managed, "_reconcile", original_reconcile)
    monkeypatch.setattr(managed, "atomic_write_json", original_write)
    assert promote_managed_candidate(tmp_path, repo, clone, base, candidate, "workflow") == candidate
    assert (repo / "result.md").read_text() == "result"
    assert not managed._marker(repo).exists()


def test_scheduler_dispatch_and_resume_do_not_require_resident_turn(tmp_path):
    from datetime import datetime, timezone, timedelta
    from types import SimpleNamespace
    from pal.foundation import PalV2Database
    from pal.proactive.models import ProactiveDefinitionModel, ProactiveRunModel
    from pal.proactive.repository import ProactiveRepository
    from pal.proactive.runtime import ProactiveManager
    from pal.proactive.contracts import ProactiveDefinition
    from pal.proactive.background_dispatch import BackgroundScheduleHandler, BackgroundScheduleSource
    from pal.shared.background_protocol import BACKGROUND_WORKFLOWS
    from pal.control.runtime_contracts import CORE_CONTROL

    db = PalV2Database(tmp_path / "pal.sqlite3")
    db.initialize([ProactiveDefinitionModel, ProactiveRunModel])
    try:
        repo = ProactiveRepository()
        manager = ProactiveManager(repository=repo)
        due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        manager.hydrate(ProactiveDefinition("daily", "a note", schedule={"cadence": "once", "run_at_utc": due}),
                        next_due_at_utc=due, persist=True)
        calls = []

        class Backend:
            async def start_scheduled_run(self, request):
                calls.append(request)
                return {"workflow_id": "wf"}

            async def scheduled_run_status(self, workflow_id):
                return {"workflow": {"workflow_state": "COMPLETED", "delivery": {"result_text": "done"}}}

        backend = Backend()
        busy = SimpleNamespace(snapshot=lambda: SimpleNamespace(memory_maintenance=False),
                               can_start_background_turn=lambda: False)
        context = SimpleNamespace(port_registry=SimpleNamespace(
            optional=lambda key: busy if key == CORE_CONTROL else backend if key == BACKGROUND_WORKFLOWS else None,
            get=lambda _: None,
        ))
        source = BackgroundScheduleSource(manager, SimpleNamespace())
        assert source.prepare(context)
        event, = source.drain(context)
        asyncio.run(BackgroundScheduleHandler(repo).handle(event, context))
        assert len(calls) == 1
        run = repo.pending_background_runs()[0]
        assert run.status == "dispatched"
        asyncio.run(BackgroundScheduleHandler(repo).handle(event, context))
        assert len(calls) == 1
        assert not repo.pending_background_runs()
        assert repo.latest_run("daily").output_summary == "done"
    finally:
        db.close()


def test_scheduled_archive_snapshot_uses_complete_records(tmp_path):
    from pal.bunshin.scheduled_inputs import capture_conversation_archive
    source = tmp_path / "data" / "conversation-history"
    source.mkdir(parents=True)
    (source / "today.jsonl").write_bytes(b'{"record":1}\n{"unfinished"')
    target = Path(capture_conversation_archive(tmp_path, "wf"))
    assert (target / "today.jsonl").read_bytes() == b'{"record":1}\n'
    (source / "today.jsonl").write_bytes(b'{"record":1}\n{"record":2}\n')
    assert Path(capture_conversation_archive(tmp_path, "wf")) == target
    assert (target / "today.jsonl").read_bytes() == b'{"record":1}\n'


@pytest.mark.parametrize("mode", ["direct", "planned"])
def test_external_wait_pause_resume_cancel_never_admits_a_worker(tmp_path, mode):
    service = BunshinWorkflowService(tmp_path / "runtime")
    overrides = {"implementation": None} if mode == "direct" else {"architect": None}
    response = service.start_workflow({"title": "external", "profile": "general.generic",
        "workspace": {"kind": "new_project", "repo_path": str(tmp_path / "source")},
        "goal": "External task", "execution_mode": mode, "role_overrides": overrides,
        "task_spec": {"authoritative_text": "External task"},
        "delivery_binding": {"channel_id": "null", "channel_kind": "null", "reply_target": {"sink": "null"}}})
    processor = BunshinOutboxProcessor(service, publish_workflow_event=lambda _: None, semantic_effects=SemanticOrchestrator(service))
    workflow_id = response["workflow_id"]
    aggregate_type = AggregateType.DAG_NODE_RUN if mode == "direct" else AggregateType.ARCHITECTURE_REVISION

    async def drain():
        for _ in range(20):
            result = await processor.process_once(limit=20)
            assert result["failed"] == 0
            if not result["claimed"]:
                break

    def child_state():
        return next(n.state for n in service.repository.queries.list_workflow_snapshots(workflow_id)
                    if n.aggregate_type == aggregate_type)

    asyncio.run(drain())
    assert child_state() == "WAITING_PRODUCT"
    service.control_workflow(workflow_id=workflow_id, command="pause", actor="pal", source_channel="test")
    asyncio.run(drain())
    assert child_state() == "PAUSED"
    service.resume_workflow(workflow_id=workflow_id, actor="pal", source_channel="test")
    asyncio.run(drain())
    assert child_state() == "WAITING_PRODUCT"
    service.control_workflow(workflow_id=workflow_id, command="cancel", actor="pal", source_channel="test")
    asyncio.run(drain())
    assert service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id).state == "CANCELLED"
    if mode == "direct":
        from pal.bunshin.workflow_capabilities import BunshinPublicProvider
        from pal.execution.contracts import CapabilityCall
        inbox = BackgroundInbox(service.runtime_root)
        assert inbox.pending()[0]["workflow_id"] == workflow_id
        result = asyncio.run(BunshinPublicProvider(service.runtime_root).read_task_result(
            CapabilityCall(name="read_bunshin_task_result", args={"task": "external"})))
        assert result.status == "ok"
        assert result.structured["state"] == "CANCELLED"
        assert result.context_messages[0].metadata["background_workflow_id"] == workflow_id
        # Only a persisted Resident L1 acceptance consumes the receipt.
        from types import SimpleNamespace
        from pal.shared.tool_protocol import ToolExecutionResult, new_tool_call
        from tests.test_resident_checkpoint import _build_app
        app = _build_app(service.runtime_root)
        app.handle.memory_service.begin_l1_turn("read", user_text="What happened?")
        async def accept_result():
            executor = app.handle.core.turn_executor
            await executor._append_l1_tool_context_messages_async(
                SimpleNamespace(turn_id="read"), (new_tool_call(name="read_bunshin_task_result", args={}),),
                (ToolExecutionResult(name="read_bunshin_task_result", ok=True, llm_text="Cancelled",
                                     context_messages=tuple(result.context_messages)),))
            assert inbox.pending()
            await executor._checkpoint_resident_async("read", "round")
        asyncio.run(accept_result())
        assert not inbox.pending()
        app.handle.core.context.execution_runtime.shutdown()


def test_schedule_storage_failure_preserves_cursor_and_retries(tmp_path, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from types import SimpleNamespace
    from unittest.mock import Mock
    from pal.foundation import PalV2Database
    from pal.proactive.models import ProactiveDefinitionModel, ProactiveRunModel
    from pal.proactive.repository import ProactiveRepository
    from pal.proactive.runtime import ProactiveManager
    from pal.proactive.contracts import ProactiveDefinition
    from pal.proactive.background_dispatch import BackgroundScheduleSource

    db = PalV2Database(tmp_path / "pal.sqlite3")
    db.initialize([ProactiveDefinitionModel, ProactiveRunModel])
    try:
        repo = ProactiveRepository()
        manager = ProactiveManager(repository=repo)
        due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        manager.hydrate(ProactiveDefinition("daily", "a note", schedule={"cadence": "once", "run_at_utc": due}),
                        next_due_at_utc=due, persist=True)
        real_admission = repo.queue_background_run
        monkeypatch.setattr(repo, "queue_background_run", Mock(side_effect=OSError("storage unavailable")))
        context = SimpleNamespace(port_registry=SimpleNamespace(optional=lambda _: None))
        timers = Mock()
        source = BackgroundScheduleSource(manager, timers)
        assert not source.prepare(context)
        timers.schedule.assert_called_once()
        assert manager.schedule_engine.next_due_at("daily") == due
        assert repo.list_definitions()[0].next_due_at_utc == due
        assert not repo.pending_background_runs()
        monkeypatch.setattr(repo, "queue_background_run", real_admission)
        assert source.prepare(context)
        assert len(repo.pending_background_runs()) == 1
        assert repo.list_definitions()[0].next_due_at_utc is None
    finally:
        db.close()


@pytest.mark.parametrize("scheduled,channel", [(True, True), (False, True), (False, False), (False, None)])
def test_channel_publication_separates_scheduled_message_from_internal_files(tmp_path, monkeypatch, scheduled, channel):
    import hashlib
    from types import SimpleNamespace
    from pal.bunshin.workflow_publication import WorkflowPublication

    service = BunshinWorkflowService(tmp_path / "runtime")
    content = b"internal answer key\n"
    file_ref = service.artifacts.put_bytes(content, artifact_type="TaskDeliverableArtifact", media_type="text/markdown")
    receipt = service.artifacts.put_json({
        "schema_version": "5", "kind": "files",
        "verification_ref": {}, "verification_status": "not_required",
        "result_text": "The requested quiz, without answers.",
        "next_steps": ["Grade it when the user replies."],
        "files": [{"repository_path": "answer-key.md", "content_sha256": hashlib.sha256(content).hexdigest(),
                   "artifact_ref": file_ref.to_dict(), "mime_type": "text/markdown"}],
    }, artifact_type="DeliveryReceiptArtifact", schema_version="5")
    payload = {"background_handoff": True, "result_artifact_ref": receipt.to_dict(),
               **({"channel_delivery_requested": channel} if channel is not None else {}),
               **({"scheduled_run_id": "run"} if scheduled else {})}
    workflow = SimpleNamespace(state="COMPLETED", updated_at="2026-11-28T10:16:00Z", payload=payload)
    monkeypatch.setattr(service.repository.snapshots, "read_snapshot", lambda *args: workflow)
    events = []
    WorkflowPublication(service, publish_workflow_event=events.append).publish_terminal("workflow")
    event, = events
    result, = BackgroundInbox(service.runtime_root).pending()
    assert result["result_text"] == "The requested quiz, without answers."
    assert result["next_steps"] == ["Grade it when the user replies."]
    assert result["files"][0]["artifact_ref"] == file_ref.to_dict()
    if scheduled or channel is False:
        assert event["summary"] == result["result_text"]
        assert "attachments" not in event
        assert result["delivery_status"] == ("pending" if channel else "not_requested")
        if channel is False:
            assert event["channel_delivery_requested"] is False
    else:
        assert event["attachments"][0]["file_name"] == "answer-key.md"
        assert "Next steps:" in event["summary"]


def test_scheduled_delivery_contract_is_frozen_into_each_task_ledger(tmp_path):
    from pal.bunshin.scheduled_workflows import start_scheduled_run, SCHEDULED_DELIVERY_CONTRACT
    service = BunshinWorkflowService(tmp_path / "runtime")
    result = start_scheduled_run(service, {
        "run_id": "run", "task_name": "daily-quiz", "goal": "Send today's quiz",
        "method": "Use the private answer key only for grading",
        "delivery_binding": {"channel_id": "telegram", "channel_kind": "telegram", "reply_target": {"chat_id": "1"}},
    })
    wf = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, result["workflow_id"])
    request = service.artifacts.read_json(wf.payload["request_ref"])
    ledger = service.artifacts.read_json(request["requirements_ref"])
    assert ledger["original"]["goal"] == "Send today's quiz"
    assert ledger["original"]["method"] == "Use the private answer key only for grading"
    assert ledger["original"]["delivery_contract"] == SCHEDULED_DELIVERY_CONTRACT


@pytest.mark.parametrize("recipient,expected", [(None, False), ("resident", False), ("channel", True)])
def test_general_direct_result_audience_frozen_per_workflow(tmp_path, recipient, expected):
    service = BunshinWorkflowService(tmp_path / "runtime")
    result = service.start_workflow({
        "title": "research", "profile": "general.generic", "execution_mode": "direct",
        "goal": "Research a topic", "workspace": {"kind": "managed_git"},
        "result_recipient": recipient, "task_spec": {"authoritative_text": "Research a topic"},
        "delivery_binding": {"channel_id": "telegram", "channel_kind": "telegram", "reply_target": {"chat_id": "1"}},
    })
    workflow_id = result["workflow_id"]
    assert result["result_recipient"] == ("channel" if expected else "resident")
    wf = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
    request = service.artifacts.read_json(wf.payload["request_ref"])
    assert wf.payload["channel_delivery_requested"] is expected
    assert request["channel_delivery_requested"] is expected
    assert service.workflow_status(workflow_id)["result_recipient"] == ("channel" if expected else "resident")
    # The binding is still present for approval and clarification regardless of result audience.
    binding = service.repository.delivery_bindings.read_task_delivery(wf.payload["task_id"])
    assert binding["current"]["channel_id"] == "telegram"


@pytest.mark.parametrize("primary_sent", [False, True])
def test_legacy_scheduled_outbox_recovers_text_only_without_replaying_parts(tmp_path, monkeypatch, primary_sent):
    from copy import deepcopy
    from types import SimpleNamespace
    from pal.bunshin.manager import BunshinManager, _delivery_event_from_row
    from pal.bunshin.capabilities import BunshinManagerProvider
    from pal.bunshin.workflow_publication import WorkflowPublication
    from pal.bunshin.source import BunshinControlEventHandler, _event_payload
    from pal.foundation import EventEnvelope
    from pal.shared import EventKind, SourceKind
    from pal.shared.ports import PortRegistry

    manager = BunshinManager(tmp_path / "runtime")
    service = manager.workflow_service
    route = {"channel_id": "telegram", "channel_kind": "telegram", "reply_target": {"chat_id": "1"}}
    service.create_task({"task_id": "__PROACTIVE__", "title": "__PROACTIVE__", "objective": "Quiz",
                         "profile": "general.proactive_worker", "workspace": {"kind": "managed_git"},
                         "actor": "pal", "delivery_binding": route})
    response = service.start_workflow({"task_id": "__PROACTIVE__", "scheduled_run_id": "run",
        "scheduled_task_name": "quiz", "channel_delivery_requested": True, "execution_mode": "direct",
        "goal": "Generate quiz", "task_spec": {"authoritative_text": "Keep answer key private"},
        "role_overrides": {"implementation": None}, "delivery_binding": route, "memory_policy": "disabled"})
    wid = response["workflow_id"]
    published = []
    processor = BunshinOutboxProcessor(service, publish_workflow_event=published.append,
                                       semantic_effects=SemanticOrchestrator(service))

    async def drain():
        for _ in range(30):
            result = await processor.process_once(limit=20)
            assert result["failed"] == 0
            if not result["claimed"]:
                break

    asyncio.run(drain())
    node = next(n for n in service.repository.queries.list_workflow_snapshots(wid)
                if n.aggregate_type == AggregateType.DAG_NODE_RUN)
    clone = tmp_path / "external"
    _git(tmp_path, "clone", str(service.runtime_root / "proactive"), str(clone))
    candidate = commit(clone, "answer-key.md", "private fixture answer")
    submit_external_product(service.repository, service.artifacts, {
        "workflow_id": wid, "node_run_id": node.aggregate_id, "generation": node.payload["graph_generation"],
        "submission_id": "external-1", "repository": str(clone), "commit_sha": candidate,
        "result_text": "Question text only", "next_steps": ["Grade on reply"], "deliverable_paths": ["answer-key.md"]})
    asyncio.run(drain())
    workflow = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, wid)
    assert workflow.state == "COMPLETED"
    canonical = next(e for e in published if e.get("status") == "completed")
    receipt = service.artifacts.read_json(workflow.payload["result_artifact_ref"])
    file = receipt["files"][0]
    file_record = service.repository.artifacts.read_artifact_record(file["artifact_ref"]["sha256"])

    # Persist exactly the old presentation under the same delivery identity.
    old = deepcopy(canonical)
    old["summary"] += "\n\nNext steps:\nGrade on reply"
    old["attachments"] = [{"path": file_record["storage_path"], "file_name": "answer-key.md",
                           "mime_type": "text/markdown", "caption": "Task deliverable: answer-key.md"}]
    manager._publish_workflow_event(old)
    old_row, = service.repository.deliveries.list_pending_task_deliveries()
    did = old_row["delivery_id"]
    deliveries = service.repository.deliveries
    assert deliveries.acknowledge_task_delivery_part(did, "attachment:0")
    if primary_sent:
        assert deliveries.acknowledge_task_delivery_part(did, "primary")
        BackgroundInbox(service.runtime_root).update_delivery(wid, "delivered")

    queued = []
    manager.events.queue_event = queued.append
    WorkflowPublication(service, publish_workflow_event=manager._publish_workflow_event).publish_terminal(wid)
    replay, = queued
    assert replay["delivery_id"] == did
    assert deliveries.delivered_task_delivery_parts(did) == (("attachment:0", "primary") if primary_sent else ("attachment:0",))
    # Different receipt/route identities must still fail deduplication.
    changed = deepcopy(canonical)
    changed["result_artifact_ref"] = {**changed["result_artifact_ref"], "sha256": "0" * 64}
    with pytest.raises(ValueError, match="dedup key collision"):
        manager._publish_workflow_event(changed)

    provider = BunshinManagerProvider(service.runtime_root)
    monkeypatch.setattr(provider, "_live_route_for_binding", lambda _: {
        "endpoint_id": "telegram", "channel_kind": "telegram", "reply_target": {"chat_id": "1"}})

    def request_sync(method, args):
        if method == "list_task_delivery_parts":
            return {"parts": deliveries.delivered_task_delivery_parts(args["delivery_id"])}
        if method == "ack_task_delivery_part":
            return {"acknowledged": deliveries.acknowledge_task_delivery_part(args["delivery_id"], args["part_key"])}
        if method == "ack_task_delivery":
            return {"acknowledged": deliveries.acknowledge_task_delivery(args["delivery_id"])}
        raise AssertionError(method)
    monkeypatch.setattr(provider.client, "request_sync", request_sync)
    prepared = provider._prepare_delivery_event(replay)
    assert prepared["payload"]["summary"] == "Question text only"
    assert "attachments" not in prepared["payload"]
    sent = []
    async def deliver(delivery, *, require_provider):
        sent.append(delivery.delivery_kind)
        return True
    handler = BunshinControlEventHandler(provider, SimpleNamespace(deliver=deliver))
    event = EventEnvelope(event_kind=EventKind.BUNSHIN_TERMINAL, source_kind=SourceKind.BUNSHIN,
                          payload=_event_payload(prepared))
    asyncio.run(handler.handle(event, SimpleNamespace(port_registry=PortRegistry())))
    assert sent == ([] if primary_sent else ["reply"])
    assert not deliveries.list_pending_task_deliveries()
    final = deliveries.latest_task_delivery(task_id="__PROACTIVE__", workflow_id=wid, event_kind="workflow_terminal")
    assert final["delivery_id"] == did and final["status"] == "delivered"
    assert final["payload"] == old_row["payload"]  # historical packet remains intact
    queued.clear()
    WorkflowPublication(service, publish_workflow_event=manager._publish_workflow_event).publish_terminal(wid)
    assert queued == []


def test_legacy_worker_terminal_is_suppressed_not_forged_as_delivery(tmp_path, monkeypatch):
    from pal.bunshin.capabilities import BunshinManagerProvider
    service = BunshinWorkflowService(tmp_path / "runtime")
    response = service.start_workflow({"title": "research", "profile": "general.generic", "execution_mode": "direct",
        "goal": "Research", "workspace": {"kind": "managed_git"}, "task_spec": {"authoritative_text": "Research"},
        "delivery_binding": {"channel_id": "null", "channel_kind": "null", "reply_target": {"sink": "null"}}})
    w = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, response["workflow_id"])
    deliveries = service.repository.deliveries
    row = deliveries.enqueue_task_delivery(task_id=w.payload["task_id"], workflow_id=w.aggregate_id,
        event_kind="terminal", payload={"event_kind": "terminal", "payload": {"summary": "internal evidence"}},
        dedup_key="legacy")
    provider = BunshinManagerProvider(service.runtime_root)
    def sync(method, args):
        assert method == "suppress_task_delivery"
        return {"acknowledged": deliveries.suppress_task_delivery(args["delivery_id"])}
    monkeypatch.setattr(provider.client, "request_sync", sync)
    provider.settle_event({"event_kind": "terminal", "delivery_id": row["delivery_id"]}, accepted=True)
    latest = deliveries.latest_task_delivery(task_id=w.payload["task_id"], workflow_id=w.aggregate_id, event_kind="terminal")
    assert latest["status"] == "suppressed"
    assert deliveries.delivered_task_delivery_parts(row["delivery_id"]) == ()
    assert not deliveries.list_pending_task_deliveries()
