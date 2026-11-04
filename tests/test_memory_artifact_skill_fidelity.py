"""Provider diagnostics and partial writes must survive model-facing delivery."""
from __future__ import annotations

import asyncio
import json
import builtins
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

import pytest

from pal.artifact import (ArtifactManager, ArtifactRepository, ArtifactRecordModel,
                          ArtifactRepresentationModel, ArtifactHotStateModel)
from pal.artifact.processors import normalize_image_file, prepare_inline_image_file
from pal.artifact.tools import ArtifactImportTool
from pal.artifact.tools import (ArtifactListTool, ArtifactInfoTool, ArtifactReadTool,
                                ArtifactSearchTool, ArtifactSelectTool, ArtifactContentSearchTool)
from pal.execution.contracts import CapabilityResult
from pal.execution.capability_compiler import _attach_effect_receipt
from pal.execution.tool_facade import EmptyToolInput, StructuredToolOutput
from pal.execution.tool_semantics import DIRECT_LOCAL_WRITE
from pal.foundation import PalV2Database
from pal.llm import generation_result_from_values
from pal.memory.contracts import L3RecallResult, L3MutationResult, L3BatchCommitResult, L3CommitRequest, MemoryQuery
from pal.memory.rendering import (build_recall_structured_payload, render_recall_result_for_llm,
                                  build_mutation_structured_payload, render_mutation_result_for_llm)
from pal.skill.models import SkillModel
from pal.skill.service import SkillService
from pal.skill.tools import SkillAssimilateTool, SkillCommitTool, SkillUpdateTool
from pal.shared.result_rendering import render_titled_structured_for_llm
from tests.test_memory_review import setup_review, stage, decision, ROUTE
from tests.test_tool_failure_affordances import runtime, invoke, metadata
from tests.capability_fixture import mount_test_capability


def fail_with_cause(*args, **kwargs):
    try:
        raise OSError("backend detail: storage unavailable")
    except OSError as exc:
        raise ValueError("operation failed") from exc


@pytest.mark.parametrize('status', ['ok', 'error'])
def test_memory_mutation_and_batch_errors_are_short_with_receipts_intact(status):
    from dataclasses import asdict
    from pal.memory.rendering import render_batch_result_for_llm

    report = 'Traceback (most recent call last):\n  File "private.py", line 7\nOSError: storage offline'
    metadata = {'error': report, 'changed': True, 'replayed': False}
    result = L3MutationResult(status=status, document_id='fact:one', metadata=metadata)
    text = render_mutation_result_for_llm('write', result)
    batch = render_batch_result_for_llm({'status': status, 'provider_result': {
        'status': status, 'results': [asdict(result)], 'metadata': metadata}})
    for value in (text, batch):
        assert 'OSError: storage offline' in value
        assert 'changed: True' in value
        assert 'fact:one' in value
        assert 'Traceback' not in value and 'private.py' not in value
    assert build_mutation_structured_payload(result)['metadata']['error'] == report


def test_memory_health_retains_root_after_long_frames():
    from pal.memory.capabilities import MemoryIntrospectionProvider
    from pal.shared import IntrospectionCall

    report = 'Traceback (most recent call last):\n' + '  File "private.py", line 7\n    save()\n' * 30 + 'OSError: storage offline'
    backend = SimpleNamespace(last_embedding_error=report, inspect_summary=lambda: {'last_embedding_error': report})
    registry = SimpleNamespace(plugins={'test': backend}, get=lambda _: backend)
    provider = MemoryIntrospectionProvider(
        service=SimpleNamespace(l3_selector=SimpleNamespace(active_provider_id='test')),
        context=SimpleNamespace(execution_runtime=SimpleNamespace(l3_plugin_registry=registry)))
    for action in (provider.active_provider, provider.list_providers):
        result = action(IntrospectionCall(name='audit'))
        assert 'OSError: storage offline' in result.llm_text
        assert 'Traceback' not in result.llm_text and 'private.py' not in result.llm_text


