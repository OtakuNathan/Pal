"""Generation and memory maintenance preserve the cause and the committed state."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pal.core.compaction import CompactionEngine, CompactionSnapshot
from pal.core.pal_compaction import PalCompactionPolicy
from pal.llm import LLMRuntime, EndpointResolver, generation_result_from_values
from pal.llm.ir import LLMResponseUpdate, LLMResponseDeltaKind
from pal.memory import MemoryService
from pal.memory.contracts import L3CommitRequest, L1TranscriptMessage, MemoryCommitRequest
from pal.memory.dreaming.service import DreamingService
from pal.shared.tool_protocol import new_tool_call
from pal.shared.json_values import thaw_json
from tests.test_llm_runtime_ir import _endpoint, _Settings, _request, _capture_turn_failure_signal
from tests.test_memory_dreaming import ReviewingLLM
from tests.test_memory_review import setup_review
from tests.test_runtime_compaction import _ScriptedLLM, _valid_pal_payload


def chained_error(message="provider failed"):
    try:
        raise OSError("backend diagnostic: upstream disconnected; api_key=hidden-secret")
    except OSError as exc:
        try:
            raise RuntimeError(message) from exc
        except RuntimeError as wrapped:
            return wrapped


def runtime_for(invoker, **config):
    return LLMRuntime(endpoint_resolver=EndpointResolver(endpoints=(_endpoint(),)),
        settings_repository=_Settings(), endpoint_invoker=invoker,
        config=SimpleNamespace(llm_endpoint_retry_attempts=2, **config))


def test_generation_failure_preserves_every_attempt_and_redacts_secrets(monkeypatch):
    class Invoker:
        calls = 0
        def invoke(self, *args, **kwargs):
            self.calls += 1
            raise chained_error(f"attempt diagnostic {self.calls}")
    monkeypatch.setattr("pal.llm.runtime._retry_delay", lambda _: 0)
    invoker = Invoker()
    result = runtime_for(invoker).generate(_request())
    visible = result.text + json.dumps(thaw_json(result.response.message.metadata))
    assert "backend diagnostic: upstream disconnected" in visible
    assert "attempt diagnostic 1" in visible and "attempt diagnostic 2" in visible
    assert "hidden-secret" not in visible
    signal = _capture_turn_failure_signal(result.response)
    assert "backend diagnostic: upstream disconnected" in signal.primary_blocker


def test_stream_failure_preserves_reason_but_revokes_incomplete_tool_calls():
    partial = generation_result_from_values(text="partial answer", tool_calls=[new_tool_call(name="run_shell", args={"cmd": "work"})]).response
    class Invoker:
        def invoke_updates(self, *args, **kwargs):
            yield LLMResponseUpdate(partial, LLMResponseDeltaKind.TEXT, text_delta="partial answer")
            raise chained_error()
    updates = list(runtime_for(Invoker())._iter_stream_updates(_request()))
    final = updates[-1].response
    assert final.message.message_id == partial.message.message_id
    assert final.finish_reason == "error"
    assert not final.tool_calls
    assert "backend diagnostic: upstream disconnected" in str(dict(final.message.metadata))
    signal = _capture_turn_failure_signal(final)
    assert "backend diagnostic: upstream disconnected" in signal.primary_blocker


def test_wall_timeout_targets_the_partial_message_for_discard(monkeypatch):
    runtime = runtime_for(SimpleNamespace(), llm_stream_wall_timeout_seconds=0.02,
                          llm_stream_cleanup_timeout_seconds=0.5)
    partial = generation_result_from_values(text="partial answer").response
    def updates(request, *, stream_control, **kwargs):
        yield LLMResponseUpdate(partial, LLMResponseDeltaKind.TEXT, text_delta="partial answer")
        while not stream_control.cancelled:
            time.sleep(0.001)
    monkeypatch.setattr(runtime, "_iter_stream_updates", updates)
    async def collect():
        return [item async for item in runtime.astream(_request())]
    final = asyncio.run(collect())[-1].response
    assert final.finish_reason == "error"
    assert final.message.message_id == partial.message.message_id
    assert final.message.metadata["partial_output_chars"] == len("partial answer")
    assert "wall-clock limit" in str(dict(final.message.metadata))


@pytest.fixture
def dreaming(setup_review):
    _, provider, storage = setup_review
    item = L3CommitRequest(kind="fact", title="API preference", summary="Use explicit APIs", search_text="Use explicit APIs", topics=["API"])
    provider.commit(item)
    provider.commit(replace(item, title="Interface preference"))
    return DreamingService(storage=storage, provider=provider, llm=ReviewingLLM())


def test_dreaming_provider_error_retains_response_and_metadata(dreaming):
    class LLM(ReviewingLLM):
        async def agenerate(self, request):
            response = generation_result_from_values(text="provider diagnostic: account unavailable", finish_reason="error").response
            return SimpleNamespace(response=replace(response, message=replace(response.message,
                metadata={"backend_error": "original provider error body"})))
    dreaming.llm = LLM()
    result = asyncio.run(dreaming.run())
    assert result["status"] == "failed"
    assert "provider diagnostic: account unavailable" in result["report"]["error"]
    assert "original provider error body" in result["report"]["error"]


def test_dreaming_post_publish_error_does_not_disappear(dreaming, monkeypatch):
    before = dreaming.storage.current()
    publish = dreaming.storage.publish
    def publish_then_fail(*args, **kwargs):
        publish(*args, **kwargs)
        raise chained_error("publication acknowledgement failed")
    monkeypatch.setattr(dreaming.storage, "publish", publish_then_fail)
    result = asyncio.run(dreaming.run())
    assert result["status"] == "completed"
    assert dreaming.storage.current() != before
    assert "backend diagnostic: upstream disconnected" in result["report"]["post_publish_error"]


def test_dreaming_cancel_preserves_worker_failure(dreaming):
    started = threading.Event()
    release = threading.Event()
    def worker():
        started.set()
        release.wait(5)
        raise chained_error("worker failed while cancelling")
    async def cancel():
        task = asyncio.create_task(dreaming._work(worker))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.__cause__ is not None
        assert "worker failed while cancelling" in str(caught.value.__cause__)
    asyncio.run(cancel())


def test_dreaming_background_cleanup_failure_remains_inspectable(dreaming):
    dreaming.run_id = dreaming.create_run()
    async def fail():
        raise chained_error("background cleanup failed")
    async def finish():
        task = asyncio.create_task(fail())
        try:
            await task
        except RuntimeError:
            pass
        dreaming._task_done(task)
    asyncio.run(finish())
    status = dreaming.status(dreaming.run_id)
    assert status["status"] == "failed"
    assert "backend diagnostic: upstream disconnected" in status["task_error"]
    assert "backend diagnostic: upstream disconnected" in status["report"]["background_error"]


def test_memory_commit_failure_keeps_cause_and_uncertain_effect(monkeypatch):
    memory = MemoryService()
    def append(*args, **kwargs):
        raise chained_error("L1 persistence failed")
    monkeypatch.setattr(memory.l1_store, "append", append)
    result = memory.commit_l1(MemoryCommitRequest(turn_id="audit", transcript=[L1TranscriptMessage(role="user", content="request")]))
    assert result.status == "retry"
    assert "backend diagnostic: upstream disconnected" in result.metadata["error"]
    assert result.metadata["effect"] == "unknown"


def test_provider_resolution_exception_is_not_absent_provider():
    memory = MemoryService()
    def resolve():
        raise chained_error("provider registry failed")
    memory.l3_selector = SimpleNamespace(resolve=resolve)
    with pytest.raises(RuntimeError, match="provider registry failed"):
        memory._resolve_l3_provider()


@pytest.mark.parametrize("mode", ["exception", "error_response", "commit"])
def test_compaction_keeps_actual_error_in_result(mode, monkeypatch):
    policy = PalCompactionPolicy()
    engine = CompactionEngine(policy, max_attempts=1)
    snapshot = CompactionSnapshot(target_input_budget=8192, reserved_output_tokens=1024,
        clock_kind=policy.clock_kind, clock_value=1,
        memory_items=((L1TranscriptMessage(role="user", content="Keep this evidence"),),))
    outcome = (chained_error() if mode == "exception" else generation_result_from_values(
        text="compaction provider diagnostic" if mode == "error_response" else _valid_pal_payload(),
        finish_reason="error" if mode == "error_response" else "stop"))
    async def commit(*args, **kwargs):
        return chained_error("checkpoint persistence failed")
    if mode == "commit":
        monkeypatch.setattr(engine, "_commit", commit)
    result = asyncio.run(engine.run(snapshot, llm_runtime=_ScriptedLLM([outcome]), memory_service=MemoryService()))
    assert not result.success
    details = str(result)
    assert ("compaction provider diagnostic" if mode == "error_response" else "backend diagnostic: upstream disconnected") in details
    assert "hidden-secret" not in details


@pytest.mark.parametrize("event", ["complete", "response.failed", "error"])
def test_responses_decoder_delivers_provider_error_body(event):
    from pal.llm.ir import WireShape
    from pal.llm.shapes.base import ShapeContext, _JSONFrame
    from pal.llm.shapes.openai_response import OpenAIResponseDecoder
    decoder = OpenAIResponseDecoder(ShapeContext(wire_shape=WireShape.OPENAI_RESPONSE, endpoint_id="ep", model_id="test"))
    payload = {"status": "failed", "output": [], "error": {"code": "provider_failure",
        "message": "original Responses diagnostic", "api_key": "hidden-secret"}}
    if event == "response.failed":
        payload = {"type": event, "response": payload}
    elif event == "error":
        payload = {"type": event, **payload["error"]}
    decoder.feed(_JSONFrame(0, payload))
    response = decoder.finish()
    assert response.finish_reason == "error"
    class Invoker:
        def invoke(self, *args, **kwargs):
            return response, ()
    runtime = runtime_for(Invoker())
    runtime.endpoint_retry_attempts = 1
    final = runtime.generate(_request())
    assert "original Responses diagnostic" in final.text
    assert "hidden-secret" not in final.text


def test_subscription_error_retains_redacted_provider_detail():
    from pal.llm.chatgpt import ChatGPTError, response_error
    error = response_error({"error": {"code": "request_failed", "message": "subscription provider diagnostic",
                                     "access_token": "hidden-secret"}})
    assert "subscription provider diagnostic" in str(error)
    assert "hidden-secret" not in str(error)
    assert "subscription provider diagnostic" in str(ChatGPTError.from_dict(error.to_dict()))


def test_dreaming_correction_prompt_keeps_error_beyond_old_cutoff(dreaming, monkeypatch):
    from pal.memory.dreaming.contracts import BatchResult
    original = BatchResult.model_validate_json
    count = 0
    def validate(cls, *args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            raise ValueError("long validation detail " + "x" * 700 + " ACTUAL_CORRECTION_AT_TAIL")
        return original(*args, **kwargs)
    monkeypatch.setattr(BatchResult, "model_validate_json", classmethod(validate))
    dreaming.llm = ReviewingLLM(merge=False)
    result = asyncio.run(dreaming.run())
    assert result["status"] == "completed"
    assert "ACTUAL_CORRECTION_AT_TAIL" in dreaming.llm.calls[1].messages[0].text


def test_compaction_cleanup_error_keeps_committed_receipt():
    from tests.test_memory_two_segment_service import _entry
    memory = MemoryService()
    memory.l1_store.append([L1TranscriptMessage(role="user", content="Keep this evidence")])
    memory.history_root.promote()
    memory.begin_left_compaction("audit", reason="auto")
    policy = PalCompactionPolicy()
    snapshot = CompactionSnapshot(target_input_budget=8192, reserved_output_tokens=1024,
        clock_kind=policy.clock_kind, clock_value=1, metadata={"two_segment_run_id": "audit"})
    def cleanup():
        raise chained_error("cleanup after checkpoint failed")
    result = asyncio.run(CompactionEngine(policy)._commit(snapshot, memory_service=memory,
        summary_entry=_entry("Committed summary"), after_commit=cleanup))
    assert result.metadata["status"] == "committed"
    assert "backend diagnostic: upstream disconnected" in result.metadata["post_commit_detail"]
    assert "Committed summary" in memory.l1_store.turns.turns[0].messages[0].text
