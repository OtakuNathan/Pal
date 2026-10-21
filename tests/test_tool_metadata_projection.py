"""Presentation omits backend metadata while retaining actionable results."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from pal.artifact.contracts import ArtifactReadResult
from pal.artifact.tools import ArtifactReadTool
from pal.execution.tool_presentation import render_tool_search
from pal.lsp.plugin import _capability_from_rpc
from pal.mcp.normalize import normalize_prompt_result, normalize_tool_result
from pal.memory.contracts import L3MutationResult, L3RecallResult, MemoryQuery
from pal.memory.rendering import build_recall_structured_payload, render_batch_result_for_llm, render_mutation_result_for_llm, render_recall_result_for_llm
from pal.skill.tools import _render_skill_tool_payload
from pal.web_search.capabilities import WebSearchIntrospectionProvider
from pal.shared import IntrospectionCall
from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EmptyToolInput, StructuredToolOutput
from pal.execution.tool_semantics import INDIRECT_EXTERNAL_READ
from tests.capability_fixture import mount_test_capability
from tests.test_tool_failure_affordances import invoke, runtime


def visible(result):
    return json.loads(result.llm_text.split("\n", 1)[1])


@pytest.mark.parametrize("view,body", [("summary", "Remember this"), ("origin", "Original text")])
def test_recall_keeps_exact_refs_and_body_without_arbitrary_metadata(view, body):
    result = L3RecallResult(hits=[{"document_id": "case:opaque:abc", "summary": "Remember this",
                                  "search_text": "Original text", "score": 0.8}],
                            metadata={"embedding_health": {"base_url": "internal-url"}, "future_debug": "noise"})
    args = dict(provider_id="storage-provider", query=MemoryQuery(), result=result, view=view)
    text = render_recall_result_for_llm(**args)
    assert f"[case:opaque:abc]: {body}" in text
    assert "provider_id: storage-provider" in text
    assert "noise" not in text and "embedding_health" not in text and "internal-url" not in text
    assert build_recall_structured_payload(**args)["metadata"] == result.metadata


@pytest.mark.parametrize("status", ["found", "deleted", "superseded", "unavailable"])
def test_exact_recall_preserves_lookup_outcome_and_successor_refs(status):
    result = L3RecallResult(metadata={"lookup_status": status, "successors": ["fact:next:opaque"], "debug": "noise"})
    text = render_recall_result_for_llm(provider_id="store", query=MemoryQuery(mem_ref="fact:old:opaque"), result=result, view="summary")
    assert f"mem_ref: fact:old:opaque; lookup_status: {status}" in text
    assert "fact:next:opaque" in text and "noise" not in text


@pytest.mark.parametrize("diagnostic_key", ["reason", "warning", "error", "backend_error"])
def test_mutation_keeps_receipt_and_complete_redacted_diagnostics(diagnostic_key):
    diagnostic = "api_key=secret " + "detail " * 200 + "ROOT_CAUSE: index unavailable"
    result = L3MutationResult(status="ok", document_id="fact:opaque", metadata={
        "no_changes": True, "replayed": True, diagnostic_key: diagnostic,
        "index_refresh": {"internal": "noise"}})
    text = render_mutation_result_for_llm("update", result, provider_id="original-store")
    assert "mem_ref: fact:opaque" in text and "provider_id: original-store" in text
    assert "no_changes: True" in text and "replayed: True" in text
    assert "noise" not in text and "secret" not in text
    # Delivery budgets and snapshots belong to the shared tool presenter.
    assert diagnostic.replace("api_key=secret", "api_key=[redacted]") in text


@pytest.mark.parametrize("status", ["ok", "conflict"])
def test_batch_receipts_keep_refs_replay_and_partial_effects_without_backend_meta(status):
    payload = {"status": status, "references": ["fact:receipt"], "accepted": 1, "skipped": 0,
        "provider_result": {"status": status, "metadata": {"projection_failed": True,
            "reason": "batch changed", "internal": "backend-noise"}, "results": [
                {"status": status, "document_id": "fact:receipt", "hit": {"internal": "backend-hit"},
                 "metadata": {"replayed": True, "internal": "backend-noise"}}]}}
    text = render_batch_result_for_llm(payload)
    assert "fact:receipt" in text and "batch changed" in text and "replayed: True" in text
    assert ("needs refresh" in text) is (status == "ok")
    assert "backend-" not in text


def test_mcp_filters_protocol_meta_without_filtering_business_metadata():
    business = {"_meta": {"customer": "business-value"}, "metadata": {"purpose": "business"}}
    raw = {"_meta": {"debug": "internal-envelope"}, "content": [
        {"type": "text", "text": "Tool body", "_meta": {"debug": "internal-block"}},
        {"type": "resource", "resource": {"uri": "file:///body", "text": "Resource body",
          "_meta": {"debug": "internal-resource"}}}], "structuredContent": business}
    result = normalize_tool_result(raw, server_id="server", tool_name="tool")
    assert visible(result)["result"]["structuredContent"] == business
    assert result.structured["raw_result"] == raw
    assert "internal-" not in result.llm_text
    assert "Resource body" in result.llm_text
    prompt = {"_meta": {"debug": "internal-prompt"}, "messages": [
        {"role": "user", "content": {"type": "text", "text": "Prompt body", "_meta": {"debug": "internal-block"}}}]}
    result = normalize_prompt_result(prompt, server_id="server", prompt_name="prompt")
    assert "Prompt body" in result.llm_text and "internal-" not in result.llm_text
    assert result.structured["raw_result"] == prompt


@pytest.mark.parametrize("status", ["ok", "partial", "error"])
def test_lsp_routine_success_hides_server_details_but_keeps_provenance(status):
    payload = {"status": status, "result": ["location"],
               "evidence": {"server_id": "python", "file_sha256": "digest", "freshness": "fresh"},
               "server": {"pid": 123, "last_error": "backend-detail"}}
    result = _capability_from_rpc("References", payload)
    assert visible(result)["evidence"] == payload["evidence"]
    assert ("server" in visible(result)) is (status != "ok")
    assert all(result.structured[key] == value for key, value in payload.items())


@pytest.mark.parametrize("representation,ok", [("text", True), ("metadata", True), ("text", False)])
def test_artifact_read_keeps_body_paging_and_requested_diagnostics(representation, ok):
    read = ArtifactReadResult(artifact_id="a", ok=ok, kind="text", representation=representation,
        text="Whole body", truncated=True, selection={"page": 2}, text_file={"file_path": "/managed/text"},
        metadata={"file_name": "a.txt", "status": "partial", "notes": "Some pages missing",
                  "metadata": {"backend": "internal-record"}, "representation": {"id": "internal-representation"}})
    tool = ArtifactReadTool(SimpleNamespace(read=lambda *args, **kwargs: read))
    host = SimpleNamespace(turn_io=SimpleNamespace(artifact_scope_for_turn=lambda _: "scope"))
    result = asyncio.run(tool.ainvoke({"artifact_id": "a"}, runtime=host, turn_id="turn"))
    projected = visible(result)
    assert projected["text"] == read.text and projected["selection"] == read.selection
    assert projected["text_file"] == read.text_file and projected["truncated"] is True
    assert projected["metadata"]["notes"] == "Some pages missing"
    assert ("internal-record" in result.llm_text) is (representation == "metadata" or not ok)
    assert all(result.structured[key] == value for key, value in read.to_dict().items())


def test_search_presentation_hides_rank_implementation_and_keeps_contracts():
    payload = {"hits": [{"alias": "read_memory", "score": 1.0}], "suggestions": [
        {"alias": "recall_memory", "score": 0.03, "retrieval_sources": ["fuzzy", "purpose"],
         "contract": {"score": 0.03, "input_contract": {"type": "object"}}}]}
    projected = json.loads(render_tool_search(None, payload))
    assert projected["hits"] == [{"alias": "read_memory"}]
    assert projected["suggestions"][0]["contract"]["input_contract"] == {"type": "object"}
    assert "score" not in str(projected) and "retrieval_sources" not in str(projected)
    skill = {"hits": [{"name": "manual", "score": 1.0, "match_reason": "count=3",
                       "avoid_when_overlap": True, "metadata": {"score": "business-value"}}]}
    projected = json.loads(_render_skill_tool_payload(SimpleNamespace(execution_runtime=None), "Skills", skill).split("\n", 1)[1])
    assert projected["hits"][0] == {"name": "manual", "avoid_when_overlap": True, "metadata": {"score": "business-value"}}


def test_web_success_keeps_all_redacted_provider_diagnostics_and_complete_items():
    error = 'Traceback (most recent call last):\n  File "/backend/private.py", line 1\n    backend()\nRuntimeError: quota exhausted; api_key=secret'
    details = " detail" * 200
    errors = [{"provider_id": f"p{i}", "provider_kind": "kind",
               "error": error + details + f" ROOT_CAUSE_{i}"} for i in range(7)]
    service = SimpleNamespace(query=lambda _: SimpleNamespace(items=[SimpleNamespace(
        title="Title", url="https://example.test", snippet="Complete snippet", provider_id="working", provider_kind="kind", rank=1)],
        configured_provider_id="p0", effective_provider_id="working", fallback_used=True, provider_errors=errors))
    result = WebSearchIntrospectionProvider(service).query(IntrospectionCall(name="search_web", args={"query": "q"}))
    projected = visible(result)
    assert projected["items"] == [{"title": "Title", "url": "https://example.test", "snippet": "Complete snippet"}]
    assert projected["fallback_used"] is True and projected["effective_provider_id"] == "working"
    assert "quota exhausted" in result.llm_text
    assert "Traceback" not in result.llm_text and "/backend/private.py" not in result.llm_text and "secret" not in result.llm_text
    assert projected["provider_errors"] == [
        {"provider_id": f"p{i}",
         "error": "RuntimeError: quota exhausted; api_key=[redacted]" + details + f" ROOT_CAUSE_{i}"}
        for i in range(7)
    ]
    assert result.structured["provider_errors"] == errors


@pytest.mark.parametrize("asynchronous", [False, True])
def test_failed_mcp_delivery_does_not_reinsert_raw_protocol_metadata(runtime, asynchronous):
    raw = {"isError": True, "content": [{"type": "text", "text": "Remote operation failed",
            "_meta": {"debug": "internal-block"}}], "_meta": {"debug": "internal-envelope"}}
    mount_test_capability(runtime, alias="audit_mcp", canonical_path="op_audit_mcp",
        InputModel=EmptyToolInput, OutputModel=StructuredToolOutput, execution=INDIRECT_EXTERNAL_READ,
        handler=lambda _: normalize_tool_result(raw, server_id="server", tool_name="tool"))
    result = invoke(runtime, "audit_mcp", {}, asynchronous=asynchronous)
    assert not result.ok
    assert "Remote operation failed" in result.llm_text and "read_mcp_server" in result.llm_text
    assert "internal-" not in result.llm_text
    assert result.invocation_result.details["raw_result"] == raw


def test_explicit_failure_projection_preserves_raw_receipt(runtime):
    raw = {"error": "RuntimeError: short cause\ninternal traceback", "metadata": {"debug": "internal-only"}}
    mount_test_capability(runtime, alias="audit_failure", canonical_path="op_audit_failure",
        InputModel=EmptyToolInput, OutputModel=StructuredToolOutput, execution=INDIRECT_EXTERNAL_READ,
        handler=lambda _: CapabilityResult(status="error", llm_text="short cause", text="Failure",
            structured=raw, llm_failure_details={"error": "short cause"}))
    result = invoke(runtime, "audit_failure", {})
    assert result.invocation_result.details == raw
    assert "internal" not in result.llm_text


def test_capability_wrapper_preserves_failure_projection(runtime):
    from pal.execution.tool_search import _invocation_capability_result
    from pal.shared.tool_protocol import new_tool_call

    raw = {"metadata": {"debug": "internal-only"}}
    mount_test_capability(runtime, alias="audit_failure", canonical_path="op_audit_failure",
        InputModel=EmptyToolInput, OutputModel=StructuredToolOutput, execution=INDIRECT_EXTERNAL_READ,
        handler=lambda _: CapabilityResult(status="error", llm_text="short cause", text="Failure",
            structured=raw, llm_failure_details={}))
    invocation = runtime.invoke_indirect_tool(new_tool_call(name="audit_failure", args={}))
    wrapped = _invocation_capability_result(runtime, "audit_failure", invocation)
    record = runtime.registry_generation.record_for_alias("call_tool")
    delivered = runtime.deliver_invocation_result(record,
        new_tool_call(name="call_tool", args={"name": "audit_failure", "args": {}}),
        wrapped, budget=None, turn_id="review")
    assert "short cause" in delivered.llm_text
    assert "internal-only" not in delivered.llm_text
    assert delivered.details["details"] == raw
