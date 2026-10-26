import json

import pytest

from pal.llm.decode_diagnostics import DecodeFailureCapture
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext, ShapeDecodeError, _JSONFrame
from pal.llm.ir import WireShape


def test_capture_reproduces_decoder_failure_and_retains_exception_cause(tmp_path, monkeypatch):
    monkeypatch.setenv("PAL_LOG_ROOT", str(tmp_path))
    frame = _JSONFrame(0, {
        "type": "response.output_item.done", "output_index": 0,
        "item": {"type": "function_call", "call_id": "c", "name": "read", "arguments": '{"x":'},
    })
    capture = DecodeFailureCapture()
    capture.observe(frame)
    assert not list(tmp_path.iterdir())
    context = ShapeContext(WireShape.OPENAI_RESPONSE, "ep", "model")
    codec = codec_for_shape(context.wire_shape)
    with pytest.raises(ShapeDecodeError) as error:
        list(codec.decode([frame], context))
    path = capture.save(error.value, attempt_id="attempt")
    data = json.loads(path.read_text())
    assert "JSONDecodeError" in data["traceback"]
    assert data["complete_capture"]
    with pytest.raises(ShapeDecodeError, match="invalid JSON"):
        list(codec.decode([_JSONFrame(**entry) for entry in data["frames"]], context))
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_capture_bounds_tail_and_marks_incomplete(tmp_path, monkeypatch):
    monkeypatch.setenv("PAL_LOG_ROOT", str(tmp_path))
    capture = DecodeFailureCapture(max_bytes=4096)
    for sequence in range(100):
        capture.observe(_JSONFrame(sequence, {"delta": "x" * 100}))
    capture.observe(_JSONFrame(100, {"delta": "y" * 20000}))
    assert capture.retained_bytes <= 4096
    data = json.loads(capture.save(ShapeDecodeError("test")).read_text())
    assert data["total_frames"] == 101
    assert data["dropped_frames"] > 0
    assert data["truncated_frames"] == 1
    assert not data["complete_capture"]
    assert data["frames"][-1]["sequence"] == 100
    assert data["frames"][-1]["truncated"]


def test_capture_retention_and_disk_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("PAL_LOG_ROOT", str(tmp_path))
    monkeypatch.setattr("pal.llm.decode_diagnostics.MAX_CAPTURES", 2)
    capture = DecodeFailureCapture()
    for attempt in range(4):
        assert capture.save(ShapeDecodeError("test"), attempt_id=attempt)
    files = list((tmp_path / "llm-decode-failures").glob("*.json"))
    assert {json.loads(path.read_text())["attempt_id"] for path in files} == {2, 3}
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocked")
    monkeypatch.setenv("PAL_LOG_ROOT", str(blocker))
    assert capture.save(ShapeDecodeError("original")) is None


def test_unserializable_diagnostic_does_not_interrupt_response(tmp_path, monkeypatch):
    monkeypatch.setenv("PAL_LOG_ROOT", str(tmp_path))
    capture = DecodeFailureCapture()
    capture.observe(_JSONFrame(0, {"extension": object()}))
    data = json.loads(capture.save(ShapeDecodeError("original")).read_text())
    assert data["capture_errors"] == 1
    assert not data["complete_capture"]


def test_endpoint_logs_full_decode_chain_at_warning(tmp_path, monkeypatch, caplog):
    from pal.llm.endpoint import ShapeEndpointInvoker
    from tests.test_llm_runtime_ir import _endpoint, _request

    monkeypatch.setenv("PAL_LOG_ROOT", str(tmp_path))

    class Transport:
        def frames(self, endpoint, request):
            yield _JSONFrame(0, {"choices": [{
                "message": {"role": "assistant", "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "read_file", "arguments": '{"range": 1,120}'},
                }]}, "finish_reason": "tool_calls",
            }]})

    with pytest.raises(ShapeDecodeError):
        ShapeEndpointInvoker(transport=Transport()).invoke(_endpoint(), _request())
    record = next(r for r in caplog.records if "LLM decode failed" in r.message)
    assert record.levelname == "WARNING"
    assert "failed_at=" in record.message
    assert "capture=" in record.message
    assert "Traceback (most recent call last)" in record.message
    assert "JSONDecodeError" in record.message
    assert "ShapeDecodeError" in record.message