def test_memory_review_error_card_keeps_roots_and_private_provider_result(setup_review, monkeypatch):
    from pal.memory.rendering import render_batch_result_for_llm

    reviews, provider, _ = setup_review
    state = decision(reviews, stage(reviews), 'accept', candidate_id='c1')
    state = decision(reviews, state, 'submit')
    report = 'Traceback (most recent call last):\n  File "private.py", line 7\nOSError: storage offline'
    result = L3BatchCommitResult(status='error', metadata={'error': report})
    monkeypatch.setattr(provider, 'commit_batch', lambda _: result)
    output = reviews.commit(state['batch_id'])
    saved = reviews.get(state['batch_id'], ROUTE)
    assert saved['result']['provider_result']['metadata']['error'] == report
    for legacy in (False, True):
        if legacy:
            saved['error'] = 'Old diagnostic dump: ' + json.dumps(saved['result'])
        card = reviews.delivery(saved, ROUTE).interaction
        assert 'OSError: storage offline' in card.text
        assert 'Traceback' not in card.text and 'private.py' not in card.text
        assert 'before retrying' in card.text
    assert 'Traceback' not in render_batch_result_for_llm(output)


def test_memory_submission_control_delivers_the_owner_cause(setup_review, monkeypatch):
    from pal.control.contracts import ControlAction
    from pal.memory.capabilities import MemoryIntrospectionProvider

    reviews, backend, _ = setup_review
    state = decision(reviews, stage(reviews), 'accept', candidate_id='c1')
    monkeypatch.setattr(backend, 'commit_batch', lambda _: L3BatchCommitResult(
        status='error', metadata={'error': 'OSError: storage offline'}))
    async def execute(call):
        return SimpleNamespace(status=reviews.commit(call.args['batch_id'])['status'])
    provider = MemoryIntrospectionProvider(service=reviews.memory,
        context=SimpleNamespace(execution_runtime=SimpleNamespace(execute_async=execute)))
    result = asyncio.run(provider.handle_memory_candidate_decision_async(ControlAction(
        'memory_candidate_decision', 'memory', target_id=state['batch_id'], route=ROUTE,
        args={'decision': 'submit', 'revision': state['revision']})))
    assert 'OSError: storage offline' in result['delivery'].text
    assert 'before retrying' in result['delivery'].text


@pytest.fixture
def services(tmp_path):
    db = PalV2Database(tmp_path / "services.sqlite3")
    db.initialize([SkillModel, ArtifactRecordModel, ArtifactRepresentationModel, ArtifactHotStateModel])
    yield (SkillService(runtime_root=tmp_path),
           ArtifactManager(runtime_root=tmp_path, repository=ArtifactRepository()))
    db.close()


def deliver(runtime, handler):
    mount_test_capability(runtime, alias="audit_result", canonical_path="op_audit_result",
        InputModel=EmptyToolInput, OutputModel=StructuredToolOutput, execution=DIRECT_LOCAL_WRITE,
        handler=lambda _: _attach_effect_receipt(handler(), DIRECT_LOCAL_WRITE))
    return invoke(runtime, "audit_result", {})


def artifact_call(tool, args):
    host = SimpleNamespace(turn_io=SimpleNamespace(artifact_scope_for_turn=lambda _: "audit"))
    return asyncio.run(tool.ainvoke(args, runtime=host, turn_id="review"))


def test_recall_retains_provider_diagnostics_even_without_degraded_flag(runtime):
    result = L3RecallResult(metadata={"backend_error": "vector shard unavailable", "attempt": 2})
    query = MemoryQuery(queries=["preferences"])
    args = dict(provider_id="test", query=query, result=result, view="summary")
    raw = CapabilityResult(status="ok", text="Recall", structured=build_recall_structured_payload(**args),
                           llm_text=render_recall_result_for_llm(**args))
    final = deliver(runtime, lambda: raw)
    assert "Recall incomplete:" in final.llm_text
    assert "vector shard unavailable" not in final.llm_text
    assert raw.structured["metadata"] == result.metadata
    assert "No matching memories found." not in final.llm_text


def test_successful_memory_mutation_retains_provider_warnings(runtime):
    result = L3MutationResult(status="ok", document_id="fact:one",
                              metadata={"warning": "index refresh failed; durable write succeeded"})
    final = deliver(runtime, lambda: CapabilityResult(status="ok", text="Written",
        structured=build_mutation_structured_payload(result), llm_text=render_mutation_result_for_llm("write", result)))
    assert final.ok
    assert "index refresh failed; durable write succeeded" in final.llm_text


