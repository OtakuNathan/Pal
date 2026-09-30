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
