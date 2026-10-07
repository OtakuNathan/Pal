from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pal.bunshin.failure_diagnostics import append_failure_diagnostic, exception_diagnostic
from pal.bunshin.runner import BunshinRunner
from pal.bunshin.v2 import worker_main
from pal.bunshin.v2.contracts import AggregateType, PermanentEffectError
from pal.bunshin.v2.orchestration import BunshinV2OutboxProcessor
from pal.bunshin.v2.semantic_orchestration.attempt_process_result import ProcessResult
from pal.bunshin.v2.semantic_orchestration.attempt_worker_execution import WorkerExecution


def _failure(depth=0):
    secret_local = "SECRET_LOCAL_MUST_NOT_APPEAR"
    if depth:
        return _failure(depth - 1)
    raise RuntimeError("SECRET_MESSAGE_MUST_NOT_BE_AMPLIFIED")


def _diagnostic(depth=0):
    try:
        _failure(depth)
    except RuntimeError as exc:
        return exception_diagnostic(exc)


def test_metadata_contains_no_values_source_or_absolute_paths():
    diagnostic = _diagnostic(20)
    encoded = json.dumps(diagnostic)
    assert diagnostic["error_type"] == "RuntimeError"
    assert len(diagnostic["frames"]) == 12
    assert "SECRET" not in encoded
    assert "raise" not in encoded
    assert all(set(frame) == {"file", "function", "line"} for frame in diagnostic["frames"])
    assert all(frame["file"] == "test_bunshin_failure_diagnostics.py" for frame in diagnostic["frames"])
    assert all(frame["function"] == "_failure" for frame in diagnostic["frames"])


def test_wire_metadata_is_bounded_and_missing_field_is_compatible():
    assert append_failure_diagnostic("old error", None) == "old error"
    assert append_failure_diagnostic("old error", {"frames": []}) == "old error"
    summary = append_failure_diagnostic("old error", {
        "error_type": "RuntimeError", "secret": "SECRET",
        "frames": [{"file": "/SECRET/fixture.py", "function": "f" * 200,
                    "line": 4, "locals": {"token": "SECRET"}}] * 50,
    })
    assert summary.startswith("old error\nworker_diagnostic=")
    assert "SECRET" not in summary
    parsed = json.loads(summary.split("worker_diagnostic=", 1)[1])
    assert len(parsed["frames"]) == 12
    assert all(len(f["function"]) == 96 for f in parsed["frames"])
    assert append_failure_diagnostic("old error", {"error_type": [], "frames": {}}) == "old error"