@pytest.mark.parametrize("status", ["error", "ok"])
def test_batch_review_preserves_provider_and_item_results(setup_review, monkeypatch, runtime, status):
    reviews, provider, _ = setup_review
    state = decision(reviews, stage(reviews), "accept", candidate_id="c1")
    state = decision(reviews, state, "submit")
    result = L3BatchCommitResult(status=status,
        results=(L3MutationResult(status=status, document_id="fact:one", metadata={"diagnostic": "item receipt detail"}),),
        metadata={"reason": "provider diagnostic", "effect": "unknown" if status == "error" else "applied"})
    monkeypatch.setattr(provider, "commit_batch", lambda _: result)
    output = reviews.commit(state["batch_id"])
    final = deliver(runtime, lambda: CapabilityResult(status=output["status"], text="Batch result", structured=output,
        llm_text=render_titled_structured_for_llm("Batch result", output)))
    assert "provider diagnostic" in final.llm_text
    assert "item receipt detail" in final.llm_text
    saved = reviews.get(state["batch_id"], ROUTE)
    assert saved["result"]["provider_result"]["metadata"] == result.metadata
    if status == "error":
        assert "not committed" not in saved["error"]
        assert "provider diagnostic" in saved["error"]


@pytest.mark.parametrize("finish", ["error", "content_filter", "length"])
def test_skill_sanitizer_failure_keeps_provider_response(services, runtime, finish):
    service, _ = services
    class LLM:
        async def agenerate(self, request):
            return generation_result_from_values(text="provider detail: unavailable", finish_reason=finish)
    service.llm_runtime = LLM()
    final = deliver(runtime, lambda: asyncio.run(SkillAssimilateTool(service).ainvoke({"source_text": "Do work"})))
    assert not final.ok
    assert "provider detail: unavailable" in final.llm_text
    assert finish in final.llm_text


def test_skill_invalid_sanitizer_json_keeps_cause_and_response(services, runtime, caplog):
    service, _ = services
    class LLM:
        async def agenerate(self, request):
            return generation_result_from_values(text="not JSON: provider payload", finish_reason="stop")
    service.llm_runtime = LLM()
    final = deliver(runtime, lambda: asyncio.run(SkillAssimilateTool(service).ainvoke({"source_text": "Do work"})))
    assert "JSONDecodeError" in final.llm_text
    assert "not JSON: provider payload" in caplog.text
    assert "Traceback" not in final.llm_text


def test_skill_unusable_sanitizer_payload_keeps_provider_details(services, runtime, caplog):
    service, _ = services
    class LLM:
        async def agenerate(self, request):
            return generation_result_from_values(text='{"error": "provider diagnostic for invalid candidate"}')
    service.llm_runtime = LLM()
    final = deliver(runtime, lambda: asyncio.run(SkillAssimilateTool(service).ainvoke({"source_text": "Do work"})))
    assert not final.ok
    assert "provider diagnostic for invalid candidate" in caplog.text
    assert "manual_text is required" in final.llm_text


@pytest.mark.parametrize("operation", ["commit", "update"])
def test_skill_write_failure_is_not_input_rejection_and_keeps_cause(services, runtime, monkeypatch, operation):
    service, _ = services
    candidate = asyncio.run(service.assimilate_async({"source_text": "Do work", "desired_skill_id": "audit.test"}))
    if operation == "update":
        service.commit_candidate({"candidate_id": candidate.candidate_id})
        handler = lambda: SkillUpdateTool(service).invoke({"skill_id": "audit.test", "patch": {"summary": "Changed"}})
    else:
        handler = lambda: SkillCommitTool(service).invoke({"candidate_id": candidate.candidate_id})
    monkeypatch.setattr(service.repository, "upsert_skill", fail_with_cause)
    final = deliver(runtime, handler)
    assert not final.ok
    assert "backend detail: storage unavailable" in final.llm_text
    assert metadata(final)["kind"] == "failed"
    assert metadata(final)["effect"] == "unknown"
    assert metadata(final)["retry"] == "reconcile_first"
    assert (service.runtime_root / "SKILL" / "audit.test" / "skill.json").exists()


def test_skill_missing_candidate_rejected_before_write(services, runtime):
    service, _ = services
    final = deliver(runtime, lambda: SkillCommitTool(service).invoke({"candidate_id": "absent"}))
    assert metadata(final)["kind"] == "rejected"
    assert metadata(final)["effect"] == "not_started"


def test_artifact_bad_image_retains_decoder_error_and_no_write(services, tmp_path, runtime):
    _, manager = services
    source = tmp_path / "bad.png"
    source.write_bytes(b"this is not an image")
    final = deliver(runtime, lambda: artifact_call(ArtifactImportTool(manager), {"path": str(source)}))
    assert not final.ok
    assert "UnidentifiedImageError" in final.llm_text
    assert metadata(final)["effect"] == "not_started"
    assert manager.list_hot("audit") == ()


