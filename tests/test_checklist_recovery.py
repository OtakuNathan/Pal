import asyncio
from copy import deepcopy

import pytest

from pal.checklist import ChecklistService, register_with_core
from pal.checklist.runtime_state import ChecklistRuntimeStatePort
from pal.core.resident_checkpoint import ResidentCheckpointStore
from pal.core.runtime_state import RuntimeSnapshotCoordinator
from tests.test_resident_checkpoint import _build_app


PLAN = [{"step": "Read the current code", "status": "completed"},
        {"step": "Preserve failure evidence /workspace/a.py:42", "status": "in_progress"},
        {"step": "Run the agreed checks", "status": "pending"}]


def app_with_checklist(root):
    app = _build_app(root)
    service = ChecklistService()
    register_with_core(app.handle.core.context, service)
    return app, service


def test_checkpoint_round_trip_and_reset_keep_checklist_independent(tmp_path):
    app, checklist = app_with_checklist(tmp_path)
    checklist.upsert(PLAN)
    asyncio.run(app._publish_checkpoint_async())
    snapshot = ResidentCheckpointStore(tmp_path).read()
    assert snapshot["modules"]["checklist"]["payload"] == {"plan": PLAN}
    assert "Preserve failure evidence" not in ResidentCheckpointStore(tmp_path).path.read_text()
    restored, service = app_with_checklist(tmp_path)
    asyncio.run(restored._restore_checkpoint_async())
    assert restored.last_checkpoint_status == "restored", restored.last_checkpoint_error
    assert list(service.show().plan) == PLAN
    assert service.show().markdown == checklist.show().markdown
    asyncio.run(RuntimeSnapshotCoordinator(restored.handle.core.context.module_registry).reset("user_reset"))
    assert service.show() is None


def test_last_check_does_not_restore_completed_checklist_after_restart(tmp_path):
    app, checklist = app_with_checklist(tmp_path)
    checklist.upsert([{"step": "Finish work"}])
    assert checklist.check("Finish work").cleared
    asyncio.run(app._publish_checkpoint_async())
    restored, service = app_with_checklist(tmp_path)
    asyncio.run(restored._restore_checkpoint_async())
    assert restored.last_checkpoint_status == "restored", restored.last_checkpoint_error
    assert service.show() is None


def test_legacy_snapshot_adds_only_empty_checklist_without_rewriting_history(tmp_path):
    old = _build_app(tmp_path)
    memory = old.handle.memory_service
    from pal.memory.contracts import L1MessageKind, L1TranscriptMessage
    memory.l1_store.append([L1TranscriptMessage(role="assistant", content="ORIGINAL SUMMARY BODY",
        kind=L1MessageKind.RUNTIME_CONTEXT_SUMMARY,
        payload={"schema": "pal.compaction.pal.v2", "summary": {"summary": "old summary", "search_text": "legacy"}})])
    asyncio.run(old._publish_checkpoint_async())
    old_snapshot = ResidentCheckpointStore(tmp_path).read()
    restored, checklist = app_with_checklist(tmp_path)
    asyncio.run(restored._restore_checkpoint_async())
    assert restored.last_checkpoint_status == "restored", restored.last_checkpoint_error
    assert checklist.show() is None
    port = restored.handle.core.context.module_registry.modules["memory"].runtime_state_port
    assert port.snapshot_state()["l1_turns"] == old_snapshot["modules"]["memory"]["payload"]["l1_turns"]
    assert ResidentCheckpointStore(tmp_path).read() is None


@pytest.mark.parametrize("damage", ["bad_plan", "missing_current_module", "bad_hash", "extra_module"])
def test_corrupt_checkpoint_does_not_partially_restore_or_consume(tmp_path, damage):
    source, checklist = app_with_checklist(tmp_path)
    checklist.upsert(PLAN)
    asyncio.run(source._publish_checkpoint_async())
    store = ResidentCheckpointStore(tmp_path)
    snapshot = deepcopy(store.read())
    if damage == "bad_plan": snapshot["modules"]["checklist"]["payload"]["plan"][1]["status"] = "invented"
    elif damage == "missing_current_module": del snapshot["modules"]["checklist"]
    elif damage == "bad_hash": snapshot["runtime_spec_hash"] = "wrong"
    else: snapshot["modules"]["other"] = {"schema_version": "1", "payload": {}}
    store.publish(snapshot)
    restored, service = app_with_checklist(tmp_path)
    service.upsert([{"step": "Existing local state", "status": "pending"}])
    before = service.show()
    asyncio.run(restored._restore_checkpoint_async())
    assert restored.last_checkpoint_status == "restore_failed"
    assert service.show() == before
    assert store.read() is not None


@pytest.mark.parametrize("plan", [[], {}, [{"step": "", "status": "pending"}],
    [{"step": "valid", "status": "pending", "extra": "not discarded"}],
    [{"step": "valid", "status": True}]])
def test_restore_validation_never_changes_live_checklist(plan):
    service = ChecklistService()
    service.upsert(PLAN)
    before = service.show()
    with pytest.raises(ValueError):
        ChecklistRuntimeStatePort(service).prepare_restore_state({"plan": plan})
    assert service.show() == before


def test_compact_leaves_checklist_and_its_current_projection_unchanged(tmp_path):
    from pal.checklist.prompt import ChecklistPromptFragmentProvider
    from pal.llm import generation_result_from_values
    from pal.shared import PromptAssemblyContext
    from tests.test_runtime_compaction import _ScriptedLLM, _valid_pal_payload
    app, checklist = app_with_checklist(tmp_path)
    checklist.upsert(PLAN)
    memory = app.handle.memory_service
    memory.begin_l1_turn("old", user_text="Keep working on this task")
    memory.settle_l1_turn("old")
    before = checklist.show()
    provider = ChecklistPromptFragmentProvider(checklist)
    fragments = provider.build_prompt_fragments(PromptAssemblyContext())
    app.handle.core.context.port_registry["llm:llm"] = _ScriptedLLM([
        generation_result_from_values(text=_valid_pal_payload())])
    result = asyncio.run(app.handle.core.turn_executor.compact_memory_async(
        memory, target_input_budget=8192, reserved_output_tokens=1024))
    assert result.success, result.failures
    assert checklist.show() == before
    assert provider.build_prompt_fragments(PromptAssemblyContext()) == fragments


def test_legacy_all_completed_plan_is_not_reactivated():
    service = ChecklistService()
    port = ChecklistRuntimeStatePort(service)
    prepared = port.prepare_restore_state({"plan": [{"step": "Done", "status": "completed"}]})
    port.install_prepared_state(prepared)
    assert service.show() is None
    assert port.snapshot_state() == {"plan": None}