def test_worker_main_reports_pre_runner_exception(monkeypatch, capsys):
    async def fail(*args):
        _failure()
    monkeypatch.setattr(worker_main, "_run", fail)
    assert worker_main.main(["--runtime-root", "/unused", "--pack-json", "/unused",
                             "--bunshin-id", "b", "--run-id", "r"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "worker_error"
    assert payload["error"] == "RuntimeError: SECRET_MESSAGE_MUST_NOT_BE_AMPLIFIED"
    assert "SECRET" not in json.dumps(payload["failure_diagnostic"])
    assert payload["failure_diagnostic"]["frames"][-1]["function"] == "_failure"


def test_runner_reports_runtime_build_failure_before_accepted(monkeypatch):
    from pal.bunshin import runner as runner_module
    runner = object.__new__(BunshinRunner)
    runner.pack = SimpleNamespace(goal="", instruction="", allowed_capabilities=[], metadata={}, workspace={})
    runner.runtime_root = None
    runner.run_id = "r"
    runner.runtime_bundle = None
    runner.components = MagicMock()
    runner.components.invocation.close_execution_work = AsyncMock()
    runner.components.reporter.emit = AsyncMock()
    monkeypatch.setattr(runner_module, "build_slim_bunshin_runtime", lambda *a, **kw: _failure())
    assert asyncio.run(runner.run()) == 1
    calls = runner.components.reporter.emit.await_args_list
    assert len(calls) == 1 and calls[0].args[0] == "terminal"
    payload = calls[0].args[1]
    assert payload["failure_diagnostic"]["error_type"] == "RuntimeError"
    assert "SECRET" not in json.dumps(payload["failure_diagnostic"])


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("permanent", [False, True])
def test_real_parse_and_process_result_preserve_metadata_in_failure_artifact(monkeypatch, terminal, permanent):
    from pal.bunshin.v2.semantic_orchestration import attempt_worker_execution as execution_module
    diagnostic = _diagnostic()
    payload = {"error": "original error", "failure_diagnostic": diagnostic}
    if terminal:
        payload.update(status="failed", retry_directive="do_not_retry" if permanent else "reconcile_first")
        item = {"kind": "event", "event": {"event_kind": "terminal", "payload": payload}}
    else:
        item = {"kind": "worker_error", **payload}

    async def lines():
        yield json.dumps(item).encode()
    owner = SimpleNamespace(stdout_lines=lines, wait=AsyncMock(), returncode=1, stderr=b"")
    monkeypatch.setattr(execution_module, "WorkerProcessOwner", lambda **kw: owner)
    @asynccontextmanager
    async def shell(*args, **kwargs):
        yield
    repository = MagicMock()
    repository.role_assignments.read_role_assignment.return_value = {}
    supervisor = SimpleNamespace(process_shell=shell)
    execution = WorkerExecution(MagicMock(), None, None, repository, MagicMock(), supervisor, None, MagicMock())
    command = SimpleNamespace(effect={"effect_key": "diagnostic-effect"}, fencing_token=1, invocation_id="i", lease_resource="l", snapshot=MagicMock())
    admission = SimpleNamespace(assignment_lease=SimpleNamespace(fencing_token=1),
                                assignment_lease_resource="a", attempt={"assignment_id": "assignment-a", "attempt_id": "a"})
    publication = SimpleNamespace(argv=[], env={}, pack=SimpleNamespace(workspace={}))
    exited = asyncio.run(execution.execute(command, admission, publication,
                                            SimpleNamespace(role="coder", run_id="r")))
    checkpoints = MagicMock()
    checkpoints.publish_agent_session_checkpoint.return_value = None
    processor = ProcessResult(repository, checkpoints)
    with pytest.raises((RuntimeError, PermanentEffectError)) as caught:
        asyncio.run(processor.execute(command, admission,
                    SimpleNamespace(continuation_output_path=None), publication,
                    SimpleNamespace(pal_checkpoint_capable=False),
                    SimpleNamespace(assignment={"assignment_id": "a"}), exited))
    error = caught.value
    assert (isinstance(error, PermanentEffectError)) == (terminal and permanent)
    assert "worker_diagnostic=" in str(error)
    assert "SECRET" not in str(error)
    if not (terminal and permanent):
        assert "worker_diagnostic=" in repository.role_retries.queue_role_attempt_retry.call_args.kwargs["error_text"]
    service = MagicMock()
    snapshot = SimpleNamespace(aggregate_type=AggregateType.WORKFLOW, aggregate_id="w",
                               workflow_id="w", state="RUNNING")
    service.repository.snapshots.read_snapshot.return_value = snapshot
    service.repository.transitions.legal_actions.return_value = ["ENTER_TRIAGE"]
    outbox = BunshinV2OutboxProcessor(service)
    action = outbox._failed_effect_triage_action({"effect_key": "e", "aggregate_type": "workflow",
                                               "aggregate_id": "w"}, error)
    stored = service.artifacts.put_json.call_args
    assert stored.kwargs["artifact_type"] == "EffectFailureArtifact"
    assert "worker_diagnostic=" in stored.args[0]["error"]
    assert "_failure" in stored.args[0]["error"]
    assert "SECRET" not in json.dumps(stored.args[0])
    assert action.payload["failure_artifact_ref"] is not None


def test_background_role_failure_artifact_retains_diagnostics():
    from pal.bunshin.v2.semantic_orchestration.assignment_failures import AssignmentFailures
    artifacts = MagicMock()
    repository = MagicMock()
    assignment = {"assignment_id": "a", "role": "coder", "state": "settled",
                  "submission_payload_hash": "hash"}
    repository.role_assignments.read_role_assignment.return_value = assignment
    repository.role_attempts.list_role_attempts.return_value = []
    repository.transitions.legal_actions.return_value = []
    reads = MagicMock()
    reads.effect_snapshot.return_value = SimpleNamespace(state="TRIAGE_REQUIRED", aggregate_type=AggregateType.WORKFLOW)
    failures = AssignmentFailures(reads, MagicMock(), artifacts, repository)
    failures.require_cycle_triage = MagicMock()
    error = RuntimeError(append_failure_diagnostic("original error", _diagnostic()))
    failures.settle_background_role_failure({}, assignment, error, exhausted=True)
    stored = artifacts.put_json.call_args
    assert stored.kwargs["artifact_type"] == "RoleAssignmentFailureArtifact"
    assert "worker_diagnostic=" in stored.args[0]["error"]
    assert "_failure" in stored.args[0]["error"]
    assert "SECRET" not in json.dumps(stored.args[0])


def test_malformed_wire_frames_are_ignored_and_labels_are_safe():
    frames = [None, "bad", {"file": "x.py", "line": True},
              {"file": "x.py", "line": 1.5}, {"file": "x.py", "line": 10**9},
              {"file": "x.py", "line": -1}, {"file": [], "line": 2},
              {"file": "C:\\private\\file\nname.py", "function": "call\nname", "line": 2},
              {"file": "x.py", "function": {"secret": "SECRET"}, "line": 3}]
    summary = append_failure_diagnostic("old", {"error_type": "Some\nError", "frames": frames})
    parsed = json.loads(summary.split("worker_diagnostic=", 1)[1])
    assert parsed == {"error_type": "Some_Error", "frames": [
        {"file": "file_name.py", "function": "call_name", "line": 2},
        {"file": "x.py", "function": "", "line": 3},
    ]}
    assert "SECRET" not in summary


def test_checkpoint_classification_is_unchanged_with_metadata():
    from pal.bunshin.v2.semantic_orchestration.worker_results import _worker_terminal_failure
    kind, details, directive = _worker_terminal_failure([{
        "event_kind": "terminal", "payload": {
            "status": "failed", "error_kind": "invalid_agent_session_checkpoint",
            "error_type": "AgentSessionCheckpointError", "retry_directive": "do_not_retry",
            "error": "continuation does not contain L1", "failure_diagnostic": _diagnostic(),
        },
    }])
    assert kind == "invalid_agent_session_checkpoint"
    assert directive == "do_not_retry"
    assert details.startswith("continuation does not contain L1\nworker_diagnostic=")


@pytest.mark.parametrize("terminal_payload,has_receipt,permanent", [
    ({"status": "blocked", "blocker_kind": "output_length_recovery_exhausted",
      "summary": "bounded output recovery exhausted; narrow the next file edit"}, False, True),
    ({"status": "blocked", "blocker_kind": "completion_gate_stalled",
      "summary": "required primary artifact absent after submit feedback"}, False, True),
    ({"status": "blocked", "blocker_kind": "other_blocker",
      "summary": "ordinary blocked outcome"}, False, False),
    ({"status": "failed", "error_kind": "runner_failure",
      "retry_directive": "reconcile_first", "summary": "retryable failure"}, False, False),
    ({"status": "failed", "error_kind": "invalid_agent_session_checkpoint",
      "retry_directive": "do_not_retry", "summary": "invalid checkpoint"}, False, True),
    ({"status": "completed", "summary": "durable submission recorded"}, True, False),
])
def test_nonzero_cleanup_preserves_terminal_policy_and_diagnostics(
    monkeypatch, terminal_payload, has_receipt, permanent,
):
    """Parse real wire messages, then classify the nonzero process result."""
    from pal.bunshin.v2.semantic_orchestration import attempt_worker_execution as execution_module

    cleanup = "ExceptionGroup: bunshin runtime shutdown failed (1 sub-exception)"
    messages = [
        {"kind": "event", "event": {"event_kind": "terminal", "payload": terminal_payload}},
        {"kind": "worker_error", "error": cleanup, "failure_diagnostic": _diagnostic()},
    ]

    async def lines():
        for message in messages:
            yield json.dumps(message).encode()

    owner = SimpleNamespace(stdout_lines=lines, wait=AsyncMock(), returncode=1, stderr=b"")
    monkeypatch.setattr(execution_module, "WorkerProcessOwner", lambda **kw: owner)

    @asynccontextmanager
    async def shell(*args, **kwargs):
        yield

    repository = MagicMock()
    repository.role_assignments.read_role_assignment.return_value = {
        "submission_artifact_ref": {"sha256": "receipt"} if has_receipt else {},
    }
    execution = WorkerExecution(
        MagicMock(), None, None, repository, MagicMock(),
        SimpleNamespace(process_shell=shell), None, MagicMock(),
    )
    command = SimpleNamespace(effect={"effect_key": "diagnostic-effect"}, fencing_token=1, invocation_id="i", lease_resource="l", snapshot=MagicMock())
    admission = SimpleNamespace(
        assignment_lease=SimpleNamespace(fencing_token=1),
        assignment_lease_resource="a", attempt={"assignment_id": "assignment-a", "attempt_id": "a"},
    )
    publication = SimpleNamespace(argv=[], env={}, pack=SimpleNamespace(workspace={}))
    exited = asyncio.run(execution.execute(
        command, admission, publication, SimpleNamespace(role="verifier", run_id="r"),
    ))
    checkpoints = MagicMock()
    checkpoints.publish_agent_session_checkpoint.return_value = None
    processor = ProcessResult(repository, checkpoints)

    async def process():
        return await processor.execute(
            command, admission, SimpleNamespace(continuation_output_path=None),
            publication, SimpleNamespace(pal_checkpoint_capable=False),
            SimpleNamespace(assignment={"assignment_id": "a"}), exited,
        )

    if has_receipt:
        result = asyncio.run(process())
        assert result.terminal_payload == terminal_payload
        repository.role_retries.queue_role_attempt_retry.assert_not_called()
        return
    with pytest.raises((RuntimeError, PermanentEffectError)) as caught:
        asyncio.run(process())
    assert isinstance(caught.value, PermanentEffectError) is permanent
    assert cleanup in str(caught.value)
    assert "worker_diagnostic=" in str(caught.value)
    if terminal_payload["status"] == "failed" or permanent:
        assert terminal_payload["summary"] in str(caught.value)
    if permanent:
        repository.role_retries.queue_role_attempt_retry.assert_not_called()
    else:
        repository.role_retries.queue_role_attempt_retry.assert_called_once()