def test_artifact_processor_failure_reaches_import_ref_and_info(services, tmp_path, monkeypatch, runtime):
    _, manager = services
    source = tmp_path / "source.txt"
    source.write_text("Original content")
    monkeypatch.setattr(manager.processor_registry, "processor_for", lambda _: SimpleNamespace(process=fail_with_cause))
    final = deliver(runtime, lambda: artifact_call(ArtifactImportTool(manager), {"path": str(source)}))
    assert not final.ok
    assert "backend detail: storage unavailable" in final.llm_text
    assert metadata(final)["effect"] == "applied"
    ref = manager.list_hot("audit")[0]
    assert "Traceback" in manager.repository.get_record(ref.artifact_id).notes
    assert "Traceback" not in final.llm_text
    assert "backend detail: storage unavailable" in ref.to_dict()["notes"]
    assert "backend detail: storage unavailable" in manager.info(ref.artifact_id, "audit")["artifact"]["notes"]
    exposure = manager.select_prompt_exposure("audit", "review", "", {}, artifact_ids=(ref.artifact_id,))
    assert "backend detail: storage unavailable" in exposure.text
    assert "Traceback" not in exposure.text


def test_existing_transcript_read_failure_preserves_original_result(services, tmp_path, monkeypatch, runtime):
    from pal.artifact.contracts import ArtifactReadResult
    _, manager = services
    original = ArtifactReadResult(ok=False, artifact_id="one", kind="audio", representation="transcript", reason="decoder_unavailable",
                                  metadata={"error": "codec library failed"})
    monkeypatch.setattr(manager, "read", lambda *a, **kw: original)
    final = deliver(runtime, lambda: artifact_call(ArtifactReadTool(manager), {"artifact_id": "one", "representation": "transcript"}))
    assert "decoder_unavailable" in final.llm_text
    assert "codec library failed" in final.llm_text


def test_image_normalization_does_not_claim_jpeg_when_pillow_import_fails(services, tmp_path, monkeypatch):
    _, manager = services
    source = tmp_path / "source.png"
    source.write_bytes(b"unconverted PNG bytes")
    original_import = builtins.__import__
    def importing(name, *args, **kwargs):
        if name == "PIL":
            raise ImportError("Pillow dependency failed")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", importing)
    with pytest.raises(ImportError, match="Pillow dependency failed"):
        normalize_image_file(source, tmp_path / "out.jpg", policy=manager.policy)
    assert not (tmp_path / "out.jpg").exists()


def test_image_preservation_does_not_accept_invalid_image(services, tmp_path):
    _, manager = services
    source = tmp_path / "source.png"
    source.write_bytes(b"unreadable PNG")
    with pytest.raises(OSError):
        prepare_inline_image_file(source, tmp_path / "out.jpg", policy=manager.policy)


@pytest.mark.parametrize("tool,method", [
    (ArtifactListTool, "list_hot"), (ArtifactInfoTool, "describe"), (ArtifactReadTool, "read"),
    (ArtifactSearchTool, "artifact_search"), (ArtifactSelectTool, "select"),
    (ArtifactContentSearchTool, "content_search"),
])
def test_artifact_lookup_wrapper_keeps_backend_cause(services, monkeypatch, runtime, tool, method):
    _, manager = services
    def missing(*args, **kwargs):
        try:
            raise OSError("backend lookup diagnostic")
        except OSError as exc:
            raise KeyError("artifact_not_found") from exc
    monkeypatch.setattr(manager, method, missing)
    final = deliver(runtime, lambda: artifact_call(tool(manager), {"artifact_id": "one", "query": "test"}))
    assert "backend lookup diagnostic" in final.llm_text


