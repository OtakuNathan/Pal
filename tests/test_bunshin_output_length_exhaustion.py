"""An exhausted local length recovery must not start another worker shell."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pal.bunshin.runner import BunshinRunner
from pal.bunshin.runner_components.models import BunshinAgentLoopState
from pal.bunshin.runner_components.prompt_values import DEFAULT_BUNSHIN_OUTPUT_LENGTH_RECOVERY_ROUNDS
from pal.bunshin.background_assignments import BackgroundAssignments
from pal.bunshin.contracts import PermanentEffectError
from pal.bunshin.semantic_orchestration.assignment_execution import AssignmentExecution
from pal.bunshin.semantic_orchestration.attempt_terminal_validation import TerminalValidation
from pal.core.turns import EffectResult
from pal.llm import generation_result_from_values
from pal.shared import BunshinInvocationPack, RuntimeStatus


async def noop(*args, **kwargs):
    return None


def exhausted_terminal(root):
    events = []

    async def record(event):
        events.append(event)

    runner = BunshinRunner(
        runtime_root=root,
        pack=BunshinInvocationPack(invocation_id="architect-length", instruction="Author a contract"),
        bunshin_id="architect-length", run_id="length-attempt", write_event=record, read_decision=noop,
    )
    state = BunshinAgentLoopState(
        execution_runtime=SimpleNamespace(), memory_service=SimpleNamespace(),
        memory_candidate_sink=SimpleNamespace(),
    )

    async def run_loop(bundle, **kwargs):
        # Each synthetic provider response exercises the real length handler;
        # no model, process, or live runtime is used.
        for index in range(DEFAULT_BUNSHIN_OUTPUT_LENGTH_RECOVERY_ROUNDS + 1):
            state.llm_round_count += 1
            await runner.components.llm_rounds.postprocess_bunshin_llm_round(
                state,
                EffectResult(status=RuntimeStatus.OK, payload=generation_result_from_values(
                    text="partial", finish_reason="length",
                )),
            )
            if index < DEFAULT_BUNSHIN_OUTPUT_LENGTH_RECOVERY_ROUNDS:
                assert not runner.components.status.blocked_summary
        return "partial"

    runner.components.agent_session.run_agent_loop = run_loop
    assert asyncio.run(runner.components.invocation.run_invocation(None)) == 0
    assert state.output_length_recovery_count == DEFAULT_BUNSHIN_OUTPUT_LENGTH_RECOVERY_ROUNDS
    assert state.pending_output_length_recovery_note == ""
    assert state.llm_round_count == 0
    terminal = next(event["payload"] for event in events if event["event_kind"] == "terminal")
    assert terminal["status"] == "blocked"
    assert terminal["blocker_kind"] == "output_length_recovery_exhausted"
    assert "automatic retries have stopped" in terminal["summary"]
    assert "smaller file chunks" in terminal["summary"]
    return terminal


@pytest.mark.parametrize("blocker,expected_calls", [
    ("output_length_recovery_exhausted", 1),
    ("completion_gate_stalled", 1),
    ("other_blocker", 3),
    ("", 3),
])
def test_blocked_terminal_reaches_supervisor_without_rebinding_exhausted_recovery(
    tmp_path, monkeypatch, blocker, expected_calls,
):
    terminal = exhausted_terminal(tmp_path)
    terminal["blocker_kind"] = blocker
    assignment = {"assignment_id": "assignment", "state": "running"}
    attempts = []
    repository = MagicMock()
    repository.role_assignments.read_role_assignment.side_effect = lambda _: dict(assignment)
    repository.role_attempts.list_role_attempts.side_effect = lambda _: list(attempts)

    def queue_retry(**kwargs):
        assignment["state"] = "retry_queued"
        attempts.append({"status": "lost", "error_kind": kwargs["error_kind"]})

    repository.role_retries.queue_role_attempt_retry.side_effect = queue_retry
    checkpoints = MagicMock()
    checkpoints.publish_agent_session_checkpoint.return_value = None
    validator = TerminalValidation(repository, checkpoints)
    background = BackgroundAssignments()
    background.bind("architect-effect", "assignment")
    failures, identity, retries, leases = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    identity.role_assignment_disposition.return_value = ""
    failures.settle_background_role_failure.return_value = {"status": "triage_required"}
    supervisor = AssignmentExecution(failures, identity, retries, leases, background, repository)
    command = SimpleNamespace(fencing_token=1, invocation_id="architect-session")
    admission = SimpleNamespace(
        assignment_lease=SimpleNamespace(fencing_token=1),
        assignment_lease_resource="attempt-lease", attempt={"attempt_id": "attempt"},
    )
    calls = 0

    async def role_runner(effect):
        nonlocal calls
        calls += 1
        assignment["state"] = "running"
        await validator.execute(
            command, admission, SimpleNamespace(continuation_output_path=None),
            SimpleNamespace(pal_checkpoint_capable=False),
            SimpleNamespace(terminal_payload=terminal), SimpleNamespace(assignment=assignment),
        )

    monkeypatch.setattr("pal.bunshin.semantic_orchestration.assignment_execution.asyncio.sleep", AsyncMock())
    result = asyncio.run(supervisor.background_worker_loop(
        {"effect_key": "architect-effect", "effect_type": "run_architecture_stage"}, role_runner,
    ))
    assert result == {"status": "triage_required"}
    assert calls == expected_calls
    failures.settle_background_role_failure.assert_called_once()
    settled = failures.settle_background_role_failure.call_args
    assert isinstance(settled.args[2], PermanentEffectError) is (expected_calls == 1)
    assert settled.kwargs["exhausted"] is (expected_calls == 3)
    assert repository.role_retries.queue_role_attempt_retry.call_count == (0 if expected_calls == 1 else 3)
    retries.queue_active_assignment_retry.assert_not_called()
