"""Original verifier failure provenance without changing public tool results."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from pal.bunshin.runner_components.heartbeat import Heartbeat
from pal.bunshin.runner_components.reporter import Reporter
from pal.bunshin.runner_components.tool_observation import ToolObservation
from pal.bunshin.runner_components.tool_session import ToolSession
from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
from pal.bunshin.verifier_tool_diagnostics import (
    VerifierFailureProvenance, VerifierToolDiagnostic, capture_verifier_failure,
    record_verifier_failure,
)
from pal.bunshin.v2.semantic_evidence import _error_result
from pal.execution.runtime import ExecutionRuntime
from pal.shared import BunshinInvocationPack, ToolExecutionResult
from pal.shared.tool_protocol import new_tool_call

SECRET = "PRIVATE_ARGUMENT_PROMPT_EXCEPTION_CREDENTIAL"
NAME = "op_bunshin_verification_run_historical_regression"
ALIAS = "run_verification_historical_regression"


def tool_call():
    return new_tool_call(name=ALIAS, args={"name": "synthetic", "command": "true"})


def binding(stale):
    return {} if not stale else {"workflow_id": "fixture", "invocation_id": "fixture",
        "lease_resource_key": "fixture", "fencing_token": 1, "role": "verifier", "mode": "module",
        "authoring_input_fingerprint": "fixture", "authoring_contract_version": "7"}


def public_result(result):
    return (result.ok, result.status, result.text, result.llm_text, result.structured,
            result.invocation_result.model_dump())


@pytest.mark.parametrize("stale", [False, True])
def test_real_wrapper_and_heartbeat_preserve_original_failure_without_public_changes(tmp_path, stale):
    async def scenario():
        runtime = ExecutionRuntime(runtime_root=tmp_path)
        scoped = BunshinScopedExecutionRuntime(runtime, [NAME], workspace={
            "run_id": "run", "runtime_root": str(tmp_path), "bunshin_v2": binding(stale)})
        events = []
        async def write(event):
            events.append(event)
        pack = BunshinInvocationPack(invocation_id="session", metadata={
            "bunshin_v2": {"role": "verifier"}, "heartbeat_interval_seconds": 0.01})
        reporter = Reporter("session", pack, "run", write)
        async def execute(_runtime, call, **kwargs):
            return await scoped.execute_tool_async(call)
        observation = ToolObservation(Heartbeat(reporter, pack), reporter,
            SimpleNamespace(execute_allowed_tool=execute), ToolSession())
        try:
            baseline = await scoped.execute_tool_async(tool_call())
            result = await observation.execute_bunshin_tool_with_observation(
                SimpleNamespace(execution_runtime=SimpleNamespace(), llm_round_count=87, tool_call_count=0),
                SimpleNamespace(pending_tool_results=[], turn_id="turn"), tool_call())
            assert public_result(result) == public_result(baseline)
            assert result.status == "handler_exception"
            item = next(event["payload"] for event in events
                        if event["event_kind"] == "verifier_tool_diagnostic" and event["payload"]["stage"] == "completed")
            provenance = item["provenance"]
            assert provenance["error_type"] == "ValueError"
            assert provenance["frames"][-1]["file"] == "submission_drafts.py"
            assert provenance["frames"][-1]["function"] == "SubmissionDraftContext.from_workspace"
            # These errors originate on different source lines, before any DB read.
            expected_line = 114 if stale else 112
            assert provenance["frames"][-1]["line"] == expected_line
            assert SECRET not in json.dumps(item)
            assert "provenance" not in result.llm_text
            assert "provenance" not in json.dumps(result.structured)
            assert len(json.dumps(item)) < 2048
            with capture_verifier_failure(enabled=True) as next_capture:
                assert next_capture.provenance is None
        finally:
            scoped.base_runtime.runtime.shutdown()
            runtime.shutdown()
    asyncio.run(scenario())


def test_dynamic_exception_and_forged_frame_names_are_not_copied():
    namespace = {"SecretError": type(SECRET, (Exception,), {})}
    exec(compile("def from_workspace():\n    raise SecretError('" + SECRET + "')\n",
                 "/private/submission_drafts.py", "exec"), namespace)
    with capture_verifier_failure(enabled=True) as capture:
        try:
            namespace["from_workspace"]()
        except Exception as exc:
            record_verifier_failure(exc)
    assert capture.provenance.model_dump() == {"error_type": "other", "frames": []}
    assert SECRET not in capture.provenance.model_dump_json()


@pytest.mark.parametrize("provenance", [
    {"error_type": SECRET, "frames": []},
    {"error_type": "ValueError", "frames": [], "message": SECRET},
    {"error_type": "ValueError", "frames": [{"file": SECRET, "function": "from_workspace", "line": 3}]},
    {"error_type": "ValueError", "frames": [{"file": "submission_drafts.py", "function": SECRET, "line": 3}]},
    {"error_type": "ValueError", "frames": [{"file": "submission_drafts.py", "function": "SubmissionDraftContext.from_workspace", "line": 3, "locals": SECRET}]},
    *({"error_type": "ValueError", "frames": [{"file": "submission_drafts.py", "function": "SubmissionDraftContext.from_workspace", "line": line}]} for line in [True, -1, 10**100, "3"]),
    {"error_type": "ValueError", "frames": [{"file": "submission_drafts.py", "function": "SubmissionDraftContext.from_workspace", "line": 3}] * 7},
])
def test_nested_wire_provenance_rejects_unknown_or_unbounded_fields(provenance):
    with pytest.raises(ValidationError):
        VerifierToolDiagnostic.model_validate({"round": 1, "tool_call_index": 0,
            "tool_alias": ALIAS, "stage": "completed", "provenance": provenance})


def test_capture_is_concurrent_scoped_reset_and_never_copies_text():
    async def child(error):
        await asyncio.sleep(0)
        try:
            raise error(SECRET)
        except Exception as exc:
            result = _error_result(tool_call(), exc)
            assert "provenance" not in result.structured
    async def task(error):
        with capture_verifier_failure(enabled=True) as capture:
            await asyncio.create_task(child(error))
        return capture.provenance.error_type
    async def scenario():
        assert await asyncio.gather(task(ValueError), task(TypeError)) == ["ValueError", "TypeError"]
        with capture_verifier_failure(enabled=True) as outer:
            with capture_verifier_failure(enabled=False) as disabled:
                await child(ValueError)
            assert disabled.provenance is None and outer.provenance is None
            await child(KeyError)
        assert outer.provenance.error_type == "KeyError"
        await child(TypeError)
        assert outer.provenance.error_type == "KeyError"
        with capture_verifier_failure(enabled=True) as next_call:
            pass
        assert next_call.provenance is None
    asyncio.run(scenario())


def test_capture_failure_cannot_replace_original_tool_error():
    try:
        raise ValueError(SECRET)
    except ValueError as exc:
        baseline = _error_result(tool_call(), exc)
        with capture_verifier_failure(enabled=True) as capture:
            with patch("pal.bunshin.verifier_tool_diagnostics.VerifierFailureProvenance", side_effect=RuntimeError(SECRET)):
                result = _error_result(tool_call(), exc)
        assert result.ok == baseline.ok
        assert result.status == baseline.status
        assert result.llm_text == baseline.llm_text
        assert result.structured == baseline.structured
        assert capture.provenance is None


def test_observation_does_not_attach_previous_failure_to_next_success():
    async def scenario():
        events = []
        async def write(event):
            events.append(event)
        async def execute(_runtime, call, **kwargs):
            await asyncio.sleep(0)
            if call.args["name"] == "synthetic":
                try:
                    raise ValueError(SECRET)
                except ValueError as exc:
                    return _error_result(call, exc)
            return ToolExecutionResult(name=ALIAS, ok=True, llm_text="done")
        pack = BunshinInvocationPack(invocation_id="session", metadata={
            "bunshin_v2": {"role": "verifier"}, "heartbeat_interval_seconds": 0.01})
        reporter = Reporter("session", pack, "run", write)
        observation = ToolObservation(Heartbeat(reporter, pack), reporter,
            SimpleNamespace(execute_allowed_tool=execute), ToolSession())
        state = SimpleNamespace(execution_runtime=SimpleNamespace(), llm_round_count=1, tool_call_count=0)
        continuation = SimpleNamespace(pending_tool_results=[], turn_id="turn")
        await observation.execute_bunshin_tool_with_observation(state, continuation, tool_call())
        await observation.execute_bunshin_tool_with_observation(state, continuation,
            new_tool_call(name=ALIAS, args={"name": "next", "command": "true"}))
        payloads = [event["payload"] for event in events
                    if event["event_kind"] == "verifier_tool_diagnostic" and event["payload"]["stage"] == "completed"]
        assert payloads[0]["provenance"]["error_type"] == "ValueError"
        assert payloads[1]["ok"] is True and payloads[1]["provenance"] is None
    asyncio.run(scenario())


def test_capture_resets_on_cancellation():
    async def scenario():
        captured = None
        try:
            with capture_verifier_failure(enabled=True) as captured:
                raise asyncio.CancelledError()
        except asyncio.CancelledError:
            pass
        try:
            raise ValueError(SECRET)
        except ValueError as exc:
            record_verifier_failure(exc)
        assert captured.provenance is None
    asyncio.run(scenario())
