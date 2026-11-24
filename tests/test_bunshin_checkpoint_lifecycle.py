"""Persisted startup admission distinguishes never-created from lost continuation."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from pal.bunshin.checkpoint import (
    AgentSessionCheckpointError, LogicalCoroutineCheckpointStore, seal_agent_session_checkpoint,
)
from pal.bunshin.contracts import AggregateType, StaleFencingToken
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.role_gateway import RoleAssignmentGateway
from pal.bunshin.role_protocol import RoleSessionAction
from pal.bunshin.semantic_orchestration.role_checkpoints import RoleCheckpoints
from pal.bunshin.service import BunshinWorkflowService
from tests import test_bunshin_role_protocol as protocol_fixture
from tests.test_bunshin_completion_resume import make_bundle, make_runner


@pytest.fixture
def role():
    fixture = protocol_fixture.BunshinRoleProtocolTests()
    fixture.setUp()
    return fixture


def checkpoints(role):
    return RoleCheckpoints(role.artifacts, None, role.repository, role.runtime_root)


def prepare(role, attempt="next-attempt"):
    return checkpoints(role).prepare_agent_session_attempt(
        session_id="session-router", attempt_id=attempt,
    )


def status(role):
    return role.repository.role_sessions.read_role_session("session-router")["status"]


def restart(role):
    role.repository = BunshinRepository(role.runtime_root)
    role.repository.database.ensure_schema()


def launch(role):
    assignment = role.repository.role_assignments.create_role_assignment(role.request())
    attempt, fence = role.start_attempt(assignment["assignment_id"])
    token = role.repository.role_access.issue_role_attempt_access_token(
        assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
        fencing_token=fence,
    )
    return assignment, attempt, fence, token


def checkpoint(role, fence=1, sequence=1):
    identity = {
        "logical_coroutine_id": "session-router", "workflow_id": "workflow-router",
        "stage_key": "module:router:implementation", "sequence": sequence,
        "producer_fencing_token": fence, "runtime_spec_hash": "test-spec",
    }
    return seal_agent_session_checkpoint(role.runtime_root, {
        **identity, "coroutine_state": {"llm_round_count": 0, "tool_call_count": 0},
        "runtime_snapshot": {"schema_version": "1", **identity, "modules": {}},
    })


def write_initial(role, attempt, fence):
    restore, output = prepare(role, attempt["attempt_id"])
    assert restore is None
    payload = checkpoint(role, fence)
    output.write_text(json.dumps(payload))
    return output, payload


def acknowledge(role, token):
    gateway = RoleAssignmentGateway(BunshinWorkflowService(role.runtime_root))
    return gateway.call("checkpoint_initialize", {"access_token": token})


def park(role):
    with role.repository.database.write_connection() as connection:
        role.repository.role_sessions.transition_role_session_locked(
            connection, "session-router", RoleSessionAction.PARK, now="test",
        )


def test_initial_launch_retries_and_exhaustion_remain_uninitialized_after_restart(role):
    assignment = role.repository.role_assignments.create_role_assignment(role.request())
    for _ in range(3):
        attempt, fence = role.start_attempt(assignment["assignment_id"])
        assert prepare(role, attempt["attempt_id"])[0] is None
        assert status(role) == "uninitialized"
        role.repository.role_retries.queue_role_attempt_retry(
            assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
            error_kind="worker_process_failed", error_text="bwrap: prelaunch failed",
        )
        role.repository.leases.release_lease(
            f"assignment:{assignment['assignment_id']}", attempt["attempt_id"], fence,
        )
        restart(role)
        assert status(role) == "uninitialized"
    failure = role.artifacts.put_json({"failure": "startup exhausted"}, artifact_type="RoleFailureArtifact")
    receipt = role.repository.role_retries.record_role_failure_result(
        assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
        error_kind="worker_process_failed", error_text="bwrap: prelaunch failed",
        failure_artifact_ref=failure.to_dict(), payload_hash="failure-hash",
        settlement_action={"action_type": "ROLE_FAILED"},
    )
    role.repository.role_submissions.settle_role_assignment(
        assignment_id=assignment["assignment_id"], submission_payload_hash=receipt.payload_hash,
    )
    restart(role)
    assert status(role) == "uninitialized"
    assert prepare(role)[0] is None
    # A later authorized retry is a new assignment on the same logical role.
    resumed = role.repository.role_assignments.create_role_assignment(role.request(key="retry-after-repair"))
    role.start_attempt(resumed["assignment_id"])
    assert prepare(role, "repaired-attempt")[0] is None


def test_cancellation_before_first_checkpoint_keeps_fresh_admission(role):
    launch(role)
    role.repository.role_cancellation.cancel_role_assignments(
        workflow_id="workflow-router", aggregate_type=AggregateType.DAG_NODE_RUN,
        aggregate_id="node-router", reason="paused before launch",
    )
    restart(role)
    assert status(role) == "uninitialized"
    assert prepare(role)[0] is None


@pytest.mark.parametrize("state", ["active", "suspended"])
def test_legacy_established_session_missing_checkpoint_fails_closed(role, state):
    with role.repository.database.write_connection() as connection:
        connection.execute("UPDATE bunshin_role_sessions SET status = ?", (state,))
    restart(role)
    with pytest.raises(AgentSessionCheckpointError, match="requires.*missing"):
        prepare(role)
    assert status(role) == state


@pytest.mark.parametrize("defect", ["json", "sequence", "directory", "ciphertext"])
@pytest.mark.parametrize("initialized", [False, True])
def test_missing_or_corrupt_checkpoint_never_authorizes_fresh_start(role, defect, initialized):
    _assignment, attempt, fence, token = launch(role)
    _output, payload = write_initial(role, attempt, fence)
    if initialized:
        acknowledge(role, token)
        park(role)
    path = LogicalCoroutineCheckpointStore(role.runtime_root).current_path("session-router")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    if defect == "directory":
        path.mkdir()
    elif defect == "json":
        path.write_text("{")
    else:
        payload["sequence" if defect == "sequence" else "ciphertext"] = "invalid" if defect == "sequence" else ""
        path.write_text(json.dumps(payload))
    restart(role)
    with pytest.raises(AgentSessionCheckpointError):
        prepare(role)


@pytest.mark.parametrize("suspend", [False, True])
def test_acknowledged_checkpoint_loss_stays_fail_closed_after_retry(role, suspend):
    assignment, attempt, fence, token = launch(role)
    write_initial(role, attempt, fence)
    acknowledge(role, token)
    LogicalCoroutineCheckpointStore(role.runtime_root).delete("session-router")
    role.repository.role_retries.queue_role_attempt_retry(
        assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
        error_kind="worker_lost", error_text="killed after work",
    )
    if suspend:
        park(role)
    restart(role)
    assert status(role) == ("suspended" if suspend else "active")
    with pytest.raises(AgentSessionCheckpointError, match="requires.*missing"):
        prepare(role)


def test_file_publication_survives_database_rollback_and_recovers_as_resume(role):
    _assignment, attempt, fence, token = launch(role)
    _output, payload = write_initial(role, attempt, fence)
    with patch("pal.bunshin.storage.role_sessions.RoleSessionsStore.transition_role_session_locked", side_effect=RuntimeError("database commit lost")):
        with pytest.raises(RuntimeError, match="database commit lost"):
            acknowledge(role, token)
    restart(role)
    assert status(role) == "uninitialized"
    assert role.repository.role_maintenance.reconcile_role_session_checkpoints() == ()
    restore, _output = prepare(role, "after-db-rollback")
    assert json.loads(restore.read_text()) == payload
    assert status(role) == "active"


def test_ack_replay_is_idempotent_but_same_sequence_mutation_is_rejected(role):
    _assignment, attempt, fence, token = launch(role)
    output, payload = write_initial(role, attempt, fence)
    assert acknowledge(role, token) == {"sequence": 1}
    # Lost response after COMMIT: the same first file remains safe to ACK.
    restart(role)
    assert acknowledge(role, token) == {"sequence": 1}
    assert checkpoints(role).publish_agent_session_checkpoint("session-router", fence, output) == payload
    assert not output.exists()
    restore, _ = prepare(role)
    assert json.loads(restore.read_text()) == payload
    output.write_text(json.dumps({**payload, "ciphertext": "different-ciphertext"}))
    with pytest.raises(AgentSessionCheckpointError, match="sequence"):
        acknowledge(role, token)


def test_stale_attempt_cannot_initialize_session(role):
    assignment, attempt, fence, token = launch(role)
    write_initial(role, attempt, fence)
    role.repository.role_retries.queue_role_attempt_retry(
        assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
        error_kind="worker_lost", error_text="old process lost",
    )
    role.repository.leases.release_lease(f"assignment:{assignment['assignment_id']}", attempt["attempt_id"], fence)
    role.start_attempt(assignment["assignment_id"])
    with pytest.raises(StaleFencingToken):
        acknowledge(role, token)
    assert status(role) == "uninitialized"
    assert LogicalCoroutineCheckpointStore(role.runtime_root).read("session-router") is None


def test_successful_uncheckpointed_submission_requires_continuation(role):
    assignment, attempt, fence, _token = launch(role)
    role.repository.role_submissions.record_role_submission(
        assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
        fencing_token=fence, artifact_ref=role.submission_ref.to_dict(), payload_hash="worked",
        settlement_action={"action_type": "SUBMIT_CANDIDATE"},
    )
    role.repository.role_submissions.settle_role_assignment(
        assignment_id=assignment["assignment_id"], submission_payload_hash="worked",
    )
    restart(role)
    with pytest.raises(AgentSessionCheckpointError, match="requires.*missing"):
        prepare(role)


@pytest.mark.parametrize("ack_result", ["ok", "lost", "wrong_sequence"])
def test_real_loop_cannot_work_before_durable_checkpoint_ack(role, ack_result):
    async def scenario():
        _assignment, attempt, fence, token = launch(role)
        _restore, output = prepare(role, attempt["attempt_id"])
        bundle = make_bundle()
        runner = make_runner(role.runtime_root, output=output, token=fence)
        runner.pack.metadata["agent_session"].update({
            "session_id": "session-router", "workflow_id": "workflow-router",
            "stage_key": "module:router:implementation",
            "checkpoint_initialization_required": True,
        })
        published, release_ack = asyncio.Event(), asyncio.Event()

        class Client:
            calls = 0

            async def request(self, method):
                self.calls += 1
                assert method == "checkpoint_initialize"
                result = acknowledge(role, token)
                published.set()
                await release_ack.wait()
                if ack_result == "lost":
                    raise RuntimeError("initial ACK lost")
                return result if ack_result == "ok" else {"sequence": 999}

        client = Client()
        try:
            with patch("pal.bunshin.runner_components.session_checkpoints.role_gateway_client_from_env", return_value=client):
                task = asyncio.create_task(runner.components.agent_session.run_agent_loop(bundle))
                await asyncio.wait_for(published.wait(), timeout=5)
                assert status(role) == "active"
                assert bundle.llm_runtime.calls == 0
                assert runner.components.tool_session.observed_tool_call_count == 0
                release_ack.set()
                if ack_result != "ok":
                    expected = RuntimeError if ack_result == "lost" else AgentSessionCheckpointError
                    with pytest.raises(expected, match="ACK lost|wrong sequence"):
                        await task
                    assert bundle.llm_runtime.calls == 0
                else:
                    await task
                    assert bundle.llm_runtime.calls == 1
                assert client.calls == 1
            # A process restart after either successful or lost ACK restores.
            restart(role)
            assert prepare(role, "process-restart")[0] is not None
        finally:
            bundle.execution_runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["no_gateway", "unsafe"])
def test_first_loop_requires_ack_and_restart_safe_boundary(role, failure):
    async def scenario():
        bundle = make_bundle()
        output = role.runtime_root / "first-output.json"
        runner = make_runner(role.runtime_root, output=output)
        runner.pack.metadata["agent_session"]["checkpoint_initialization_required"] = True
        try:
            with patch("pal.bunshin.runner_components.session_checkpoints.role_gateway_client_from_env", return_value=None), patch.object(
                runner.components.session_checkpoints, "continuation_is_restart_safe",
                return_value=failure != "unsafe",
            ):
                with pytest.raises(AgentSessionCheckpointError, match="acknowledgement|before its initial checkpoint"):
                    await runner.components.agent_session.run_agent_loop(bundle)
            assert bundle.llm_runtime.calls == 0
            assert runner.components.tool_session.observed_tool_call_count == 0
            if failure == "unsafe":
                assert not output.exists()
        finally:
            bundle.execution_runtime.shutdown()

    asyncio.run(scenario())


def test_initial_ack_without_valid_checkpoint_does_not_initialize(role):
    _assignment, attempt, fence, token = launch(role)
    _restore, output = prepare(role, attempt["attempt_id"])
    with pytest.raises(AgentSessionCheckpointError, match="unreadable"):
        acknowledge(role, token)
    payload = checkpoint(role, fence)
    output.write_text(json.dumps({**payload, "logical_coroutine_id": "another-session"}))
    with pytest.raises(AgentSessionCheckpointError, match="wrong logical_coroutine_id"):
        acknowledge(role, token)
    assert status(role) == "uninitialized"
    assert LogicalCoroutineCheckpointStore(role.runtime_root).read("session-router") is None


def test_required_resume_restores_prior_fence_without_initial_ack(role):
    from tests.test_bunshin_completion_resume import seed_checkpoint

    async def scenario():
        bundle = make_bundle()
        output = role.runtime_root / "resumed-output.json"
        try:
            restore, previous = await seed_checkpoint(role.runtime_root, bundle)
            runner = make_runner(role.runtime_root, output=output, restore=restore, token=2)
            runner.pack.metadata["agent_session"]["checkpoint_initialization_required"] = True
            with patch(
                "pal.bunshin.runner_components.session_checkpoints.role_gateway_client_from_env",
                side_effect=AssertionError("resume must not reinitialize"),
            ):
                await runner.components.agent_session.run_agent_loop(bundle)
            assert bundle.llm_runtime.calls == 1
            resumed = runner.components.session_checkpoints.agent_session_checkpoint
            assert resumed["producer_fencing_token"] == 2
            assert resumed["sequence"] > previous["sequence"]
            assert resumed["coroutine_state"]["llm_round_count"] == 40
            assert json.loads(restore.read_text())["producer_fencing_token"] == 1
        finally:
            bundle.execution_runtime.shutdown()

    asyncio.run(scenario())


@pytest.fixture
def workflow_with_delivery(role):
    service = BunshinWorkflowService(role.runtime_root)
    service.create_task({
        "task_id": "task-router", "title": "Router", "objective": "Implement router",
        "profile": "software_engineering.v2_coder", "actor": "test",
        "workspace": {"kind": "new_project", "project_name": "router"},
    })
    repository = role.repository
    repository.delivery_bindings.bind_task_delivery(task_id="task-router", binding={
        "channel_kind": "socket", "channel_id": "test", "reply_target": {"session_id": "first"},
    })
    with repository.database.write_connection() as db:
        db.execute("UPDATE bunshin_aggregate_snapshots SET payload_json = json_set(payload_json,"
                   "'$.task_id','task-router','$.owner','test') WHERE aggregate_type='workflow'")
    return service


def test_parked_approval_survives_manager_replacement_and_has_one_consumer(role, workflow_with_delivery):
    from pal.bunshin.manager import BunshinManager

    service = workflow_with_delivery
    repository = role.repository
    assignment, attempt, fence, token = launch(role)
    write_initial(role, attempt, fence)
    gateway = RoleAssignmentGateway(service)
    wait_id = "approval_operation"
    command = {"access_token": token, "kind": "approval", "wait_id": wait_id, "sequence": 1,
               "request": {"title": "Approve router edit"}}
    delivery = gateway.call("interaction_park", command)["delivery"]
    assert gateway.call("interaction_park", command)["delivery"]["delivery_id"] == delivery["delivery_id"]
    repository.deliveries.acknowledge_task_delivery(delivery["delivery_id"])
    key = assignment["assignment_id"]
    repository.role_retries.queue_role_attempt_retry(
        assignment_id=key, attempt_id_value=attempt["attempt_id"],
        error_kind="waiting_for_user", error_text=wait_id,
    )
    repository.leases.release_lease(f"assignment:{key}", attempt["attempt_id"], fence)
    from pal.bunshin.contracts import DeferredEffectError
    with pytest.raises(DeferredEffectError):
        repository.role_assignments.claim_role_assignment(key)
    manager = BunshinManager(role.runtime_root)
    state = manager._task_status("task-router", workflow_id="workflow-router")
    assert state["workflow"]["pending_approval_count"] == 1
    assert state["task"]["workflow_count"] == 1
    result = asyncio.run(manager._call_method("rebind_task_delivery", {
        "task_id": "task-router", "binding": {"channel_kind": "socket", "channel_id": "other",
                                             "reply_target": {"session_id": "second"}},
    }))
    assert result["changed"]
    pending = repository.deliveries.list_pending_task_deliveries()
    assert [item["dedup_key"] for item in pending] == [f"rebind-replay:task-router:2:{delivery['delivery_id']}"]
    decision = {"approval_id": wait_id, "decision": "accept"}
    asyncio.run(manager.send_decision(decision))
    asyncio.run(manager.send_decision(decision))
    with pytest.raises(ValueError, match="different answer"):
        asyncio.run(manager.send_decision({**decision, "decision": "reject"}))
    next_attempt, next_fence = role.start_attempt(key)
    next_key = next_attempt["attempt_id"]
    next_token = repository.role_access.issue_role_attempt_access_token(
        assignment_id=key, attempt_id_value=next_key, fencing_token=next_fence,
    )
    resumed_gateway = RoleAssignmentGateway(manager.workflow_service)
    with pytest.raises(StaleFencingToken):
        resumed_gateway.call("interaction_resume", {"access_token": token, "wait_id": wait_id})
    args = {"access_token": next_token, "wait_id": wait_id}
    assert resumed_gateway.call("interaction_resume", args)["answer"]["decision"] == decision
    with pytest.raises(ValueError):
        resumed_gateway.call("interaction_resume", args)
    _, output = prepare(role, next_key)
    output.write_text(json.dumps(checkpoint(role, next_fence, sequence=2)))
    resumed_gateway.call("interaction_consumed", {**args, "sequence": 2})
    assert repository.interaction_waits.get(wait_id)["state"] == "consumed"


@pytest.mark.parametrize("cancel", [False, True])
def test_clarification_during_workflow_control(role, workflow_with_delivery, cancel):
    from pal.bunshin.manager import BunshinManager

    service = workflow_with_delivery
    _, attempt, fence, token = launch(role)
    write_initial(role, attempt, fence)
    gateway = RoleAssignmentGateway(service)
    wait_id = "clarification_router"
    gateway.call("interaction_park", {
        "access_token": token, "kind": "clarification", "wait_id": wait_id, "sequence": 1,
        "request": {"questions": [{"id": "scope", "question": "Which routes?", "options": []}]},
    })
    manager = BunshinManager(role.runtime_root)
    assert manager._task_status("task-router", workflow_id="workflow-router")["workflow"]["pending_question"]["question"] == "Which routes?"
    service.control_workflow(
        workflow_id="workflow-router", actor="test", source_channel="socket",
        command="cancel" if cancel else "pause",
    )
    role.repository.role_cancellation.cancel_role_assignments(
        workflow_id="workflow-router", aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id="node-router",
        reason="workflow control", preserve_interactions=not cancel,
    )
    answer = {"clarification_id": wait_id, "answers": [{"question_id": "scope", "answer": "Public"}]}
    if cancel:
        with pytest.raises(ValueError, match="superseded"):
            asyncio.run(manager.send_clarification(answer))
        assert role.repository.interaction_waits.get(wait_id)["state"] == "cancelled"
    else:
        asyncio.run(manager.answer_workflow_question({"workflow_id": "workflow-router", "answer": "Public"}))
        asyncio.run(manager.send_clarification(answer))
        deliveries = role.repository.deliveries.list_pending_task_deliveries()
        assert sum(row["event_kind"] == "clarification_resolved" for row in deliveries) == 1
        assert role.repository.interaction_waits.get(wait_id)["state"] == "answered"


async def _interactive_worker(options):
    """Subprocess fixture using the built-in tool loop and authenticated gateway."""
    from contextlib import ExitStack
    from pathlib import Path
    from pal.bunshin.interaction_suspend import InteractionParked
    from pal.llm import generation_result_from_values
    from pal.shared.tool_protocol import ToolCallIR, ToolExecutionResult

    root = Path(options["root"])
    bundle = make_bundle()
    runner = make_runner(root, output=Path(options["output"]), restore=options["restore"], token=options["fence"])
    runner.pack.metadata["agent_session"].update({
        "session_id": "session-router", "workflow_id": "workflow-router",
        "stage_key": "module:router:implementation", "response_key": options["assignment"],
        "durable_tool_interactions": True, "checkpoint_initialization_required": True,
    })
    calls = [ToolCallIR(name="op_read_file", call_id=key, args={"path": key}) for key in ("first", "approval", "last")]

    async def perform(_state, _continuation, call, **_kwargs):
        if call.call_id == "approval":
            assert await runner.components.control.request_approval(call.name, call) == "accept"
        with (root / "observed-calls").open("a") as observed:
            observed.write(call.call_id + "\n")
        return ToolExecutionResult(name=call.name, call_id=call.call_id, ok=True, llm_text="ok")

    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(runner.components.agent_runtime.tool_observation,
                "execute_bunshin_tool_with_observation", side_effect=perform))
            if not options["restore"]:
                stack.enter_context(patch.object(bundle.llm_runtime, "_generate_response",
                    return_value=generation_result_from_values(tool_calls=calls, finish_reason="tool_calls")))
                with pytest.raises(InteractionParked):
                    await runner.components.agent_session.run_agent_loop(bundle)
            else:
                await runner.components.agent_session.run_agent_loop(bundle)
    finally:
        bundle.execution_runtime.shutdown()


def test_fresh_worker_process_resumes_through_manager_socket(role, workflow_with_delivery):
    import os
    import sys
    from pal.bunshin.manager import BunshinManager
    from pal.bunshin.ipc import ROLE_GATEWAY_TOKEN_ENV, start_role_gateway_server, cleanup_role_gateway_endpoint

    async def worker(manager, assignment, attempt, fence, token):
        restore, output = prepare(role, attempt["attempt_id"])
        options = {"root": str(role.runtime_root), "restore": str(restore) if restore else None,
                   "output": str(output), "assignment": assignment, "fence": fence}
        server, _ = await start_role_gateway_server(role.runtime_root, manager._handle_role_client)
        try:
            async with server:
                child = await asyncio.create_subprocess_exec(
                    sys.executable, "-c",
                    "import asyncio,json,sys; from tests.test_bunshin_checkpoint_lifecycle import _interactive_worker; "
                    "asyncio.run(_interactive_worker(json.load(sys.stdin)))",
                    stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    env={**os.environ, ROLE_GATEWAY_TOKEN_ENV: token},
                )
                try:
                    _, stderr = await asyncio.wait_for(child.communicate(json.dumps(options).encode()), timeout=90)
                    assert child.returncode == 0, stderr.decode(errors="replace")
                finally:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()
        finally:
            await cleanup_role_gateway_endpoint(role.runtime_root)

    async def scenario():
        assignment, attempt, fence, token = launch(role)
        key = assignment["assignment_id"]
        await worker(BunshinManager(role.runtime_root), key, attempt, fence, token)
        observed = role.runtime_root / "observed-calls"
        assert observed.read_text().splitlines() == ["first"]
        row, = role.repository.interaction_waits.pending(assignment_id=key)
        role.repository.role_retries.queue_role_attempt_retry(
            assignment_id=key, attempt_id_value=attempt["attempt_id"], error_kind="waiting_for_user", error_text=row["wait_id"],
        )
        role.repository.leases.release_lease(f"assignment:{key}", attempt["attempt_id"], fence)
        replacement = BunshinManager(role.runtime_root)
        await replacement.send_decision({"approval_id": row["wait_id"], "decision": "accept"})
        successor, next_fence = role.start_attempt(key)
        next_token = role.repository.role_access.issue_role_attempt_access_token(
            assignment_id=key, attempt_id_value=successor["attempt_id"], fencing_token=next_fence,
        )
        await worker(replacement, key, successor, next_fence, next_token)
        assert observed.read_text().splitlines() == ["first", "approval", "last"]
        assert role.repository.interaction_waits.get(row["wait_id"])["state"] == "consumed"

    asyncio.run(scenario())
