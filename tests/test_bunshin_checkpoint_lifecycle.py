"""Persisted startup admission distinguishes never-created from lost continuation."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pal.bunshin.checkpoint import (
    AgentSessionCheckpointError, LogicalCoroutineCheckpointStore, open_agent_session_checkpoint,
    seal_agent_session_checkpoint,
)
from pal.bunshin.contracts import ActionEnvelope, AggregateType, StaleFencingToken
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


def write_parked(role, output, fence, wait_id, *, sequence=1, kind="approval"):
    payload = open_agent_session_checkpoint(role.runtime_root, checkpoint(role, fence, sequence))
    payload["coroutine_state"]["parked_batch"] = {
        "wait_id": wait_id, "kind": kind, "turn_id": "turn",
        "calls": [{"call_id": "call", "name": "op_file_edit", "args": {}}], "completed": [],
    }
    output.write_text(json.dumps(seal_agent_session_checkpoint(role.runtime_root, payload)))


def acknowledge(role, token):
    gateway = RoleAssignmentGateway(BunshinWorkflowService(role.runtime_root))
    return gateway.call("checkpoint_initialize", {"access_token": token})


def park(role):
    with role.repository.database.write_connection() as connection:
        role.repository.role_sessions.transition_role_session_locked(
            connection, "session-router", RoleSessionAction.PARK, now="test",
        )


def transition_node(role, action_type, payload=None, *, aggregate_id="node-router"):
    node = role.repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, aggregate_id)
    return role.repository.transitions.dispatch(ActionEnvelope(
        action_type=action_type, workflow_id=node.workflow_id, aggregate_type=node.aggregate_type,
        aggregate_id=node.aggregate_id, actor="test", expected_version=node.version,
        payload=payload or {},
    ))


def run_node_control_effect(role, service, effect_type):
    from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator

    effect = next(item for item in role.repository.outbox_claims.claim_outbox("test-control", limit=100)
                  if item["effect_type"] == effect_type)
    asyncio.run(SemanticOrchestrator(service).execute_semantic_effect(effect))


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
    output, _ = write_initial(role, attempt, fence)
    gateway = RoleAssignmentGateway(service)
    wait_id = "approval_operation"
    write_parked(role, output, fence, wait_id)
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
    restore, output = prepare(role, next_key)
    assert restore is not None
    args = {"access_token": next_token, "wait_id": wait_id}
    assert resumed_gateway.call("interaction_resume", args)["answer"]["decision"] == decision
    with pytest.raises(ValueError):
        resumed_gateway.call("interaction_resume", args)
    output.write_text(json.dumps(checkpoint(role, next_fence, sequence=2)))
    resumed_gateway.call("interaction_consumed", {**args, "sequence": 2})
    assert repository.interaction_waits.get(wait_id)["state"] == "consumed"


@pytest.mark.parametrize("cancel", [False, True])
def test_clarification_during_workflow_control(role, workflow_with_delivery, cancel):
    from pal.bunshin.manager import BunshinManager

    service = workflow_with_delivery
    _, attempt, fence, token = launch(role)
    output, _ = write_initial(role, attempt, fence)
    gateway = RoleAssignmentGateway(service)
    wait_id = "clarification_router"
    write_parked(role, output, fence, wait_id, kind="clarification")
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
    pending = role.repository.deliveries.list_pending_task_deliveries()
    assert any(row["event_kind"] == "clarification_requested" for row in pending) is (not cancel)
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
        assert all(row["event_kind"] != "clarification_requested" for row in deliveries)
        assert role.repository.interaction_waits.get(wait_id)["state"] == "answered"


async def _interactive_worker(options):
    """Subprocess fixture using the built-in tool loop and authenticated gateway."""
    from contextlib import ExitStack
    from pathlib import Path
    from pal.bunshin.interaction_suspend import InteractionParked
    from pal.bunshin.profiles import BunshinProfileRegistry
    from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
    from pal.llm import generation_result_from_values
    from pal.shared.messages import BunshinInvocationPack
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
    if options["clarification"]:
        profile = BunshinProfileRegistry(runtime_root=root).resolve_pack(
            BunshinInvocationPack(invocation_id="clarifying-coder", bunshin_profile="software_engineering.v2_coder"),
        )
        scoped = BunshinScopedExecutionRuntime(
            bundle.execution_runtime, profile.allowed_capabilities,
            workspace={"invocation_id": "coder", "bunshin": {"role": "implementation", "mode": "repair"}},
            request_user_clarification=runner.components.control.request_architecture_clarification,
        )
        calls[1] = ToolCallIR(name="ask_question", call_id="clarification", args={
            "title": "Scope", "question": "Which limit?",
            "option_1": "5", "option_2": "6", "option_3": "7",
        })

    async def perform(_state, _continuation, call, **_kwargs):
        if call.call_id == "approval":
            assert await runner.components.control.request_approval(call.name, call) == "accept"
        elif call.call_id == "clarification":
            result = await scoped.execute_tool_async(call)
            (root / "clarification-result").write_text(result.llm_text)
            payload = (result.structured or {}).get("payload", {})
            assert result.ok and payload.get("answer") == "5", result.llm_text
            assert "task_revision" not in payload
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


@pytest.mark.parametrize("clarification,supersede", [(False, False), (True, False), (False, True)],
                         ids=["approval", "coder-clarification", "stale-assignment"])
def test_fresh_worker_process_resumes_through_manager_socket(role, workflow_with_delivery, clarification, supersede):
    import os
    import sys
    from pal.bunshin.manager import BunshinManager
    from pal.bunshin.ipc import ROLE_GATEWAY_TOKEN_ENV, start_role_gateway_server, cleanup_role_gateway_endpoint
    from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator

    async def worker(manager, assignment, attempt, fence, token):
        restore, output = prepare(role, attempt["attempt_id"])
        options = {"root": str(role.runtime_root), "restore": str(restore) if restore else None,
                   "output": str(output), "assignment": assignment, "fence": fence, "clarification": clarification}
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
        return output

    async def scenario():
        if supersede:
            transition_node(role, "DEPENDENCIES_ACCEPTED")
            transition_node(role, "START_PRODUCING", {"active_worker_id": "session-router", "fencing_token": 1})
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
        if supersede:
            transition_node(role, "REQUEST_STALE", {"stale_reason_ref": role.input_ref.to_dict()})
            effect = next(item for item in role.repository.outbox_claims.claim_outbox("stale-test", limit=100)
                          if item["effect_type"] == "cancel_role")
            await SemanticOrchestrator(workflow_with_delivery).execute_semantic_effect(effect)
            assert role.repository.interaction_waits.get(row["wait_id"])["state"] == "cancelled"
            with pytest.raises(AgentSessionCheckpointError, match="no committed actionable interaction"):
                prepare(role, attempt["attempt_id"])
            transition_node(role, "REQUEUE_STALE", {
                "unit_contract_ref": role.input_ref.to_dict(), "dependency_fingerprint": "updated-dependencies",
            })
            updated = role.repository.role_assignments.create_role_assignment(role.request(key="updated-dependencies"))
            assert updated["session_id"] == assignment["session_id"]
            key = updated["assignment_id"]
        elif clarification:
            await replacement.send_clarification({
                "clarification_id": row["wait_id"], "answers": [{"question_id": "architecture-question", "answer": "5"}],
            })
        else:
            await replacement.send_decision({"approval_id": row["wait_id"], "decision": "accept"})
        successor, next_fence = role.start_attempt(key)
        next_token = role.repository.role_access.issue_role_attempt_access_token(
            assignment_id=key, attempt_id_value=successor["attempt_id"], fencing_token=next_fence,
        )
        output = await worker(replacement, key, successor, next_fence, next_token)
        if supersede:
            assert observed.read_text().splitlines() == ["first"]
            private = open_agent_session_checkpoint(role.runtime_root, json.loads(output.read_text()))
            assert private["coroutine_state"]["active_response_key"] == key
            assert not private["coroutine_state"].get("parked_batch")
            assert role.repository.interaction_waits.get(row["wait_id"])["state"] == "cancelled"
            return
        diagnostic = role.runtime_root / "clarification-result"
        assert observed.read_text().splitlines() == ["first", "clarification" if clarification else "approval", "last"], (
            diagnostic.read_text() if diagnostic.exists() else ""
        )
        assert role.repository.interaction_waits.get(row["wait_id"])["state"] == "consumed"

    asyncio.run(scenario())


@pytest.mark.parametrize("consume,recover_first", [(False, False), (True, False), (True, True)],
                         ids=["park", "consume", "consume-after-recovery"])
def test_interaction_rollback_keeps_committed_checkpoint(role, workflow_with_delivery, consume, recover_first):
    from pal.bunshin.storage.role_attempts import RoleAttemptsStore
    from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator

    transition_node(role, "DEPENDENCIES_ACCEPTED")
    transition_node(role, "START_PRODUCING", {"active_worker_id": "session-router", "fencing_token": 1})
    assignment, attempt, fence, token = launch(role)
    output, initial = write_initial(role, attempt, fence)
    gateway = RoleAssignmentGateway(workflow_with_delivery)
    gateway.call("checkpoint_initialize", {"access_token": token})
    wait_id = "approval_atomic"
    write_parked(role, output, fence, wait_id, sequence=2)
    args = {"access_token": token, "wait_id": wait_id, "kind": "approval",
            "sequence": 2, "request": {"title": "Approve edit"}}
    previous = initial
    if consume:
        gateway.call("interaction_park", args)
        previous = json.loads(output.read_text())
        role.repository.interaction_waits.answer(wait_id, {"decision": {"decision": "accept"}})
        gateway.call("interaction_resume", args)
        output.write_text(json.dumps(checkpoint(role, fence, sequence=3)))

    original = RoleAttemptsStore.publish_initial_role_checkpoint

    def fail_before_commit(self, **kwargs):
        original(self, **kwargs)
        raise RuntimeError("injected after checkpoint publication")

    with patch.object(RoleAttemptsStore, "publish_initial_role_checkpoint", fail_before_commit):
        with pytest.raises(RuntimeError, match="after checkpoint publication"):
            gateway.call("interaction_consumed" if consume else "interaction_park",
                         {**args, "sequence": 3} if consume else args)

    restart(role)
    assert LogicalCoroutineCheckpointStore(role.runtime_root).read("session-router") == previous
    wait = role.repository.interaction_waits.get(wait_id)
    if consume:
        assert wait["state"] == "resuming"
    else:
        assert wait is None
        assert not role.repository.deliveries.list_pending_task_deliveries()
    with pytest.raises(AgentSessionCheckpointError, match="uncommitted interaction"):
        checkpoints(role).publish_agent_session_checkpoint("session-router", fence, output)
    if consume:
        transition_node(role, "ENTER_TRIAGE", {"blocker": {"kind": "interrupted_interaction"}})
        if recover_first:
            key = assignment["assignment_id"]
            role.repository.role_retries.queue_role_attempt_retry(
                assignment_id=key, attempt_id_value=attempt["attempt_id"],
                error_kind="manager_restart", error_text="original worker gone",
            )
            with pytest.raises(AgentSessionCheckpointError, match="requires checkpoint reconciliation"):
                role.repository.role_assignments.claim_role_assignment(key)
            assert len(role.repository.role_attempts.list_role_attempts(key)) == 1
            role.repository.leases.release_lease(f"assignment:{key}", attempt["attempt_id"], fence)
            assert asyncio.run(SemanticOrchestrator(workflow_with_delivery).recover_background_assignments()) == 0
            assert role.repository.interaction_waits.get(wait_id)["state"] == "resuming"
        run_node_control_effect(role, workflow_with_delivery, "quiesce_role_for_triage")
        assert role.repository.role_assignments.read_role_assignment(assignment["assignment_id"])["state"] == "cancelled"
        assert role.repository.interaction_waits.get(wait_id)["state"] == "resuming"
        closed = output.read_text()
        output.unlink()
        replacement = BunshinWorkflowService(role.runtime_root)

        def resolve():
            return replacement.resolve_triage(
                workflow_id="workflow-router", actor="test", source_channel="local",
                subject="module:router", resolution="Checked the retired attempt's local tool effect.",
            )

        with pytest.raises(ValueError, match="no proven closed checkpoint") as unresolved:
            resolve()
        assert "cancel this workflow" in str(unresolved.value)
        assert "start_bunshin_workflow with the same Task" in str(unresolved.value)
        assert "restart_bunshin_execution" not in str(unresolved.value)
        assert role.repository.interaction_waits.get(wait_id)["state"] == "resuming"
        output.write_text(closed)
        assert resolve()["status"] == "triage_resolved"
        assert role.repository.interaction_waits.get(wait_id)["state"] == "consumed"
        replacement_assignment = role.repository.role_assignments.create_role_assignment(role.request(key="after-triage"))
        key = replacement_assignment["assignment_id"]
        new, next_fence = role.start_attempt(key)
        restore, new_output = prepare(role, new["attempt_id"])
        assert json.loads(restore.read_text()) == json.loads(closed)
        token = role.repository.role_access.issue_role_attempt_access_token(
            assignment_id=key, attempt_id_value=new["attempt_id"], fencing_token=next_fence,
        )
        write_parked(role, new_output, next_fence, "approval_next", sequence=4)
        gateway.call("interaction_park", {**args, "access_token": token, "sequence": 4, "wait_id": "approval_next"})
        assert role.repository.interaction_waits.get("approval_next")["state"] == "open"


@pytest.mark.parametrize("closed", [True, False], ids=["closed", "unproven"])
def test_pause_reconciles_claimed_interaction_or_preserves_it_for_triage(role, workflow_with_delivery, closed):
    service = workflow_with_delivery
    transition_node(role, "DEPENDENCIES_ACCEPTED")
    transition_node(role, "START_PRODUCING", {"active_worker_id": "session-router", "fencing_token": 1})
    _, attempt, fence, token = launch(role)
    output, _ = write_initial(role, attempt, fence)
    gateway = RoleAssignmentGateway(service)
    gateway.call("checkpoint_initialize", {"access_token": token})
    wait_id = "approval_pause"
    args = {"access_token": token, "wait_id": wait_id, "kind": "approval", "sequence": 2,
            "request": {"title": "Approve edit"}}
    write_parked(role, output, fence, wait_id, sequence=2)
    gateway.call("interaction_park", args)
    role.repository.interaction_waits.answer(wait_id, {"decision": {"decision": "accept"}})
    gateway.call("interaction_resume", args)
    if closed:
        output.write_text(json.dumps(checkpoint(role, fence, sequence=3)))
    else:
        output.unlink()
    transition_node(role, "REQUEST_PAUSE")
    run_node_control_effect(role, service, "pause_role")
    assert role.repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, "node-router").state == "PAUSED"
    if closed:
        assert role.repository.interaction_waits.get(wait_id)["state"] == "consumed"
        restore, _ = prepare(role)
        assert json.loads(restore.read_text())["sequence"] == 3
    else:
        assert role.repository.interaction_waits.get(wait_id)["state"] == "resuming"
        with pytest.raises(AgentSessionCheckpointError, match="requires checkpoint reconciliation"):
            prepare(role)
        transition_node(role, "REQUEST_CANCEL")
        run_node_control_effect(role, service, "cancel_role")
        assert role.repository.interaction_waits.get(wait_id)["state"] == "cancelled"


@pytest.mark.parametrize("new_epoch", [False, True], ids=["same-node", "new-epoch"])
def test_superseded_claimed_interaction_requires_proof_before_new_assignment(role, workflow_with_delivery, new_epoch):
    service = workflow_with_delivery
    transition_node(role, "DEPENDENCIES_ACCEPTED")
    transition_node(role, "START_PRODUCING", {"active_worker_id": "session-router", "fencing_token": 1})
    _, attempt, fence, token = launch(role)
    output, _ = write_initial(role, attempt, fence)
    gateway = RoleAssignmentGateway(service)
    gateway.call("checkpoint_initialize", {"access_token": token})
    wait_id = "approval_stale_claimed"
    args = {"access_token": token, "wait_id": wait_id, "kind": "approval", "sequence": 2,
            "request": {"title": "Approve edit"}}
    write_parked(role, output, fence, wait_id, sequence=2)
    gateway.call("interaction_park", args)
    role.repository.interaction_waits.answer(wait_id, {"decision": {"decision": "accept"}})
    gateway.call("interaction_resume", args)
    closed = checkpoint(role, fence, sequence=3)
    output.write_text(json.dumps(closed))
    with pytest.raises(AgentSessionCheckpointError, match="requires checkpoint reconciliation"):
        prepare(role, attempt["attempt_id"])
    assert json.loads(output.read_text()) == closed
    output.unlink()
    transition_node(role, "REQUEST_STALE", {"stale_reason_ref": role.input_ref.to_dict()})
    run_node_control_effect(role, service, "cancel_role")
    assert role.repository.interaction_waits.get(wait_id)["state"] == "cancelled"
    successor_node = "node-router"
    if new_epoch:
        successor_node = "node-router-next"
        role.repository.transitions.dispatch(ActionEnvelope(
            action_type="CREATE_NODE_RUN", workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=successor_node,
            actor="test", expected_version=0,
            payload={"epoch_id": "epoch-next", "module_name": "router",
                     "unit_contract_ref": role.input_ref.to_dict()},
        ))
        transition_node(role, "DEPENDENCIES_ACCEPTED", aggregate_id=successor_node)
    else:
        transition_node(role, "REQUEUE_STALE", {"unit_contract_ref": role.input_ref.to_dict(),
                                              "dependency_fingerprint": "changed"})
    transition_node(role, "START_PRODUCING", {"active_worker_id": "session-router", "fencing_token": 2},
                    aggregate_id=successor_node)
    successor = role.repository.role_assignments.create_role_assignment(
        replace(role.request(key="stale-retry"), aggregate_id=successor_node))
    next_attempt, _ = role.start_attempt(successor["assignment_id"])
    with pytest.raises(AgentSessionCheckpointError, match="requires checkpoint reconciliation"):
        prepare(role, next_attempt["attempt_id"])
    transition_node(role, "ENTER_TRIAGE", {"blocker": {"kind": "interrupted_interaction"}},
                    aggregate_id=successor_node)
    run_node_control_effect(role, service, "quiesce_role_for_triage")
    with pytest.raises(ValueError, match="no proven closed checkpoint"):
        service.resolve_triage(workflow_id="workflow-router", actor="test", source_channel="local",
                               subject="module:router", resolution="Checked tool effect")
    output.write_text(json.dumps(closed))
    assert service.resolve_triage(workflow_id="workflow-router", actor="test", source_channel="local",
                                  subject="module:router", resolution="Checked tool effect")["status"] == "triage_resolved"
    assert role.repository.interaction_waits.get(wait_id)["state"] == "consumed"
    assert LogicalCoroutineCheckpointStore(role.runtime_root).read("session-router") == closed
    transition_node(role, "START_PRODUCING", {"active_worker_id": "session-router", "fencing_token": 3},
                    aggregate_id=successor_node)
    retry = role.repository.role_assignments.create_role_assignment(
        replace(role.request(key="after-triage-stale-retry"), aggregate_id=successor_node))
    resumed, _ = role.start_attempt(retry["assignment_id"])
    restore, _ = prepare(role, resumed["attempt_id"])
    assert json.loads(restore.read_text()) == closed


def test_unresolved_claimed_interaction_failure_keeps_attempt_lease_record(role, workflow_with_delivery):
    assignment, attempt, fence, token = launch(role)
    output, _ = write_initial(role, attempt, fence)
    gateway = RoleAssignmentGateway(workflow_with_delivery)
    gateway.call("checkpoint_initialize", {"access_token": token})
    wait_id = "approval_failure"
    write_parked(role, output, fence, wait_id, sequence=2)
    args = {"access_token": token, "wait_id": wait_id, "kind": "approval", "sequence": 2,
            "request": {"title": "Approve edit"}}
    gateway.call("interaction_park", args)
    role.repository.interaction_waits.answer(wait_id, {"decision": {"decision": "accept"}})
    gateway.call("interaction_resume", args)
    key = assignment["assignment_id"]
    resource = f"assignment:{key}"
    role.repository.leases.update_lease_metadata(resource, attempt["attempt_id"], fence, {"marker": "original"})
    role.repository.role_retries.queue_role_attempt_retry(
        assignment_id=key, attempt_id_value=attempt["attempt_id"], error_kind="manager_restart", error_text="lost",
    )
    with pytest.raises(AgentSessionCheckpointError, match="requires checkpoint reconciliation"):
        role.repository.role_assignments.claim_role_assignment(key)
    failure = role.artifacts.put_json({"reason": "interrupted interaction"}, artifact_type="RoleFailureArtifact")
    role.repository.role_retries.record_role_failure_result(
        assignment_id=key, attempt_id_value=attempt["attempt_id"], error_kind="interrupted_interaction",
        error_text="No closed boundary", failure_artifact_ref=failure.to_dict(), payload_hash="failure",
        settlement_action={"action_type": "ROLE_FAILED"},
    )
    lease = role.repository.leases.read_lease(resource)
    assert lease["owner_id"] == "" and lease["fencing_token"] == fence
    assert lease["metadata"] == {"marker": "original"}
    assert role.repository.interaction_waits.get(wait_id)["state"] == "resuming"


def test_cancelled_interaction_is_filtered_from_durable_and_buffered_delivery(role, workflow_with_delivery):
    from pal.bunshin.capabilities import BunshinManagerProvider
    from pal.bunshin.manager import BunshinManager
    from pal.bunshin.source import BunshinControlEventHandler, BunshinEventSource

    _, attempt, fence, token = launch(role)
    output, _ = write_initial(role, attempt, fence)
    gateway = RoleAssignmentGateway(workflow_with_delivery)
    gateway.call("checkpoint_initialize", {"access_token": token})
    wait_id = "clarification_waiting_channel"
    write_parked(role, output, fence, wait_id, sequence=2, kind="clarification")
    gateway.call("interaction_park", {
        "access_token": token, "sequence": 2, "kind": "clarification", "wait_id": wait_id,
        "request": {"title": "Which data?", "questions": [{
            "title": "Data", "question": "Which data?", "options": [{"label": "Current batch"}],
        }]},
    })
    repository = role.repository
    original, = repository.deliveries.list_pending_task_deliveries()
    repository.deliveries.replay_task_delivery(delivery_id=original["delivery_id"], dedup_key="rebind-replay:test")
    manager = BunshinManager(runtime_root=role.runtime_root)
    buffered = manager._pending_task_delivery_events()
    assert len(buffered) == 2
    for event in buffered:
        manager.events.queue_event(event)
    frontend = BunshinManagerProvider(runtime_root=role.runtime_root)
    for event in buffered:
        frontend._buffer_event(event)
    frontend_events = BunshinEventSource(frontend, drain_limit=1)
    with patch.object(frontend, "_live_route_for_binding", return_value={
        "endpoint_id": "test", "channel_kind": "socket", "reply_target": {"session_id": "test"},
        "control_scope_key": "socket:test:test",
    }):
        inflight, = frontend_events.drain(None)

    repository.role_cancellation.cancel_role_assignments(
        workflow_id="workflow-router", aggregate_type=AggregateType.DAG_NODE_RUN,
        aggregate_id="node-router", reason="workflow cancelled",
    )
    assert repository.interaction_waits.get(wait_id)["state"] == "cancelled"
    assert BunshinManager(runtime_root=role.runtime_root)._pending_task_delivery_events() == []
    assert not frontend_events.prepare(None)
    endpoint = Mock(deliver=AsyncMock(return_value=True))
    with patch.object(frontend.client, "request_sync", return_value={"parts": []}) as request:
        asyncio.run(BunshinControlEventHandler(frontend, endpoint).handle(inflight, None))
        assert [call.args[0] for call in request.call_args_list] == ["list_task_delivery_parts"]
    endpoint.deliver.assert_not_awaited()
    assert frontend.drain_events_sync()["remaining"] == 0

    async def reconnect():
        writer = Mock(drain=AsyncMock())
        shutdown_event = asyncio.Event()
        shutdown_event.set()
        await manager.events.handle_subscription({
            "id": "reconnect",
        }, asyncio.StreamReader(), writer, shutdown_event=shutdown_event)
        writer.write.assert_called_once()  # Subscription response only.

    asyncio.run(reconnect())


def _begin_router(role):
    transition_node(role, "DEPENDENCIES_ACCEPTED", {"accepted_dependency_node_ids": []})
    lease = role.repository.leases.claim_lease("node:node-router:writer", "session-router")
    return transition_node(role, "START_PRODUCING", {
        "active_worker_id": "session-router", "fencing_token": lease.fencing_token,
        "lease_resource_key": lease.resource_key,
    }).snapshot


@pytest.mark.parametrize("already_settled", [False, True])
def test_failure_receipt_replay_never_becomes_candidate(role, already_settled):
    from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
    from pal.bunshin.role_protocol import stable_hash
    from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator

    node = _begin_router(role)
    assignment, attempt, fence, _ = launch(role)
    assignment_id = assignment["assignment_id"]
    payload = {"kind": "role_assignment_failed", "role": "implementation", "attempt_count": 3,
               "error": "worker was exhausted", "exhausted": True, "error_kind": "attempt_budget_exhausted"}
    ref = role.artifacts.put_json(payload, artifact_type="RoleAssignmentFailureArtifact")
    receipt = role.repository.role_retries.record_role_failure_result(
        assignment_id=assignment_id, attempt_id_value=attempt["attempt_id"],
        error_kind=payload["error_kind"], error_text=payload["error"],
        failure_artifact_ref=ref.to_dict(), payload_hash=stable_hash(payload),
        settlement_action={"action_type": "ROLE_FAILED"},
    )
    if already_settled:
        role.repository.role_submissions.settle_role_assignment(
            assignment_id=assignment_id, submission_payload_hash=receipt.payload_hash)
    # The transaction itself rejects a mismatched receipt, even if the normal
    # wrapper is invoked accidentally (including on a previously settled row).
    with pytest.raises(ValueError, match="does not match the recorded outcome"):
        role.repository.transitions.dispatch(ActionEnvelope(
            action_type="SUBMIT_CANDIDATE", workflow_id=node.workflow_id,
            aggregate_type=node.aggregate_type, aggregate_id=node.aggregate_id,
            actor="test", expected_version=node.version,
            payload={"fencing_token": 1, "producer_report_ref": ref.to_dict()},
        ), role_assignment_id=assignment_id, role_submission_payload_hash=receipt.payload_hash)
    assert role.repository.snapshots.read_snapshot(node.aggregate_type, node.aggregate_id).state == "PRODUCING"

    restart(role)
    components = SemanticOrchestrator(BunshinWorkflowService(role.runtime_root)).components
    effect = {"effect_key": "replay-failed", "effect_type": "run_implementation_role",
              "workflow_id": node.workflow_id, "aggregate_type": node.aggregate_type.value,
              "aggregate_id": node.aggregate_id}
    components.assignment_retries.background.bind(effect["effect_key"], assignment_id)

    async def replay(command):
        await components.attempt_execution.run_profile_inner(
            effect=command, snapshot=node, invocation_id="session-router",
            lease_resource="node:node-router:writer", fencing_token=1,
            profile="software_engineering.v2_coder",
            activation=RoleActivation(OrchestrationRole.IMPLEMENTATION, RoleMode.PRODUCE),
            instruction="Must not invoke a worker", reference_refs={},
        )
        pytest.fail("failure receipt returned a success-shaped terminal")

    from pal.bunshin.contracts import SubmissionInvariantError
    with patch.object(components.attempt_execution.workspace_preparation, "execute",
                      side_effect=AssertionError("must not prepare a worker")):
        with pytest.raises(SubmissionInvariantError, match="recorded ROLE_FAILED"):
            asyncio.run(replay(effect))
        runner = AsyncMock(side_effect=AssertionError("must not enter normal role wrapper"))
        outcome = asyncio.run(components.assignment_execution.background_worker_loop(effect, runner))
        runner.assert_not_awaited()
    assert outcome["status"] == "triage_required"
    triaged = role.repository.snapshots.read_snapshot(node.aggregate_type, node.aggregate_id)
    assert triaged.state == "TRIAGE_REQUIRED"
    assert triaged.payload["failure_artifact_ref"] == ref.to_dict()
    record = role.repository.role_assignments.read_role_assignment(assignment_id)
    assert record["state"] == "settled"
    assert record["submission_artifact_ref"] == ref.to_dict()
    assert record["submission_payload_hash"] == receipt.payload_hash


@pytest.mark.parametrize("recorded", [False, True], ids=["retry", "receipt-replay"])
def test_retired_attempt_selects_latest_closed_checkpoint_before_resume(role, recorded):
    from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
    from pal.bunshin.role_protocol import stable_hash
    from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator

    node = _begin_router(role)
    assignment, attempt, fence, token = launch(role)
    output, _ = write_initial(role, attempt, fence)
    acknowledge(role, token)
    selected = LogicalCoroutineCheckpointStore(role.runtime_root)
    assert selected.read("session-router")["sequence"] == 1
    newer = checkpoint(role, fence, sequence=7 if recorded else 5)
    output.write_text(json.dumps(newer))
    if recorded:
        role.repository.role_submissions.record_role_submission(
            assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
            fencing_token=fence, artifact_ref=role.submission_ref.to_dict(),
            payload_hash=stable_hash({"status": "candidate_ready"}),
            settlement_action={"action_type": "SUBMIT_CANDIDATE"},
        )
    assert not role.repository.role_maintenance.reconcile_retired_role_checkpoint("session-router")
    assert selected.read("session-router")["sequence"] == 1
    role.repository.leases.release_lease(f"assignment:{assignment['assignment_id']}", attempt["attempt_id"], fence)
    restart(role)
    components = SemanticOrchestrator(BunshinWorkflowService(role.runtime_root)).components
    components.assignment_retries.background.bind("recover-role", assignment["assignment_id"])
    recovered = components.attempt_execution.existing_assignment(
        {"effect_key": "recover-role"}, node,
        RoleActivation(OrchestrationRole.IMPLEMENTATION, RoleMode.PRODUCE),
    )
    assert recovered is not None
    assert selected.read("session-router") == newer
    assert not role.repository.role_maintenance.reconcile_retired_role_checkpoint("session-router")
    restore, _ = prepare(role)
    assert json.loads(restore.read_text()) == newer


@pytest.mark.parametrize("candidate_kind", ["parked", "foreign-fence", "rollback"])
def test_retired_attempt_does_not_bypass_checkpoint_selection(role, candidate_kind):
    assignment, attempt, fence, token = launch(role)
    output, initial = write_initial(role, attempt, fence)
    acknowledge(role, token)
    if candidate_kind == "parked":
        write_parked(role, output, fence, "uncommitted", sequence=3)
    else:
        output.write_text(json.dumps(checkpoint(role, fence + (candidate_kind == "foreign-fence"), sequence=3)))
    role.repository.leases.release_lease(f"assignment:{assignment['assignment_id']}", attempt["attempt_id"], fence)
    restart(role)
    recover = role.repository.role_maintenance.reconcile_retired_role_checkpoint
    if candidate_kind == "parked":
        assert not recover("session-router")
    elif candidate_kind == "foreign-fence":
        with pytest.raises(AgentSessionCheckpointError, match="wrong producer_fencing_token"):
            recover("session-router")
    else:
        from pal.bunshin.storage.role_sessions import RoleSessionsStore
        original = RoleSessionsStore.publish_role_session_checkpoint_locked

        def crash_after_selection(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise RuntimeError("injected after selecting retired point")

        with patch.object(RoleSessionsStore, "publish_role_session_checkpoint_locked", crash_after_selection):
            with pytest.raises(RuntimeError, match="injected"):
                recover("session-router")
    selected = LogicalCoroutineCheckpointStore(role.runtime_root)
    assert selected.read("session-router") == initial
    if candidate_kind == "rollback":
        assert recover("session-router")
        assert selected.read("session-router")["sequence"] == 3


def test_successful_receipt_cannot_discharge_role_failed_even_in_shared_transaction(role):
    from pal.bunshin.role_protocol import stable_hash

    node = _begin_router(role)
    assignment, attempt, fence, _ = launch(role)
    receipt = role.repository.role_submissions.record_role_submission(
        assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
        fencing_token=fence, artifact_ref=role.submission_ref.to_dict(),
        payload_hash=stable_hash({"status": "candidate_ready"}),
        settlement_action={"action_type": "SUBMIT_CANDIDATE"},
    )
    failure = ActionEnvelope(
        action_type="ROLE_FAILED", workflow_id=node.workflow_id, aggregate_type=node.aggregate_type,
        aggregate_id=node.aggregate_id, actor="test", expected_version=node.version,
        payload={"failure_artifact_ref": role.submission_ref.to_dict(), "blocker": {"kind": "role_failure"}},
    )
    # Reject before the first domain write even when the caller catches the
    # guard inside a larger business transaction.
    with role.repository.transaction() as work:
        with pytest.raises(ValueError, match="does not match the recorded outcome"):
            work.transitions.dispatch(failure, role_assignment_id=assignment["assignment_id"],
                                      role_submission_payload_hash=receipt.payload_hash)
    assert role.repository.snapshots.read_snapshot(node.aggregate_type, node.aggregate_id).state == "PRODUCING"
    assert role.repository.role_assignments.read_role_assignment(assignment["assignment_id"])["state"] == "result_recorded"


def test_recovery_never_selects_a_superseded_attempt_output(role):
    assignment, attempt, fence, token = launch(role)
    output, initial = write_initial(role, attempt, fence)
    acknowledge(role, token)
    output.write_text(json.dumps(checkpoint(role, fence, sequence=3)))
    role.repository.role_retries.queue_role_attempt_retry(
        assignment_id=assignment["assignment_id"], attempt_id_value=attempt["attempt_id"],
        error_kind="worker_lost", error_text="old attempt retired",
    )
    role.repository.leases.release_lease(f"assignment:{assignment['assignment_id']}", attempt["attempt_id"], fence)
    # The successor claim is already durable; an old spool must never win
    # selection, even if its numeric checkpoint sequence is larger.
    successor, next_fence = role.start_attempt(assignment["assignment_id"])
    role.repository.leases.release_lease(f"assignment:{assignment['assignment_id']}", successor["attempt_id"], next_fence)
    restart(role)
    assert not role.repository.role_maintenance.reconcile_retired_role_checkpoint("session-router")
    assert LogicalCoroutineCheckpointStore(role.runtime_root).read("session-router") == initial
