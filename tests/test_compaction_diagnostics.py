import asyncio
import logging
from dataclasses import replace

from pal.core.compaction import CompactionEngine
from pal.core.pal_compaction import PalCompactionPolicy
from pal.llm import generation_result_from_values
from pal.shared import LLMFinishReason
from tests.test_runtime_compaction import (
    _ScriptedLLM, _SlowLLM, _memory_with_turns, _snapshot, _valid_pal_payload,
)


def run_compact(llm, caplog, **kwargs):
    service = _memory_with_turns()
    snapshot = replace(_snapshot(service), metadata={"preferred_endpoint_id": "test-endpoint"})
    with caplog.at_level(logging.INFO, logger="pal.core.compaction"):
        return asyncio.run(CompactionEngine(PalCompactionPolicy(), **kwargs).run(
            snapshot, llm_runtime=llm, memory_service=service,
        ))


def test_schema_retry_logs_reason_and_recovery_without_source(caplog):
    payload = _valid_pal_payload()
    llm = _ScriptedLLM([
        generation_result_from_values(text="PRIVATE invalid checkpoint"),
        generation_result_from_values(text=payload),
    ])
    result = run_compact(llm, caplog)
    assert result.success
    assert "reason=schema:output is not valid JSON" in caplog.text
    assert "endpoint=test-endpoint attempt=1" in caplog.text
    assert "status=compacted attempts=2" in caplog.text
    assert "PRIVATE" not in caplog.text
    assert payload not in caplog.text


def test_timeout_logs_each_attempt_and_terminal_status(caplog):
    result = run_compact(_SlowLLM([]), caplog, timeout_seconds=0.01, max_attempts=2)
    assert result.status == "failed"
    assert caplog.text.count("reason=endpoint:TimeoutError") == 2
    assert "timeout_seconds=0.01" in caplog.text
    assert "status=failed attempts=2" in caplog.text


def test_endpoint_error_metadata_survives_without_response_body(caplog):
    outcome = generation_result_from_values(text="PRIVATE provider response", finish_reason=LLMFinishReason.ERROR)
    outcome = replace(outcome, response=replace(outcome.response, message=replace(
        outcome.response.message, metadata={"failure_kind": "rate_limit", "error_type": "RateLimitError"},
    )))
    result = run_compact(_ScriptedLLM([outcome]), caplog, max_attempts=1)
    assert result.status == "failed"
    assert "failure_kind=rate_limit error_type=RateLimitError" in caplog.text
    assert "PRIVATE" not in caplog.text


def test_schema_extra_keys_are_not_written_to_journal(caplog):
    result = run_compact(_ScriptedLLM([generation_result_from_values(
        text='{"PRIVATE_SOURCE_TEXT": "PRIVATE_BODY"}',
    )]), caplog, max_attempts=1)
    assert result.status == "failed"
    assert "reason=schema:checkpoint has extra fields" in caplog.text
    assert "PRIVATE" not in caplog.text


def test_summary_explains_silent_continuity_use():
    service = _memory_with_turns()
    entry = PalCompactionPolicy().validate_checkpoint(_valid_pal_payload(), _snapshot(service))
    assert "provided only to preserve continuity" in entry.rendered
    assert "Do not repeatedly mention this summary" in entry.rendered
    assert "when the user asks about it" in entry.rendered


def test_preflight_failure_does_not_reuse_previous_response_usage(caplog, monkeypatch):
    from pal.llm import LLMPreflightAdvice
    from pal.shared import LLMPreflightStatus

    llm = _ScriptedLLM([generation_result_from_values(
        text="invalid JSON", input_tokens=123, output_tokens=45,
    )])
    calls = [0]

    def preflight(request):
        calls[0] += 1
        return LLMPreflightAdvice(status=(
            LLMPreflightStatus.READY if calls[0] == 1
            else LLMPreflightStatus.COMPACT_REQUIRED
        ))

    llm.preflight_hook = preflight
    monkeypatch.setattr(CompactionEngine, "_shrink", lambda *args, **kwargs: None)
    result = run_compact(llm, caplog)
    assert result.status == "failed"
    record = next(r.getMessage() for r in caplog.records
                  if "reason=input:base_context_over_budget" in r.getMessage())
    assert "input_tokens=0 output_tokens=0" in record