@pytest.mark.parametrize("failure", ["hook", "transport", "terminal"])
def test_endpoint_captures_entire_observed_round_and_request_r(failure, tmp_path, monkeypatch):
    from dataclasses import replace
    from pal.llm.endpoint import ShapeEndpointInvoker
    from pal.llm.response_hooks import ProviderResponseHook, ProviderResponseHookRegistry, ProviderResponseHookError
    from tests.test_llm_runtime_ir import _endpoint, _request

    monkeypatch.setenv("PAL_LOG_ROOT", str(tmp_path))
    context = {
        "turn_id": "turn-1", "llm_round_index": 3,
        "history_cut": {"turn_count": 2, "intra_messages": 4, "revision": 9, "cut_id": "cut-9"},
        "right_messages": [{"message_id": "right-1", "role": "user", "parts": [{"kind": "text", "text": "R input"}]}],
    }
    request = replace(_request(), metadata={"failure_context": context})
    payload = {"choices": [{"delta": {"reasoning_content": "Original failing round"}, "finish_reason": None}]}

    class Transport:
        def frames(self, endpoint, request):
            assert "failure_context" not in request.payload
            yield _JSONFrame(0, payload)
            if failure == "transport":
                raise RuntimeError("stream disconnected")
            yield _JSONFrame(1, {"choices": [{"delta": {"content": "last piece"}, "finish_reason": "error" if failure == "terminal" else "stop"}]})

    def reject(_context, updates):
        for update in updates:
            if "last piece" in update.response.text:
                raise ProviderResponseHookError("response rejected")
            yield update

    registry = ProviderResponseHookRegistry({"test": ProviderResponseHook("test", reject)}) if failure == "hook" else ProviderResponseHookRegistry({})
    invoker = ShapeEndpointInvoker(transport=Transport(), response_hooks=registry)
    if failure == "terminal":
        response = list(invoker.invoke_updates(_endpoint(), request))[-1].response
        assert response.finish_reason.value == "error"
    else:
        with pytest.raises(RuntimeError):
            list(invoker.invoke_updates(_endpoint(), request))
    files = list(tmp_path.glob("llm-decode-failures/*.json"))
    assert len(files) == 1
    document = json.loads(files[0].read_text())
    assert document["frames"][0]["payload"] == payload
    assert document["total_frames"] == (1 if failure == "transport" else 2)
    assert document["request_context"]["snapshot"] == context
    assert document["request_context"]["complete_capture"]
    assert document["complete_capture"]
    assert files[0].stat().st_mode & 0o777 == 0o600


def test_context_truncation_is_explicit_and_success_never_writes_capture(tmp_path, monkeypatch):
    from pal.llm.endpoint import ShapeEndpointInvoker
    from tests.test_llm_runtime_ir import _endpoint, _request

    monkeypatch.setenv("PAL_LOG_ROOT", str(tmp_path))
    monkeypatch.setattr("pal.llm.decode_diagnostics.MAX_CONTEXT_BYTES", 1024)
    context = {"turn_id": "T", "history_cut": {"cut_id": "cut-1"},
               "right_messages": ["x" * 10000, "tail evidence"]}
    capture = DecodeFailureCapture()
    document = json.loads(capture.save(RuntimeError("failed"), request_context=context).read_text())
    assert not document["request_context"]["complete_capture"]
    assert "tail evidence" in document["request_context"]["snapshot_tail"]
    assert document["request_context"]["snapshot_identity"] == {
        "turn_id": "T", "history_cut": {"cut_id": "cut-1"},
    }
    before = set(tmp_path.rglob("*.json"))

    class Transport:
        def frames(self, endpoint, request):
            yield _JSONFrame(0, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    ShapeEndpointInvoker(transport=Transport()).invoke(_endpoint(), _request())
    assert set(tmp_path.rglob("*.json")) == before


def test_executor_freezes_r_before_submission_moves_cut_and_keeps_failure_out_of_l1(tmp_path, monkeypatch):
    from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
    from pal.memory import MemoryService
    from tests.test_v3_n3_vertical_trace import _runtime, _executor, _request_for, _drive

    monkeypatch.setenv("PAL_LOG_ROOT", str(tmp_path))
    memory = MemoryService()
    memory.begin_l1_turn("old", user_message=LLMMessageIR(
        MessageRole.USER, (TextPartIR("already submitted"),), message_id="left-1",
    ))
    memory.settle_l1_turn("old")
    memory.history_root.promote()
    memory.begin_l1_turn("T", user_message=LLMMessageIR(
        MessageRole.USER, (TextPartIR("current R input"),), message_id="right-1",
    ))
    cut_before = memory.history_root.cut

    class Transport:
        def frames(self, endpoint, request):
            assert "failure_context" not in request.payload
            yield _JSONFrame(0, {"choices": [{"delta": {"reasoning_content": "failed reasoning"}}]})
            yield _JSONFrame(1, {"choices": [{"message": {"tool_calls": [{
                "id": "broken", "type": "function",
                "function": {"name": "read_file", "arguments": '{"target":'},
            }]}, "finish_reason": "tool_calls"}]})

    executor = _executor(memory, _runtime(Transport()))
    _drive(executor, _request_for(memory, ()))
    document = json.loads(next(tmp_path.glob("llm-decode-failures/*.json")).read_text())
    context = document["request_context"]["snapshot"]
    assert context["turn_id"] == "T"
    assert context["llm_round_index"] == 1
    assert context["history_cut"]["cut_id"] == cut_before.cut_id
    assert context["history_cut"]["revision"] == cut_before.revision
    assert [m["message_id"] for m in context["right_messages"]] == ["right-1"]
    assert memory.history_root.cut.revision > cut_before.revision
    assert "failed reasoning" in json.dumps(document["frames"])
    assert all("failed reasoning" not in m.reasoning_text
               for turn in memory.history.turns for m in turn.messages)
