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
    assert BackgroundInbox(service.runtime_root).pending()[0]["result_text"] == "Note written"
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
    processor = BunshinOutboxProcessor(service, semantic_effects=SemanticOrchestrator(service))
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
