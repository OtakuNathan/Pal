"""Worker and control results retain failures alongside committed effects."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pal.bunshin import worker_main
from pal.bunshin.contracts import AggregateType, PermanentEffectError
from pal.bunshin.orchestration import BunshinOutboxProcessor
from pal.bunshin.process_lifecycle import WorkerProcessOwner
from pal.bunshin.runner import BunshinRunner, BunshinLLMRetryableError
from pal.bunshin.semantic_orchestration.attempt_models import CollectedRoleTerminal, ExitedRoleProcess
from pal.bunshin.semantic_orchestration.attempt_process_result import ProcessResult
from pal.bunshin.semantic_orchestration.attempt_terminal_validation import TerminalValidation
from pal.bunshin.semantic_orchestration.worker_results import _worker_terminal_failure, _worker_stderr_failures
from pal.bunshin.worker_events import WorkerEventWriter, WorkerEventDeliveryError
from pal.bunshin.worker_events import run_worker_with_events
from pal.control.contracts import ControlAction, ControlRoute
from pal.core import PalCore
from pal.core.compaction import CompactionRunResult
from pal.core.turns import EffectResult
from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EffectOutcome, EffectReceipt
from pal.foundation.diagnostics import exception_report
from pal.llm import generation_result_from_values
from pal.memory.contracts import MemoryCompactResult
from pal.memory import MemoryService
from tests.test_bunshin_completion_resume import make_runner


def failure(message="worker failed"):
    try:
        raise OSError("storage root cause; api_key=hidden-secret")
    except OSError as cause:
        try:
            raise RuntimeError(message) from cause
        except RuntimeError as exc:
            return exc


def assert_diagnostic(text):
    assert "storage root cause" in text
    assert "hidden-secret" not in text


def test_worker_wire_preserves_exception_group_and_causes(monkeypatch, capsys):
    async def run(*args):
        raise ExceptionGroup("worker shutdown failed", [failure(), ValueError("second cleanup failure")])
    monkeypatch.setattr(worker_main, "_run", run)
    assert worker_main.main(["--runtime-root", "/unused", "--pack-json", "/unused",
                             "--bunshin-id", "b", "--run-id", "r"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert_diagnostic(payload["error"])
    assert "second cleanup failure" in payload["error"]


def test_terminal_delivery_failure_retains_primary_and_cleanup_errors():
    runner = object.__new__(BunshinRunner)
    runner.components = SimpleNamespace(
        invocation=SimpleNamespace(close_execution_work=AsyncMock(side_effect=failure("cleanup failed"))),
        reporter=SimpleNamespace(emit=AsyncMock(side_effect=OSError("terminal pipe failed"))),
    )
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(runner._report_terminal_after_cleanup(None, {"status": "failed", "error": "original operation failed"}))
    text = exception_report(caught.value)
    assert_diagnostic(text)
    assert "original operation failed" in text
    assert "cleanup failed" in text and "terminal pipe failed" in text


def test_confirmed_writer_failure_keeps_nested_cause():
    async def scenario():
        async def write(line):
            raise failure("pipe failed")
        async with WorkerEventWriter(write) as writer:
            with pytest.raises(WorkerEventDeliveryError) as caught:
                await writer.write_event({"event_kind": "terminal"})
            assert_diagnostic(exception_report(caught.value))
    asyncio.run(scenario())


@pytest.mark.parametrize("newline", [b"", b"\n"])
def test_worker_pipe_retains_oversized_error_frame(newline):
    async def scenario():
        reader = asyncio.StreamReader(limit=1024)
        frame = json.dumps({"kind": "worker_error", "error": "start " + "x" * 100_000 + " end"}).encode() + newline
        owner = object.__new__(WorkerProcessOwner)
        owner._stdout = reader
        owner._stdout_read_lock = asyncio.Lock()
        owner._closing = False
        async def feed():
            for offset in range(0, len(frame), 511):
                reader.feed_data(frame[offset:offset + 511])
                await asyncio.sleep(0)
            reader.feed_eof()
        task = asyncio.create_task(feed())
        received = [line async for line in owner.stdout_lines()]
        await task
        assert received == [frame]
    asyncio.run(scenario())


def test_stderr_fallback_keeps_every_worker_error():
    stderr = "\n".join(json.dumps({"kind": "worker_event_fallback", "message": {
        "kind": "worker_error", "error": item,
    }}) for item in ("original failure", "shutdown failure"))
    _, text = _worker_stderr_failures(stderr)
    assert "original failure" in text and "shutdown failure" in text


def test_stderr_fallback_reports_transport_failure_alongside_original():
    async def scenario():
        frames = []
        async def write(line):
            raise failure("pipe unavailable")
        async def fallback(line):
            frames.append(json.loads(line))
        async with WorkerEventWriter(write, fallback=fallback) as writer:
            with pytest.raises(WorkerEventDeliveryError):
                await writer.write_error(ValueError("primary operation failed"))
        stderr = "\n".join(json.dumps({"kind": "worker_event_fallback", "message": item}) for item in frames)
        _, diagnostic = _worker_stderr_failures(stderr)
        assert "primary operation failed" in diagnostic
        assert "pipe unavailable" in diagnostic
        assert_diagnostic(diagnostic)
    asyncio.run(scenario())


def test_worker_shutdown_failure_does_not_replace_original(monkeypatch, capsys):
    close = WorkerEventWriter.close
    async def fail_close(self):
        await close(self)
        raise failure("sender shutdown failed")
    async def run(write):
        raise ValueError("primary operation failed")
    monkeypatch.setattr(WorkerEventWriter, "close", fail_close)
    assert asyncio.run(run_worker_with_events(run)) == 1
    captured = capsys.readouterr()
    _, diagnostic = _worker_stderr_failures(captured.err)
    assert "primary operation failed" in diagnostic
    assert "sender shutdown failed" in diagnostic
    assert_diagnostic(diagnostic)


def stages():
    repository = MagicMock()
    repository.role_assignments.read_role_assignment.return_value = {}
    checkpoints = MagicMock()
    checkpoints.publish_agent_session_checkpoint.return_value = None
    command = SimpleNamespace(fencing_token=1, invocation_id="invocation")
    admission = SimpleNamespace(assignment_lease=SimpleNamespace(fencing_token=1),
        assignment_lease_resource="assignment-lease", attempt={"assignment_id": "assignment", "attempt_id": "attempt"})
    pack = SimpleNamespace(continuation_output_path=None)
    harness = SimpleNamespace(pal_checkpoint_capable=False)
    session = SimpleNamespace(assignment={"assignment_id": "assignment"})
    return repository, checkpoints, command, admission, pack, harness, session


@pytest.mark.parametrize("directive", ["do_not_retry", "reconcile_first"])
def test_zero_exit_failed_terminal_preserves_diagnostic_and_retry_policy(directive):
    repository, checkpoints, command, admission, pack, harness, session = stages()
    payload = {"status": "failed", "summary": "short summary", "error": exception_report(failure()),
        "cleanup_error": {"error": "cleanup also failed"}, "error_kind": "provider_failure", "retry_directive": directive}
    terminal = {"event_kind": "terminal", "payload": payload}
    collected = CollectedRoleTerminal(terminal, payload)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(TerminalValidation(repository, checkpoints).execute(
            command, admission, pack, harness, collected, session))
    assert_diagnostic(str(caught.value))
    assert "cleanup also failed" in str(caught.value)
    assert isinstance(caught.value, PermanentEffectError) is (directive == "do_not_retry")
    if directive == "do_not_retry":
        repository.role_retries.queue_role_attempt_retry.assert_not_called()
    else:
        queued = repository.role_retries.queue_role_attempt_retry.call_args.kwargs
        assert queued["error_kind"] == "provider_failure"
        assert_diagnostic(queued["error_text"])


def test_lease_cleanup_failure_preserves_primary_terminal():
    repository, checkpoints, command, admission, pack, harness, session = stages()
    repository.leases.release_lease.side_effect = failure("lease storage failed")
    payload = {"status": "failed", "error": "original worker operation failed"}
    collected = CollectedRoleTerminal({"event_kind": "terminal", "payload": payload}, payload)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(TerminalValidation(repository, checkpoints).execute(
            command, admission, pack, harness, collected, session))
    text = exception_report(caught.value)
    assert "original worker operation failed" in text and "lease storage failed" in text
    assert_diagnostic(text)


@pytest.mark.parametrize("receipt", [False, True])
def test_process_failure_preserves_early_stderr_and_recorded_submission(receipt):
    repository, checkpoints, command, admission, pack, harness, session = stages()
    if receipt:
        repository.role_assignments.read_role_assignment.return_value = {"submission_artifact_ref": {"sha256": "recorded"}}
    terminal = {"event_kind": "terminal", "payload": {"status": "completed", "summary": "submitted"}}
    owner = SimpleNamespace(returncode=1, stderr=("first failure\n" + "x" * 5000 + "\nlast failure").encode())
    exited = ExitedRoleProcess([terminal], owner, exception_report(failure("shutdown failure")))
    call = ProcessResult(repository, checkpoints).execute(command, admission, pack,
        SimpleNamespace(pack=SimpleNamespace(workspace={})), harness, session, exited)
    if receipt:
        result = asyncio.run(call)
        assert result.terminal_payload["status"] == "completed"
        assert result.terminal_payload["process_error"]["submission_recorded"] is True
        text = result.terminal_payload["process_error"]["error"]
        repository.role_retries.queue_role_attempt_retry.assert_not_called()
    else:
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(call)
        text = str(caught.value)
    assert_diagnostic(text)
    assert "first failure" in text and "last failure" in text


def test_zero_exit_missing_terminal_retains_worker_error():
    repository, checkpoints, command, admission, pack, harness, session = stages()
    exited = ExitedRoleProcess([], SimpleNamespace(returncode=0, stderr=b""), exception_report(failure()))
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(ProcessResult(repository, checkpoints).execute(command, admission, pack,
            SimpleNamespace(pack=SimpleNamespace(workspace={})), harness, session, exited))
    assert_diagnostic(str(caught.value))
    assert_diagnostic(repository.role_retries.queue_role_attempt_retry.call_args.kwargs["error_text"])


def test_blocked_terminal_retains_full_reason_and_secondary_failure(tmp_path):
    runner = make_runner(tmp_path, output=tmp_path / "checkpoint")
    runner.components.status.classify_blocker("completion_gate_stalled")
    full = "x" * 1000 + " root cause at end"
    payload = runner.components.results.terminal_payload("blocked", full)
    payload["cleanup_error"] = {"error": "cleanup failed"}
    _, details, directive = _worker_terminal_failure([{"event_kind": "terminal", "payload": payload}])
    assert len(payload["summary"]) <= 500
    assert full in details and "cleanup failed" in details
    assert directive == "do_not_retry"


@pytest.mark.parametrize("completed", [False, True])
def test_provider_metadata_survives_worker_retry_or_evidence_fallback(tmp_path, monkeypatch, completed):
    runner = make_runner(tmp_path, output=tmp_path / "checkpoint")
    rounds = runner.components.llm_rounds
    monkeypatch.setattr(rounds.completion, "completion_evidence_present", lambda: completed)
    monkeypatch.setattr(rounds.completion, "artifact_completion_evidence_present", lambda: completed)
    monkeypatch.setattr(rounds.completion, "completion_evidence_fallback_text", lambda text: "durable submission exists")
    response = generation_result_from_values(text="provider failed", finish_reason="error").response
    response = replace(response, message=replace(response.message, metadata={"provider_error": exception_report(failure())}))
    outcome = SimpleNamespace(response=response, finish_reason="error", text="provider failed")
    state = SimpleNamespace(llm_round_count=1)
    call = rounds.postprocess_bunshin_llm_round(state, EffectResult(status="ok", payload=outcome))
    if completed:
        result = asyncio.run(call)
        assert result.payload.finish_reason == "stop"
        payload = runner.components.results.terminal_payload("completed", "submission exists")
        assert_diagnostic(json.dumps(payload["diagnostics"]))
    else:
        with pytest.raises(BunshinLLMRetryableError) as caught:
            asyncio.run(call)
        assert_diagnostic(str(caught.value))


def test_failure_artifact_preserves_nested_cause():
    service = MagicMock()
    service.repository.snapshots.read_snapshot.return_value = SimpleNamespace(
        aggregate_type=AggregateType.WORKFLOW, aggregate_id="w", workflow_id="w", state="RUNNING")
    service.repository.transitions.legal_actions.return_value = ["ENTER_TRIAGE"]
    outbox = BunshinOutboxProcessor(service)
    outbox._failed_effect_triage_action({"effect_key": "e", "aggregate_type": "workflow", "aggregate_id": "w"}, failure())
    assert_diagnostic(service.artifacts.put_json.call_args.args[0]["error"])


def test_cancelled_workflow_retains_failed_effect_evidence(monkeypatch):
    service = MagicMock()
    service.artifacts.put_json.return_value.to_dict.return_value = {"sha256": "failure-artifact"}
    outbox = BunshinOutboxProcessor(service,
        semantic_effects=SimpleNamespace(execute_semantic_effect=AsyncMock(side_effect=failure())))
    monkeypatch.setattr(outbox, "_effect_snapshot", lambda effect: SimpleNamespace(state="CANCELLED"))
    for name in ("_reconcile_control_requests", "_reconcile_replan_collections", "_publish_terminal_workflow_if_any"):
        monkeypatch.setattr(outbox, name, MagicMock())
    monkeypatch.setattr(outbox, "_heartbeat", AsyncMock())
    assert asyncio.run(outbox._process_effect({"effect_id": "e", "effect_type": "test_role", "workflow_id": "w"})) == "completed"
    stored = service.artifacts.put_json.call_args.args[0]
    assert stored["status"] == "superseded_after_failure"
    assert_diagnostic(stored["error"])
    assert service.repository.outbox_results.complete_outbox_effect.call_args.kwargs["result_artifact_ref"] == {"sha256": "failure-artifact"}


@pytest.fixture
def control(monkeypatch):
    core = PalCore()
    monkeypatch.setattr(core, "_deliver_control_delivery_async", AsyncMock(return_value=True))
    route = ControlRoute("test", "socket", control_scope_key="scope")
    return core, route


def reply(core):
    return core._deliver_control_delivery_async.await_args.args[0].text


def test_log_control_reports_partial_update_and_full_cause(control):
    core, route = control
    good, bad = MagicMock(), MagicMock()
    bad.set_prompt_log_enabled.side_effect = failure("dependent update failed")
    core.context.port_registry["good"] = good
    core.context.port_registry["bad"] = bad
    asyncio.run(core.handle_control_action_async(ControlAction("set_log", "runtime", route=route,
        args={"prompt_log_enabled": True})))
    assert core.state.prompt_log_enabled is True
    good.set_prompt_log_enabled.assert_called_once_with(True)
    assert "did not confirm" in reply(core)
    assert "bad" in reply(core)
    assert_diagnostic(reply(core))
    assert_diagnostic(core.state.diagnostics[-1]["error"])


def test_button_tool_failure_delivers_full_result_and_effect(control):
    core, route = control
    result = CapabilityResult(status="error", text="x" * 500 + " original reason at end", llm_text="short failure",
        structured={"diagnostic": "hidden detail"}, effect_receipt=EffectReceipt(outcome=EffectOutcome.UNKNOWN),
        recovery_hint="inspect state before retry")
    core.context.execution_runtime = SimpleNamespace(execute_async=AsyncMock(return_value=result))
    asyncio.run(core.handle_control_action_async(ControlAction("invoke_capability", "runtime", target_id="probe", route=route,
        args={"interaction_origin": "button", "interaction_id": "i", "interaction_kind": "control_panel"})))
    text = reply(core)
    assert "original reason at end" in text and "hidden detail" in text
    assert "unknown" in text and "inspect state before retry" in text


@pytest.mark.parametrize("success", [False, True])
def test_manual_compaction_delivers_log_reference_and_retains_diagnostics(control, monkeypatch, success):
    core, route = control
    memory = MemoryService()
    monkeypatch.setattr(memory, "settled_transcripts", lambda: [["history"]])
    core.context.port_registry["memory:memory"] = memory
    result = CompactionRunResult(status="compacted" if success else "commit_failed", attempts=1,
        diagnostic_reference="compact run=control-test",
        failure_details=() if success else (exception_report(failure()),),
        memory_result=MemoryCompactResult(summary="new summary", metadata={"post_commit_detail": exception_report(failure("cleanup failed"))}) if success else None)
    monkeypatch.setattr(core, "_run_control_compaction_async", AsyncMock(return_value=result))
    monkeypatch.setattr(core, "_start_next_queued_turn_async", AsyncMock())
    monkeypatch.setattr(core, "_deliver_compact_candidates_async", AsyncMock())
    asyncio.run(core.handle_control_action_async(ControlAction("compact_memory", "memory", route=route)))
    text = reply(core)
    assert "see service logs" in text
    assert result.diagnostic_reference in text
    assert "storage root cause" not in text
    assert "Traceback" not in text
    assert "hidden-secret" not in text
    assert_diagnostic(result.diagnostic_details)
    assert "Traceback" in result.diagnostic_details
    assert "memory state was left unchanged" not in text
    if success:
        assert "Context compacted" in text
        assert "committed; follow-up failed" in result.diagnostic_details
    else:
        assert "Compaction did not complete (commit_failed)" in text
    assert not core._compaction_gate_active()