@pytest.mark.parametrize("stage_name", ["embed_documents", "embed_query", "health"])
def test_recall_keeps_embedding_error_after_other_stage_succeeds(setup_review, monkeypatch, runtime, stage_name):
    _, provider, _ = setup_review
    provider.commit(L3CommitRequest(kind="fact", title="Preference", summary="Use APIs", search_text="Use APIs"))
    monkeypatch.setattr(provider.embedding_provider, stage_name, fail_with_cause)
    query = MemoryQuery(queries=["APIs"])
    result = provider.recall(query)
    args = dict(provider_id=provider.provider_id, query=query, result=result, view="summary")
    final = deliver(runtime, lambda: CapabilityResult(status="ok",
        structured=build_recall_structured_payload(**args), llm_text=render_recall_result_for_llm(**args)))
    assert "Recall incomplete:" in final.llm_text
    assert "backend detail: storage unavailable" not in final.llm_text
    assert "backend detail: storage unavailable" in json.dumps(final.structured["metadata"])
    assert result.metadata["degraded"] is True


def test_unmounted_memory_provider_reports_unavailability(setup_review, runtime):
    _, provider, _ = setup_review
    provider.mounted = False
    result = provider.recall(MemoryQuery(queries=["APIs"]))
    assert result.metadata["degraded"] is True
    assert "not mounted" in result.metadata["degraded_reason"]


def test_partial_pdf_import_exposes_missing_dependency(services, tmp_path, monkeypatch, runtime):
    _, manager = services
    source = tmp_path / "source.pdf"
    source.write_bytes(b"PDF content")
    original_import = builtins.__import__
    def importing(name, *args, **kwargs):
        if name == "fitz":
            raise ImportError("PDF native library could not be loaded")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", importing)
    final = deliver(runtime, lambda: artifact_call(ArtifactImportTool(manager), {"path": str(source)}))
    assert final.ok
    assert "PDF native library could not be loaded" in final.llm_text
    assert "partial" in metadata(final)["recovery"]


def test_expired_artifact_cleanup_failure_is_visible_and_does_not_claim_deletion(services, tmp_path, monkeypatch, runtime):
    _, manager = services
    source = tmp_path / "source.txt"
    source.write_text("Content")
    ref = manager.register_ingested(source, scope_key="audit", turn_id="review", source_channel="test")
    hot = manager.repository.list_hot_states(scope_key="audit")[0]
    manager.repository.upsert_hot_state(replace(hot, expires_at="2000-01-01T00:00:00+00:00"))
    monkeypatch.setattr(manager, "_remove_managed_artifact_tree", fail_with_cause)
    final = deliver(runtime, lambda: artifact_call(ArtifactInfoTool(manager), {"artifact_id": ref.artifact_id}))
    assert "backend detail: storage unavailable" in final.llm_text
    exposure = manager.select_prompt_exposure("audit", "review", "", {}, artifact_ids=(ref.artifact_id,))
    assert "backend detail: storage unavailable" in exposure.text
    assert "managed bytes and representations deleted" not in exposure.text
    assert "not confirmed" in exposure.text


@pytest.mark.parametrize('collision', [False, True])
def test_import_preserves_exact_trailing_space_filename(services, tmp_path, collision):
    _, manager = services
    source = tmp_path / 'record.txt '
    source.write_text('requested file')
    if collision:
        (tmp_path / 'record.txt').write_text('wrong file')
    result = artifact_call(ArtifactImportTool(manager), {'path': str(source)})
    assert result.status == 'ok', result.llm_text
    # The manager's ingestion must receive the exact resource, not its stripped neighbour.
    assert result.structured['artifact']['file_name'] == 'record.txt '
    record = manager.repository.get_record(result.structured['artifact_id'])
    assert Path(record.original_path).read_text() == 'requested file'


def test_provider_mutation_recovery_keeps_original_provider_after_write(monkeypatch):
    from pal.plugins.l3 import sqlite_vec
    from pal.shared import IntrospectionCall
    committed = []
    provider = SimpleNamespace(provider_id='provider-a', commit=lambda request:
        committed.append(request) or L3MutationResult(status='ok', document_id='case:original'))
    monkeypatch.setattr(sqlite_vec, 'build_mutation_structured_payload', fail_with_cause)
    result = sqlite_vec.SQLiteVecL3Plugin.commit_write(provider, IntrospectionCall(
        name='write_provider_memory', args={'kind': 'fact', 'summary': 'candidate', 'search_text': 'candidate'}))
    assert len(committed) == 1
    assert result.status == 'error'
    recovery = result.affordances[0].arguments
    assert result.affordances[0].tool == 'recall_memory'
    assert recovery['provider_id'] == 'provider-a'
    assert recovery['mem_ref'] == 'case:original'
    assert result.effect_receipt.outcome.value == 'applied'
    assert 'storage unavailable' in result.llm_text
    assert 'provider-a' in result.recovery_hint
