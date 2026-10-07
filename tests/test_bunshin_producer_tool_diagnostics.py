"""Content-free producer routing diagnostics, authority, and finite retention."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from pydantic import ValidationError

from pal.bunshin.manager import BunshinManager, BunshinRunState
from pal.bunshin.producer_tool_diagnostics import (
    MAX_PRODUCER_TOOL_DIAGNOSTICS, ProducerToolDiagnostic, _PRODUCER_ALIASES,
    is_producer_pack, producer_tool_alias, producer_tool_diagnostic,
)
from pal.bunshin.runner_components.reporter import Reporter
from pal.bunshin.runner_components.tool_execution import ToolExecution
from pal.bunshin.runner_components.tool_observation import ToolObservation
from pal.bunshin.runner_components.tool_session import ToolSession
from pal.execution.tool_facade import rejection
from pal.shared import BunshinInvocationPack, ToolExecutionResult
from pal.shared.tool_protocol import new_tool_call


SECRET = "PRIVATE_ARGS_COMMAND_PATH_OUTPUT_CREDENTIAL_ERROR_REASONING" * 4000
ATTEMPT = "att_" + "a" * 24
FIELDS = {"phase", "round", "tool_call_index", "tool_alias", "route", "stage",
          "ok", "status", "error_code", "error_type", "attempt_id"}
PRODUCER_BINDINGS = [("implementation", "submit_candidate"), ("architect", "submit_contract")]


def call(name="submit_candidate", args=None):
    return new_tool_call(name=name, args=args or {}, call_id="private-call-id")


def pack(role="implementation", **metadata):
    return BunshinInvocationPack(invocation_id="session", bunshin_profile="bunshin_v2.coder", metadata={
        "bunshin_v2": {"role": role}, "prompt_log_enabled": False, **metadata})


def payload(**updates):
    return {**ProducerToolDiagnostic(round=7, tool_call_index=2,
        tool_alias="submit_candidate", stage="started").model_dump(), **updates}


async def heartbeat(operation, **kwargs):
    return await operation


def observation_fixture(execute, write, *, invocation=None, runtime=None):
    observation = ToolObservation(SimpleNamespace(await_with_progress_heartbeat=heartbeat),
        Reporter("session", invocation or pack(), "run", write),
        SimpleNamespace(execute_allowed_tool=execute), ToolSession())
    state = SimpleNamespace(execution_runtime=runtime or SimpleNamespace(),
                            llm_round_count=7, tool_call_count=0)
    continuation = SimpleNamespace(pending_tool_results=[], turn_id="turn")
    return observation, state, continuation


@pytest.mark.parametrize("canonical,alias", _PRODUCER_ALIASES.items())
@pytest.mark.parametrize("wrapper", [None, "call_tool", "op_tool_call", "read_tool", "op_tool_read"])
def test_exact_allowlisted_aliases_and_routes(canonical, alias, wrapper):
    for name in (canonical, alias):
        tool = call(wrapper, {"name": name, "args": {"private": SECRET}}) if wrapper else call(name)
        assert producer_tool_alias(tool) == alias
        item = producer_tool_diagnostic(tool, round_index=7, tool_call_index=2, stage="started")
        assert item["tool_alias"] == alias
        assert item["route"] == ("read_tool" if wrapper in {"read_tool", "op_tool_read"}
                                 else "call_tool" if wrapper else "direct")
        assert set(item) == FIELDS
        assert "PRIVATE" not in json.dumps(item)


@pytest.mark.parametrize("name,args", [
    ("submit_candidate_private", {}), (SECRET, {}), ("run_shell", {}),
    ("submit_verification_pass", {}), ("search_tools", {"query": "submit_candidate"}),
    ("call_tool", {"name": SECRET}), ("read_tool", {"name": ["submit_candidate"]}),
    ("call_tool", {"args": {"name": "submit_candidate"}}),
])
def test_other_tools_and_discovery_search_are_not_recorded(name, args):
    assert producer_tool_diagnostic(call(name, args), round_index=1,
        tool_call_index=0, stage="started") is None


@pytest.mark.parametrize("updates", [
    *({field: SECRET} for field in ("args", "command", "path", "output", "error",
       "message", "traceback", "reasoning", "provenance", "tool_alias", "status",
       "error_code", "error_type", "attempt_id", "stage", "route", "phase")),
    {"error_type": "UnknownError"}, {"error_type": 1}, {"error_type": ["ValueError"]},
    {"error_type": "ValueError"},
    {"round": True}, {"round": "7"}, {"round": -1}, {"round": 10**100},
    {"tool_call_index": 10**100}, {"tool_call_index": 1.5}, {"ok": "false"},
])
def test_wire_schema_rejects_unbounded_untyped_and_content_fields(updates):
    with pytest.raises(ValidationError):
        ProducerToolDiagnostic.model_validate(payload(**updates))


def test_unknown_result_content_maps_to_fixed_tokens():
    result = ToolExecutionResult(name=SECRET, call_id=SECRET, ok=False, status=SECRET,
        text=SECRET, llm_text=SECRET, structured={"error_code": SECRET, "reason": SECRET,
        "error": SECRET, "error_type": SECRET, "nested": {"secret": SECRET}})
    item = producer_tool_diagnostic(call(args={"private": SECRET}), round_index=1,
        tool_call_index=0, stage="completed", result=result)
    assert item["ok"] is False
    assert item["status"] == "unknown"
    assert item["error_code"] == "unclassified_error"
    assert set(item) == FIELDS and len(json.dumps(item)) < 512
    assert "PRIVATE" not in json.dumps(item)


@pytest.mark.parametrize("code", ["unknown_tool", "invalid_arguments", "handler_exception",
    "checklist_invalid", "candidate_workspace_polluted", "candidate_product_required"])
def test_known_rejection_codes_without_details(code):
    invocation = rejection(code, SECRET, details={"error": SECRET})
    result = ToolExecutionResult(name="submit_candidate", ok=False, status=code,
        llm_text=SECRET, structured=invocation.model_dump(), invocation_result=invocation)
    item = producer_tool_diagnostic(call(), round_index=1, tool_call_index=0,
        stage="completed", result=result)
    assert item["error_code"] == item["status"] == code
    assert "PRIVATE" not in json.dumps(item)


@pytest.mark.parametrize("role", ["coder", "producer", "architect", "verifier", "", None])
def test_only_explicit_implementation_binding_authorizes_diagnostics(role):
    invocation = pack(role)
    assert not is_producer_pack(invocation)
    invocation.metadata["bunshin_v2"] = {"role": "implementation"}
    assert is_producer_pack(invocation)


@pytest.mark.parametrize("role", ["implementation", "architect", "coder", "producer", "reviewer", "verifier", ""])
@pytest.mark.parametrize("alias", [*_PRODUCER_ALIASES.values(), "add_finding", "run_shell"])
def test_worker_authority_is_scoped_to_role_and_exact_alias(role, alias):
    async def scenario():
        invocation = pack(role)
        expected = (role == "architect" and alias == "submit_contract") or (
            role == "implementation" and alias in _PRODUCER_ALIASES.values() and alias != "submit_contract")
        assert is_producer_pack(invocation, alias) is expected
        events = []
        async def write(event):
            events.append(event)
        observation, state, _ = observation_fixture(None, write, invocation=invocation)
        await observation.emit_producer_diagnostic(state, call("call_tool", {"name": alias}), 0, "started")
        assert bool(events) is expected
    asyncio.run(scenario())


@pytest.mark.parametrize("normalized", [False, True])
@pytest.mark.parametrize("error,started,code,error_type", [
    ("validation", False, "invalid_contract_submission", "SubmissionValidationError"),
    (OSError(SECRET), False, "submission_infrastructure_error", "OSError"),
    (sqlite3.OperationalError(SECRET), False, "submission_infrastructure_error", "OperationalError"),
    (TimeoutError(SECRET), True, "submission_outcome_unknown", "TimeoutError"),
    (type("PrivateException", (Exception,), {})(SECRET), False, "submission_infrastructure_error", "other"),
])
def test_contract_submission_error_projection_uses_only_normalized_type_tokens(normalized, error, started, code, error_type):
    from pal.bunshin.submission_errors import SubmissionValidationError, submission_error_result
    from pal.execution.runtime import ExecutionRuntime

    if error == "validation":
        error = SubmissionValidationError(SECRET)
    result = submission_error_result(call("submit_contract"), error,
        submission_started=started, invalid_code="invalid_contract_submission", correction=SECRET)
    if normalized:
        result = ExecutionRuntime._canonical_result_from_invocation("submit_contract", "private", result.invocation_result)
    original = deepcopy(result)
    item = producer_tool_diagnostic(call("call_tool", {"name": "submit_contract", "args": {"private": SECRET}}),
        round_index=7, tool_call_index=2, stage="completed", result=result)
    assert item["ok"] is False
    assert item["error_code"] == code
    assert item["error_type"] == error_type
    assert item["status"] == (code if normalized else "invalid" if code == "invalid_contract_submission" else "error")
    assert set(item) == FIELDS and len(json.dumps(item)) < 512
    assert "PRIVATE" not in json.dumps(item) and "PrivateException" not in json.dumps(item)
    assert result == original


@pytest.mark.parametrize("error_type,expected", [("ValueError", "ValueError"), (SECRET, "other"),
    ("UnknownError", "other"), (None, "other"), ({"error": SECRET}, "other"), (["ValueError"], "other")])
def test_contract_structured_error_type_is_allowlisted_without_typed_result(error_type, expected):
    result = ToolExecutionResult(name=SECRET, ok=False, status=SECRET, text=SECRET, llm_text=SECRET,
        structured={"error_type": error_type, "error_code": SECRET, "error": SECRET})
    item = producer_tool_diagnostic(call("submit_contract"), round_index=1,
        tool_call_index=0, stage="completed", result=result)
    assert item["error_type"] == expected and item["error_code"] == "unclassified_error"
    assert item["status"] == "unknown" and "PRIVATE" not in json.dumps(item)


def test_contract_typed_result_takes_precedence_and_success_has_no_error_type():
    invocation = rejection("invalid_contract_submission", SECRET, details={"error_type": "SubmissionValidationError"})
    result = ToolExecutionResult(name="submit_contract", ok=False, status="invalid_contract_submission",
        llm_text=SECRET, structured={"error_type": "OSError"}, invocation_result=invocation)
    item = producer_tool_diagnostic(call("submit_contract"), round_index=1,
        tool_call_index=0, stage="completed", result=result)
    assert item["error_type"] == "SubmissionValidationError"
    result = ToolExecutionResult(name="submit_contract", ok=True, llm_text=SECRET, structured={"error_type": SECRET})
    item = producer_tool_diagnostic(call("submit_contract"), round_index=1,
        tool_call_index=0, stage="completed", result=result)
    assert item["error_type"] is None


@pytest.mark.parametrize("role,alias", PRODUCER_BINDINGS)
@pytest.mark.parametrize("failure", [None, "admission", "routing_exception", "clock_exception"])
def test_started_and_terminal_include_pre_handler_failures_without_behavior_change(failure, role, alias):
    async def scenario():
        events = []
        async def write(event):
            events.append(event)
        result = ToolExecutionResult(name=alias, ok=failure is None,
            status="ok" if failure is None else "error", llm_text=SECRET,
            structured={"reason": "capability_not_allowed", "private": SECRET})
        error = RuntimeError(SECRET)
        execute = AsyncMock(return_value=result,
                            side_effect=error if failure == "routing_exception" else None)
        clock = Mock(side_effect=error if failure == "clock_exception" else None)
        observation, state, continuation = observation_fixture(execute, write, invocation=pack(role),
            runtime=SimpleNamespace(advance_tool_result_clock=clock))
        operation = observation.execute_bunshin_tool_with_observation(state, continuation,
            call("call_tool", {"name": alias, "args": {"private": SECRET}}))
        if failure in {"routing_exception", "clock_exception"}:
            with pytest.raises(RuntimeError) as raised:
                await operation
            assert raised.value is error
        else:
            assert await operation is result
        diagnostics = [event["payload"] for event in events
                       if event["event_kind"] == "producer_tool_diagnostic"]
        assert [item["stage"] for item in diagnostics] == ["started",
            "failed" if failure in {"routing_exception", "clock_exception"} else "completed"]
        assert diagnostics[-1]["ok"] is (failure is None)
        assert diagnostics[-1]["error_code"] == ("tool_execution_exception"
            if failure in {"routing_exception", "clock_exception"}
            else "capability_not_allowed" if failure == "admission" else "")
        assert "PRIVATE" not in json.dumps(diagnostics)
        assert diagnostics[-1]["error_type"] == ("other" if role == "architect" and failure else None)
        assert state.tool_call_count == (0 if failure in {"routing_exception", "clock_exception"} else 1)
        assert execute.await_count == (0 if failure == "clock_exception" else 1)
    asyncio.run(scenario())


def test_real_facade_distinguishes_execution_discovery_and_rejected_routes():
    from pal.core import PalCore
    from pal.execution import register_with_core
    from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput
    from tests.capability_fixture import mount_test_capability

    async def scenario():
        core = PalCore()
        register_with_core(core.context)
        core.publish_module_capabilities("execution")
        runtime = core.context.execution_runtime
        seen, events = [], []
        mount_test_capability(runtime, alias="submit_candidate", canonical_path="op_test_candidate",
            InputModel=EmptyToolInput, OutputModel=EmptyToolOutput,
            handler=lambda _: seen.append("effect") or {})
        async def write(event):
            events.append(event)
        async def execute(_runtime, tool, **kwargs):
            return await runtime.execute_tool_async(tool, **kwargs)
        observation, state, continuation = observation_fixture(execute, write)
        try:
            cases = [
                (call("read_tool", {"name": "submit_candidate"}), True, "", 0),
                (call(), False, "wrong_invocation_mode", 0),
                (call("call_tool", {"name": "submit_candidate", "args": {"private": SECRET}}),
                 False, "invalid_arguments", 0),
                (call("call_tool", {"name": "submit_candidate", "args": {}}), True, "", 1),
            ]
            for tool, ok, error_code, effects in cases:
                result = await observation.execute_bunshin_tool_with_observation(state, continuation, tool)
                assert result.ok is ok
                item = [event["payload"] for event in events
                        if event["event_kind"] == "producer_tool_diagnostic"][-1]
                assert item["stage"] == "completed" and item["ok"] is ok
                assert item["error_code"] == error_code
                assert item["route"] == (tool.name if tool.name in {"read_tool", "call_tool"} else "direct")
                assert len(seen) == effects
                assert "PRIVATE" not in json.dumps(item)
        finally:
            runtime.shutdown()
    asyncio.run(scenario())


def test_real_admission_rejection_is_observed_before_runtime_execution():
    async def scenario():
        events = []
        async def write(event):
            events.append(event)
        invocation = pack()
        status = Mock()
        session = ToolSession()
        execution = ToolExecution(None, None, None, status, session, invocation, "run")
        runtime = SimpleNamespace(allowed_capabilities=[], execute_tool_async=AsyncMock())
        observation, state, continuation = observation_fixture(execution.execute_allowed_tool,
            write, invocation=invocation, runtime=runtime)
        result = await observation.execute_bunshin_tool_with_observation(state, continuation, call())
        assert result.ok is False
        assert result.structured["reason"] == "capability_not_allowed"
        runtime.execute_tool_async.assert_not_awaited()
        diagnostics = [event["payload"] for event in events if event["event_kind"] == "producer_tool_diagnostic"]
        assert diagnostics[-1]["error_code"] == "capability_not_allowed"
    asyncio.run(scenario())


@pytest.mark.parametrize("role,alias", PRODUCER_BINDINGS)
@pytest.mark.parametrize("tool_fails", [False, True])
@pytest.mark.parametrize("telemetry_fails", ["projection", "transport"])
def test_best_effort_telemetry_does_not_repeat_effect_or_mask_error(tool_fails, telemetry_fails, role, alias):
    async def scenario():
        error = RuntimeError("original tool failure")
        async def write(event):
            if telemetry_fails == "transport" and event["event_kind"] == "producer_tool_diagnostic":
                raise OSError(SECRET)
        execute = AsyncMock(return_value=ToolExecutionResult(name=alias, ok=True, llm_text="done"),
            side_effect=error if tool_fails else None)
        observation, state, continuation = observation_fixture(execute, write, invocation=pack(role))
        original = producer_tool_diagnostic
        with patch("pal.bunshin.runner_components.tool_observation.producer_tool_diagnostic",
                   side_effect=RuntimeError(SECRET) if telemetry_fails == "projection" else original):
            if tool_fails:
                with pytest.raises(RuntimeError) as raised:
                    await observation.execute_bunshin_tool_with_observation(state, continuation, call(alias))
                assert raised.value is error
            else:
                assert await observation.execute_bunshin_tool_with_observation(state, continuation, call(alias)) is execute.return_value
        execute.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize("role,alias", PRODUCER_BINDINGS)
@pytest.mark.parametrize("transport_fails", [False, True])
def test_ninety_repeats_bound_emission_without_limiting_execution(transport_fails, role, alias):
    async def scenario():
        emitted = []
        async def write(event):
            if event["event_kind"] == "producer_tool_diagnostic":
                emitted.append(event["payload"])
                if transport_fails:
                    raise OSError(SECRET)
        execute = AsyncMock(return_value=ToolExecutionResult(name=alias, ok=False, status="invalid", llm_text="invalid"))
        observation, state, continuation = observation_fixture(execute, write, invocation=pack(role))
        for index in range(90):
            state.llm_round_count = index
            await observation.execute_bunshin_tool_with_observation(state, continuation, call(alias))
        assert len(emitted) == MAX_PRODUCER_TOOL_DIAGNOSTICS
        assert emitted[-1]["stage"] == "completed"
        assert execute.await_count == state.tool_call_count == 90
    asyncio.run(scenario())


@pytest.mark.parametrize("role,alias", PRODUCER_BINDINGS)
@pytest.mark.parametrize("debug_enabled", [False, True])
def test_manager_owns_identity_and_rejects_wrong_roles_and_untyped_payloads(tmp_path, debug_enabled, role, alias):
    async def scenario():
        manager = BunshinManager(tmp_path)
        recorded = []
        manager.v2_service.repository.role_events.record_worker_event = recorded.append
        manager.events.queue_event = Mock()
        state = BunshinRunState(bunshin_id="session", run_id="run",
            pack=pack(role, prompt_log_enabled=debug_enabled))
        manager.runs["run"] = state
        event = {"event_kind": "producer_tool_diagnostic", "run_id": "run", "invocation_id": SECRET,
            "_attempt_id": ATTEMPT, "_owner_run_id": "run", "unexpected": SECRET,
            "payload": payload(tool_alias=alias, attempt_id="att_" + "b" * 24)}
        await manager._publish_v2_worker_event(event)
        assert recorded == [{"event_kind": "producer_tool_diagnostic", "invocation_id": "session",
                             "payload": payload(tool_alias=alias, attempt_id=ATTEMPT)}]
        for changes in ({"payload": payload(tool_alias=alias, error=SECRET)},
                        {"payload": payload(tool_alias=alias, status=SECRET)},
                        {"payload": payload(tool_alias=alias, error_type=SECRET)},
                        {"payload": payload(tool_alias=alias, error_type="UnknownError")},
                        {"payload": payload(tool_alias="submit_candidate" if role == "architect" else "submit_contract")},
                        {"payload": payload(tool_alias="submit_candidate", error_type="ValueError")},
                        {"_attempt_id": SECRET}, {"_attempt_id": ""}, {"_owner_run_id": "missing"},
                        {"_owner_run_id": None}, {"_owner_run_id": ["run"]}):
            await manager._publish_v2_worker_event({**event, **changes})
        for wrong_role in ("coder", "reviewer", "verifier", "", "implementation" if role == "architect" else "architect"):
            state.pack.metadata["bunshin_v2"]["role"] = wrong_role
            await manager._publish_v2_worker_event(event)
        assert len(recorded) == 1
        assert state.producer_diagnostic_count == 1
        manager.events.queue_event.assert_not_called()
        assert not state.last_event and state.status == "running"
    asyncio.run(scenario())


def test_manager_storage_failure_is_bounded_and_cancellation_propagates(tmp_path):
    async def scenario():
        manager = BunshinManager(tmp_path)
        record = Mock(side_effect=OSError(SECRET))
        manager.v2_service.repository.role_events.record_worker_event = record
        manager.runs["run"] = BunshinRunState(bunshin_id="session", run_id="run", pack=pack())
        event = {"event_kind": "producer_tool_diagnostic", "run_id": "run",
                 "_attempt_id": ATTEMPT, "_owner_run_id": "run", "payload": payload()}
        for _ in range(200):
            await manager._publish_v2_worker_event(event)
        assert record.call_count == MAX_PRODUCER_TOOL_DIAGNOSTICS
        manager.runs["run"].producer_diagnostic_count = 0
        record.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await manager._publish_v2_worker_event(event)
        async def write(event):
            raise asyncio.CancelledError()
        observation, state, _ = observation_fixture(None, write)
        with pytest.raises(asyncio.CancelledError):
            await observation.emit_producer_diagnostic(state, call(), 0, "started")
    asyncio.run(scenario())


@pytest.mark.parametrize("debug_enabled", [False, True])
def test_worker_cannot_forge_manager_git_diagnostics(tmp_path, debug_enabled):
    async def scenario():
        manager = BunshinManager(tmp_path)
        record = Mock()
        manager.v2_service.repository.role_events.record_worker_event = record
        manager.events.queue_event = Mock()
        state = BunshinRunState(bunshin_id="session", run_id="run",
            pack=pack(prompt_log_enabled=debug_enabled))
        manager.runs["run"] = state
        await manager._publish_v2_worker_event({"event_kind": "git_gateway_diagnostic",
            "run_id": "run", "_attempt_id": ATTEMPT, "_owner_run_id": "run", "payload": {"private": SECRET}})
        record.assert_not_called()
        manager.events.queue_event.assert_not_called()
        assert not state.last_event
    asyncio.run(scenario())


@pytest.mark.parametrize("owner_role,alias,accepted", [
    ("implementation", "submit_candidate", True), ("architect", "submit_contract", True),
    ("implementation", "submit_contract", False), ("architect", "submit_candidate", False),
    ("verifier", "submit_contract", False), ("verifier", "submit_candidate", False),
])
def test_process_owner_binds_producer_identity_despite_forged_worker_run(tmp_path, monkeypatch, owner_role, alias, accepted):
    from pal.bunshin.semantic_orchestration import attempt_worker_execution as execution_module

    async def scenario():
        manager = BunshinManager(tmp_path)
        recorded = []
        manager.v2_service.repository.role_events.record_worker_event = recorded.append
        source_pack = BunshinInvocationPack(invocation_id="source-session", metadata={
            "bunshin_v2": {"role": owner_role}})
        manager.runs["source-run"] = BunshinRunState("source-session", "source-run", source_pack)
        manager.runs["target-run"] = BunshinRunState("target-session", "target-run", pack())
        forged = {"kind": "event", "event": {"event_kind": "producer_tool_diagnostic",
            "run_id": "target-run", "invocation_id": "target-session",
            "_owner_run_id": "target-run", "_attempt_id": "att_" + "b" * 24,
            "payload": payload(tool_alias=alias, attempt_id="att_" + "c" * 24)}}
        async def lines():
            yield json.dumps(forged).encode()
        owner = SimpleNamespace(stdout_lines=lines, wait=AsyncMock())
        monkeypatch.setattr(execution_module, "WorkerProcessOwner", lambda **kwargs: owner)
        @asynccontextmanager
        async def shell(*args, **kwargs):
            yield
        repository = MagicMock(runtime_root=tmp_path)
        execution = execution_module.WorkerExecution(MagicMock(), manager._publish_v2_worker_event,
            None, repository, MagicMock(), SimpleNamespace(process_shell=shell), None, MagicMock())
        command = SimpleNamespace(effect={}, fencing_token=1, invocation_id="source-session",
            lease_resource="lease", snapshot=MagicMock())
        admission = SimpleNamespace(assignment_lease=SimpleNamespace(fencing_token=1),
            assignment_lease_resource="assignment", attempt={"assignment_id": "assignment", "attempt_id": ATTEMPT})
        publication = SimpleNamespace(argv=[], env={}, pack=SimpleNamespace(workspace={}))
        exited = await execution.execute(command, admission, publication,
            SimpleNamespace(role=owner_role, run_id="source-run"))
        assert exited.events[0]["_owner_run_id"] == "source-run"
        assert exited.events[0]["_attempt_id"] == ATTEMPT
        assert manager.runs["target-run"].producer_diagnostic_count == 0
        if accepted:
            assert recorded == [{"event_kind": "producer_tool_diagnostic",
                "invocation_id": "source-session", "payload": payload(tool_alias=alias, attempt_id=ATTEMPT)}]
        else:
            assert not recorded
    asyncio.run(scenario())


@pytest.mark.parametrize("role,alias", PRODUCER_BINDINGS)
def test_durable_retention_survives_manager_reconstruction_without_affecting_progress(tmp_path, role, alias):
    from pal.bunshin import AggregateType, ContentAddressedArtifactStore
    from pal.bunshin.submission_drafts import AUTHORING_CONTRACT_VERSION

    async def scenario():
        manager = BunshinManager(tmp_path)
        repository = manager.v2_service.repository
        artifacts = ContentAddressedArtifactStore(tmp_path, repository.artifacts)
        prompt = artifacts.put_json({"prompt": "fixture"}, artifact_type="RolePromptPackArtifact")
        lease = repository.leases.claim_lease(f"{role}:node", "session", ttl_seconds=60)
        repository.role_invocations.record_role_invocation(
            invocation_id="session", workflow_id="workflow", aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node", lease_resource_key=lease.resource_key, fencing_token=lease.fencing_token,
            role=role, mode="produce", role_profile_id="software_engineering.coder",
            family_binding_sha="binding", authoring_contract_version=AUTHORING_CONTRACT_VERSION,
            prompt_pack_ref=prompt.to_dict())
        for cycle in range(2):
            manager = BunshinManager(tmp_path)
            manager.runs["run"] = BunshinRunState(bunshin_id="session", run_id="run", pack=pack(role))
            for index in range(180):
                await manager._publish_v2_worker_event({"event_kind": "producer_tool_diagnostic",
                    "run_id": "run", "invocation_id": SECRET, "_attempt_id": ATTEMPT, "_owner_run_id": "run",
                    "payload": payload(tool_alias=alias, round=index,
                        stage="started" if index % 2 == 0 else "completed",
                        error_type="SubmissionValidationError" if role == "architect" and index % 2 else None)})
            await manager._publish_v2_worker_event({"event_kind": "progress", "run_id": "run",
                "invocation_id": "session", "_attempt_id": ATTEMPT, "_owner_run_id": "run",
                "payload": {"phase": "llm_round_completed", "round": 90 + cycle, "tool_call_count": 90}})
        with repository.database.read_connection() as connection:
            rows = connection.execute("SELECT payload_json FROM bunshin_v2_worker_events "
                "WHERE event_kind='producer_tool_diagnostic' ORDER BY event_id").fetchall()
            completed = connection.execute("SELECT last_completed_turn FROM bunshin_v2_role_invocations "
                "WHERE invocation_id='session'").fetchone()[0]
        assert len(rows) == MAX_PRODUCER_TOOL_DIAGNOSTICS
        assert completed == 91
        diagnostics = [json.loads(row["payload_json"]) for row in rows]
        assert all(set(item) == FIELDS and item["attempt_id"] == ATTEMPT for item in diagnostics)
        assert all(item["tool_alias"] == alias for item in diagnostics)
        assert all(len(row["payload_json"]) < 512 for row in rows)
        assert "PRIVATE" not in json.dumps(diagnostics)
    asyncio.run(scenario())
