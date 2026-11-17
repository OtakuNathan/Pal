"""Repair success and durable memory success are distinct observable facts."""
import asyncio
from unittest.mock import patch

import pytest

from pal.failure.contracts import FailureSignal
from pal.llm import generation_result_from_values
from pal.memory import MemoryService
from pal.memory.capabilities import register_with_core
from pal.memory.contracts import L2Entry, L3MutationResult
from pal.memory.embedding import HashingEmbedder
from pal.memory.storage import MemoryStorage
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin
from tests.llm_fakes import NonStreamingLLM
from tests.test_failure_flow import _core_with_failure_runtime


class RecoveredLLM(NonStreamingLLM):
    async def respond(self, request):
        return generation_result_from_values(text='{"verification_status":"ok","reason":"Recovered"}')


def repair(core):
    core.context.port_registry["llm:llm"] = RecoveredLLM()
    return asyncio.run(core.handle_failure_async(FailureSignal(
        subsystem="execution", component="fixture", failure_kind="unavailable",
        severity="medium", primary_blocker="Fixture is unavailable"), origin="test"))


@pytest.fixture
def memory_env(tmp_path):
    core = _core_with_failure_runtime()
    memory = MemoryService()
    register_with_core(core.context, memory)
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    provider = SQLiteVecL3Plugin(service=memory, repository=storage.open(), embedder=HashingEmbedder())
    memory.register_l3_provider(provider)
    yield core, memory, provider
    provider.repository.close()
    core.close()


def test_repair_case_uses_memory_commit_and_projects_the_committed_document(memory_env):
    core, memory, provider = memory_env
    result = repair(core)
    saved = result.user_feedback.details["repair_memory"]
    assert saved["status"] == "ok", saved
    document = provider.repository.get_document(saved["document_id"])
    assert document["document_kind"] == "case"
    assert document["canonical_key"] == "repair:execution:fixture:unavailable"
    entry = memory.l2_store.items[saved["document_id"]]
    assert entry.source_ref == saved["document_id"]
    assert result.repair_resolution.situation_text in entry.search_text
    assert len(memory.l2_store.items) == 1


@pytest.mark.parametrize("mode", ["unavailable", "error", "exception"])
def test_failed_repair_save_does_not_publish_a_successful_l2_case(memory_env, mode):
    core, memory, provider = memory_env
    kwargs = ({"side_effect": OSError("storage unavailable")} if mode == "exception" else {
        "return_value": L3MutationResult(status=mode, document_id="",
            projected_entry=L2Entry(entry_id="uncommitted", kind="case", scope="system", title="Uncommitted", summary="Uncommitted"))})
    with patch.object(provider, "commit", **kwargs):
        result = repair(core)
    assert result.verification.status == "ok"
    assert result.user_feedback.details["repair_memory"]["status"] != "ok"
    assert "could not be saved" in result.user_feedback.next_step
    assert not memory.l2_store.items
    assert not provider.repository.list_projection_rows()
    assert core.state.diagnostics[-1]["kind"] == "failure.repair_memory.failed"
