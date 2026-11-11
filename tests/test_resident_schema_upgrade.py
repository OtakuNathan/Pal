"""Older readable checkpoints must survive admission, rebase and compaction."""
import asyncio
import hashlib
import json
from unittest.mock import patch

import pytest

from pal.checklist import ChecklistService, register_with_core
from pal.core.compaction import CompactionEngine
from pal.core.pal_compaction import PalCompactionPolicy
from pal.memory.persistence.journal import ResidentJournalStore
from tests.test_resident_history import app, begin, answer
from tests.test_runtime_compaction import _valid_pal_payload
from tests.test_two_segment_executor_flow import _Transport, _ScriptedLLM


def _recorded_hash(snapshot, host="pal:resident"):
    # Independent reproduction of the saved contract, including old schemas.
    payload = {"identity": {"host": host}, "modules": sorted(
        (name, record["schema_version"]) for name, record in snapshot["modules"].items())}
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _app(root, checklist):
    instance = app(root)
    if checklist:
        register_with_core(instance.handle.core.context, ChecklistService())
    return instance


def _executor(instance):
    executor = instance.handle.core.turn_executor
    executor._compaction_engine = CompactionEngine(PalCompactionPolicy(), max_attempts=1)
    transport = _Transport(_ScriptedLLM(_valid_pal_payload("migrated continuity")))
    transport.projection_port = None
    executor.context.port_registry["llm:llm"] = transport
    return executor


@pytest.mark.parametrize("old_schema", ["4", "5"])
@pytest.mark.parametrize("checklist", ["none", "existing", "added"])
def test_readable_checkpoint_and_journal_restore_then_compact(tmp_path, old_schema, checklist):
    async def scenario():
        source = _app(tmp_path, checklist == "existing")
        port = source.handle.core.context.module_registry.modules["memory"].runtime_state_port
        port.schema_version = old_schema
        memory = begin(source, "original before upgrade")
        assert await source.history.checkpoint_async("submission")
        answer(memory, "reply after base checkpoint")
        memory.settle_l1_turn("t")
        assert await source.history.checkpoint_async("round")
        assert source.history.store.path.stat().st_size > 0

        target = _app(tmp_path, checklist != "none")
        await target._restore_checkpoint_async()
        assert target.last_checkpoint_status == "restored", target.last_checkpoint_error
        assert not target.history.inspect()["restore_failed"]
        restored = target.handle.memory_service.history.turns[0]
        assert restored.messages[0].text == "original before upgrade"
        assert restored.messages[-1].text == "reply after base checkpoint"
        assert restored.messages[-1].reasoning_text == "PRIVATE REASONING"
        assert restored.messages[-1].replay.payload["message"]["signature"] == "opaque"
        saved = ResidentJournalStore(tmp_path).read()
        assert saved["modules"]["memory"]["schema_version"] == "5"
        assert saved["runtime_spec_hash"] == target._runtime_spec_hash()

        result = await _executor(target).compact_memory_async(
            target.handle.memory_service, target_input_budget=100000, reserved_output_tokens=4096)
        assert result.success, result.failures
        assert target.history.archives.pending_count() == 0
        again = _app(tmp_path, checklist != "none")
        await again._restore_checkpoint_async()
        assert again.last_checkpoint_status == "restored", again.last_checkpoint_error
        assert "migrated continuity" in repr(again.handle.memory_service.history.turns)
    asyncio.run(scenario())


@pytest.mark.parametrize("fault", [
    "hash", "host", "future_schema", "missing_module", "extra_module", "invalid_payload",
])
def test_incompatible_snapshot_is_preserved_and_compact_explains_recovery_failure(tmp_path, fault):
    async def scenario():
        source = app(tmp_path)
        memory = begin(source, "preserve old recovery point")
        memory.settle_l1_turn("t")
        assert await source.history.checkpoint_async("exit")
        snapshot = source.history.store.read()
        snapshot["modules"]["memory"]["schema_version"] = "4"
        if fault == "future_schema":
            snapshot["modules"]["memory"]["schema_version"] = "999"
        elif fault == "missing_module":
            del snapshot["modules"]["execution"]
        elif fault == "extra_module":
            snapshot["modules"]["unknown"] = {"schema_version": "1", "payload": {}}
        elif fault == "invalid_payload":
            snapshot["modules"]["memory"]["payload"]["history_root"] = "invalid"
        snapshot["runtime_spec_hash"] = _recorded_hash(
            snapshot, host="another-host" if fault == "host" else "pal:resident")
        if fault == "hash":
            snapshot["runtime_spec_hash"] = "unverified"
        source.history.store.base.publish(snapshot)
        before = source.history.store.base.path.read_bytes()

        target = app(tmp_path)
        await target._restore_checkpoint_async()
        assert target.last_checkpoint_status == "restore_failed"
        assert target.history.inspect()["restore_failed"]
        assert target.handle.memory_service.history.turns == ()
        assert target.history.archives.pending_count() == 0
        result = await _executor(target).compact_memory_async(
            target.handle.memory_service, target_input_budget=100000, reserved_output_tokens=4096)
        assert result.status == "commit_failed"
        assert "resident history recovery failed" in result.failures[0]
        assert not await target.history.checkpoint_async("round")
        assert source.history.store.base.path.read_bytes() == before
    asyncio.run(scenario())


def test_failed_upgrade_rebase_keeps_old_checkpoint_and_blocks_compact(tmp_path):
    async def scenario():
        source = app(tmp_path)
        port = source.handle.core.context.module_registry.modules["memory"].runtime_state_port
        port.schema_version = "4"
        memory = begin(source, "keep old checkpoint on failed rebase")
        memory.settle_l1_turn("t")
        assert await source.history.checkpoint_async("exit")
        before = source.history.store.base.path.read_bytes()
        target = app(tmp_path)
        with patch.object(target.history.store, "save", side_effect=OSError("disk unavailable")):
            await target._restore_checkpoint_async()
        assert target.last_checkpoint_status == "restore_failed"
        assert target.history.restore_failed
        assert source.history.store.base.path.read_bytes() == before
        result = await _executor(target).compact_memory_async(
            target.handle.memory_service, target_input_budget=100000, reserved_output_tokens=4096)
        assert result.status == "commit_failed"
        assert "resident history recovery failed" in result.failures[0]
        again = app(tmp_path)
        await again._restore_checkpoint_async()
        assert again.last_checkpoint_status == "restored", again.last_checkpoint_error
        assert again.handle.memory_service.history.turns[0].messages[0].text == "keep old checkpoint on failed rebase"
    asyncio.run(scenario())
