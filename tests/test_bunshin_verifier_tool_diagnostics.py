"""Privacy and pre-handler coverage for ordinary verifier diagnostics."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from pal.bunshin.manager import BunshinManager, BunshinRunState
from pal.bunshin.runner_components.reporter import Reporter
from pal.bunshin.runner_components.tool_observation import ToolObservation
from pal.bunshin.runner_components.tool_session import ToolSession
from pal.bunshin.verifier_tool_diagnostics import (
    VerifierToolDiagnostic, _VERIFIER_ALIASES, verifier_tool_alias,
    verifier_tool_diagnostic,
)
from pal.execution.tool_facade import rejection
from pal.shared import BunshinInvocationPack, ToolExecutionResult
from pal.shared.tool_protocol import new_tool_call


SECRET = "private prompt / credential / exception / output " * 4000
ATTEMPT = "att_" + "a" * 24


def call(name="submit_verification_pass", args=None):
    return new_tool_call(name=name, args=args or {}, call_id="secret-call-id")


def payload(**updates):
    return {
        **VerifierToolDiagnostic(round=7, tool_call_index=2,
            tool_alias="submit_verification_pass", stage="started").model_dump(),
        **updates,
    }


@pytest.mark.parametrize("name,alias", _VERIFIER_ALIASES.items())
@pytest.mark.parametrize("wrapper", [None, "call_tool", "op_tool_call", "read_tool", "op_tool_read"])
def test_only_exact_allowlisted_aliases_and_routes(name, alias, wrapper):
    for target in (name, alias):
        tool = call(wrapper, {"name": target, "args": {"secret": SECRET}}) if wrapper else call(target)
        assert verifier_tool_alias(tool) == alias
        item = verifier_tool_diagnostic(tool, round_index=7, tool_call_index=2, stage="started")
        assert item["tool_alias"] == alias
        assert item["route"] == ("read_tool" if wrapper in {"read_tool", "op_tool_read"}
                                 else "call_tool" if wrapper else "direct")
        assert SECRET not in json.dumps(item)


@pytest.mark.parametrize("name,args", [
    ("submit_verification_pass_secret", {}), (SECRET, {}),
    ("read_file", {}), ("call_tool", {"name": SECRET}),
    ("call_tool", {"name": ["submit_verification_pass"]}),
    ("call_tool", {"args": {"name": "submit_verification_pass"}}),
])
def test_unknown_tools_are_not_recorded(name, args):
    assert verifier_tool_diagnostic(call(name, args), round_index=1,
                                   tool_call_index=0, stage="started") is None


@pytest.mark.parametrize("updates", [
    {"args": SECRET}, {"text_preview": SECRET}, {"prompt": SECRET},
    {"structured": {"private": SECRET}}, {"error": SECRET},
    {"tool_alias": SECRET}, {"status": SECRET}, {"error_code": SECRET},
    {"attempt_id": SECRET}, {"round": True}, {"round": "7"}, {"round": -1},
    {"round": 10**100}, {"tool_call_index": 10**100}, {"tool_call_index": 1.5},
    {"ok": "false"}, {"stage": "secret"}, {"route": SECRET},
])
def test_wire_schema_rejects_unknown_fields_values_types_and_oversize(updates):
    with pytest.raises(ValidationError):
        VerifierToolDiagnostic.model_validate(payload(**updates))


def test_result_projection_does_not_serialize_unknown_content():
    result = ToolExecutionResult(name=SECRET, call_id=SECRET, ok=False,
        status=SECRET, text=SECRET, llm_text=SECRET,
        structured={"error_code": SECRET, "reason": SECRET, "error_type": SECRET,
                    "error": SECRET, "prompt": SECRET, "nested": {"key": SECRET}})
    item = verifier_tool_diagnostic(call(args={"secret": SECRET}), round_index=1,
        tool_call_index=0, stage="completed", result=result)
    assert item["ok"] is False
    assert item["status"] == "unknown"
    assert item["error_code"] == "unclassified_error"
    assert len(json.dumps(item)) < 512
    assert "private" not in json.dumps(item)


@pytest.mark.parametrize("code", ["unknown_tool", "invalid_arguments", "handler_exception"])
def test_structured_facade_error_code_is_preserved_without_details(code):
    invocation = rejection(code, SECRET, details={"error": SECRET})
    result = ToolExecutionResult(name="submit_verification_pass", ok=False,
        llm_text=SECRET, structured=invocation.model_dump(), status=code,
        invocation_result=invocation)
    item = verifier_tool_diagnostic(call(), round_index=1, tool_call_index=0,
                                   stage="completed", result=result)
    assert item["error_code"] == item["status"] == code
    assert len(json.dumps(item)) < 512
    assert "private" not in json.dumps(item)


def test_admission_reason_and_unknown_code_are_bounded():
    for reason, expected in (("capability_not_allowed", "capability_not_allowed"),
                             ({"secret": SECRET}, "unclassified_error")):
        result = ToolExecutionResult(name="submit_verification_pass", ok=False,
            llm_text=SECRET, structured={"reason": reason}, status="error")
        item = verifier_tool_diagnostic(call(), round_index=1, tool_call_index=0,
                                       stage="completed", result=result)
        assert item["error_code"] == expected


@pytest.mark.parametrize("failure", [None, "admission", "routing_exception", "clock_exception"])
def test_observation_records_started_and_terminal_before_handler_or_early_failure(failure):
    async def scenario():
        events = []
        async def write_event(event):
            events.append(event)
        async def heartbeat(operation, **kwargs):
            return await operation
        pack = BunshinInvocationPack(invocation_id="session", metadata={
            "bunshin_v2": {"role": "verifier"}, "prompt_log_enabled": False})
        reporter = Reporter("session", pack, "run", write_event)
        execute = AsyncMock(return_value=ToolExecutionResult(name="submit_verification_pass",
            ok=failure is None, status="ok" if failure is None else "error", llm_text=SECRET,
            structured={"reason": "capability_not_allowed", "secret": SECRET}))
        if failure == "routing_exception":
            execute.side_effect = RuntimeError(SECRET)
        clock = Mock(side_effect=RuntimeError(SECRET) if failure == "clock_exception" else None)
        observation = ToolObservation(SimpleNamespace(await_with_progress_heartbeat=heartbeat),
            reporter, SimpleNamespace(execute_allowed_tool=execute), ToolSession())
        state = SimpleNamespace(execution_runtime=SimpleNamespace(advance_tool_result_clock=clock),
            llm_round_count=7, tool_call_count=0)
        continuation = SimpleNamespace(pending_tool_results=[], turn_id="turn")
        operation = observation.execute_bunshin_tool_with_observation(state, continuation,
            call("call_tool", {"name": "submit_verification_pass", "args": {"secret": SECRET}}))
        if failure in {"routing_exception", "clock_exception"}:
            with pytest.raises(RuntimeError):
                await operation
        else:
            result = await operation
            assert result is execute.return_value
        diagnostics = [event["payload"] for event in events
                       if event["event_kind"] == "verifier_tool_diagnostic"]
        assert [item["stage"] for item in diagnostics] == ["started",
            "failed" if failure in {"routing_exception", "clock_exception"} else "completed"]
        assert diagnostics[-1]["ok"] is (failure is None)
        assert "private" not in json.dumps(diagnostics)
        assert state.tool_call_count == (0 if failure in {"routing_exception", "clock_exception"} else 1)
        if failure == "clock_exception":
            execute.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("debug_enabled", [False, True])
def test_manager_persists_only_typed_payload_and_manager_owned_identity(tmp_path, debug_enabled):
    async def scenario():
        manager = BunshinManager(tmp_path)
        recorded = []
        manager.workflow_service.repository.role_events.record_worker_event = recorded.append
        manager.events.queue_event = Mock()
        state = BunshinRunState(bunshin_id="session", run_id="run", pack=BunshinInvocationPack(
            invocation_id="session", metadata={"bunshin_v2": {"role": "verifier"},
                                              "prompt_log_enabled": debug_enabled}))
        manager.runs["run"] = state
        event = {"event_kind": "verifier_tool_diagnostic", "run_id": "run",
            "invocation_id": SECRET, "_attempt_id": ATTEMPT,
            "unexpected": SECRET, "payload": payload(attempt_id="att_" + "b" * 24)}
        await manager._publish_worker_event(event)
        assert recorded == [{"event_kind": "verifier_tool_diagnostic",
                             "invocation_id": "session", "payload": payload(attempt_id=ATTEMPT)}]
        manager.events.queue_event.assert_not_called()
        assert not state.last_event
        for changes in ({"payload": payload(error=SECRET)}, {"payload": payload(status=SECRET)},
                        {"_attempt_id": SECRET}, {"run_id": "missing"}):
            await manager._publish_worker_event({**event, **changes})
        state.pack.metadata["bunshin_v2"]["role"] = "coder"
        await manager._publish_worker_event(event)
        assert len(recorded) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("tool_fails", [False, True])
def test_diagnostic_transport_failure_does_not_prevent_effect_or_mask_error(tool_fails):
    async def scenario():
        original_error = RuntimeError("original tool exception")
        async def write(event):
            if event["event_kind"] == "verifier_tool_diagnostic":
                raise OSError(SECRET)
        async def heartbeat(operation, **kwargs):
            return await operation
        pack = BunshinInvocationPack(invocation_id="session",
            metadata={"bunshin_v2": {"role": "verifier"}})
        execute = AsyncMock(return_value=ToolExecutionResult(name="submit_verification_pass",
            ok=True, llm_text="done"), side_effect=original_error if tool_fails else None)
        observation = ToolObservation(SimpleNamespace(await_with_progress_heartbeat=heartbeat),
            Reporter("session", pack, "run", write),
            SimpleNamespace(execute_allowed_tool=execute), ToolSession())
        state = SimpleNamespace(execution_runtime=SimpleNamespace(), llm_round_count=7, tool_call_count=0)
        continuation = SimpleNamespace(pending_tool_results=[], turn_id="turn")
        if tool_fails:
            with pytest.raises(RuntimeError) as error:
                await observation.execute_bunshin_tool_with_observation(state, continuation, call())
            assert error.value is original_error
        else:
            assert await observation.execute_bunshin_tool_with_observation(state, continuation, call()) is execute.return_value
        execute.assert_awaited_once()
    asyncio.run(scenario())


def test_diagnostic_cancellation_is_not_swallowed():
    async def scenario():
        async def write(event):
            raise asyncio.CancelledError()
        pack = BunshinInvocationPack(invocation_id="session", metadata={"bunshin_v2": {"role": "verifier"}})
        observation = ToolObservation(None, Reporter("session", pack, "run", write), None, ToolSession())
        with pytest.raises(asyncio.CancelledError):
            await observation.emit_verifier_diagnostic(SimpleNamespace(llm_round_count=7), call(), 0, "started")
    asyncio.run(scenario())


def test_manager_storage_failure_does_not_abort_worker_reader(tmp_path):
    async def scenario():
        manager = BunshinManager(tmp_path)
        record = Mock(side_effect=OSError(SECRET))
        manager.workflow_service.repository.role_events.record_worker_event = record
        manager.runs["run"] = BunshinRunState(bunshin_id="session", run_id="run",
            pack=BunshinInvocationPack(invocation_id="session",
                metadata={"bunshin_v2": {"role": "verifier"}}))
        event = {"event_kind": "verifier_tool_diagnostic", "run_id": "run",
                 "_attempt_id": ATTEMPT, "payload": payload()}
        await manager._publish_worker_event(event)
        record.assert_called_once()
        record.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await manager._publish_worker_event(event)
    asyncio.run(scenario())


def test_real_manager_storage_distinguishes_no_call_from_pre_handler_rejection(tmp_path):
    from pal.bunshin import AggregateType, ContentAddressedArtifactStore
    from pal.bunshin.submission_drafts import AUTHORING_CONTRACT_VERSION

    async def scenario():
        manager = BunshinManager(tmp_path)
        repository = manager.workflow_service.repository
        artifacts = ContentAddressedArtifactStore(tmp_path, repository.artifacts)
        prompt = artifacts.put_json({"prompt": "fixture"}, artifact_type="RolePromptPackArtifact")
        lease = repository.leases.claim_lease("verification:node", "session", ttl_seconds=60)
        repository.role_invocations.record_role_invocation(
            invocation_id="session", workflow_id="workflow",
            aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id="node",
            lease_resource_key=lease.resource_key, fencing_token=lease.fencing_token,
            role="verifier", mode="module", role_profile_id="software_engineering.verifier",
            family_binding_sha="binding", authoring_contract_version=AUTHORING_CONTRACT_VERSION,
            prompt_pack_ref=prompt.to_dict())
        pack = BunshinInvocationPack(invocation_id="session", metadata={
            "bunshin_v2": {"role": "verifier"}, "prompt_log_enabled": False})
        manager.runs["run"] = BunshinRunState(bunshin_id="session", run_id="run", pack=pack)
        manager.events.queue_event = Mock()
        await manager._publish_worker_event({"event_kind": "progress", "run_id": "run",
            "invocation_id": "session", "_attempt_id": ATTEMPT,
            "payload": {"phase": "llm_round_completed", "round": 6, "tool_call_count": 0}})
        with repository.database.read_connection() as connection:
            assert connection.execute("SELECT count(*) FROM bunshin_v2_worker_events "
                "WHERE event_kind='verifier_tool_diagnostic'").fetchone()[0] == 0
        async def write(event):
            await manager._publish_worker_event({**event, "_attempt_id": ATTEMPT})
        async def heartbeat(operation, **kwargs):
            return await operation
        invocation = rejection("invalid_arguments", SECRET, details={"args": SECRET})
        result = ToolExecutionResult(name="submit_verification_pass", ok=False,
            llm_text=SECRET, structured=invocation.model_dump(), status="invalid_arguments",
            invocation_result=invocation)
        observation = ToolObservation(SimpleNamespace(await_with_progress_heartbeat=heartbeat),
            Reporter("session", pack, "run", write),
            SimpleNamespace(execute_allowed_tool=AsyncMock(return_value=result)), ToolSession())
        await observation.execute_bunshin_tool_with_observation(
            SimpleNamespace(execution_runtime=SimpleNamespace(), llm_round_count=7, tool_call_count=0),
            SimpleNamespace(pending_tool_results=[], turn_id="turn"),
            call("call_tool", {"name": "submit_verification_pass", "args": {"private": SECRET}}))
        with repository.database.read_connection() as connection:
            rows = connection.execute("SELECT * FROM bunshin_v2_worker_events "
                "WHERE event_kind='verifier_tool_diagnostic' ORDER BY event_id").fetchall()
            completed = connection.execute("SELECT last_completed_turn FROM bunshin_v2_role_invocations "
                "WHERE invocation_id='session'").fetchone()[0]
        diagnostics = [json.loads(row["payload_json"]) for row in rows]
        assert [item["stage"] for item in diagnostics] == ["started", "completed"]
        assert all(item["attempt_id"] == ATTEMPT and item["round"] == 7 for item in diagnostics)
        assert diagnostics[-1]["error_code"] == "invalid_arguments"
        assert diagnostics[-1]["ok"] is False
        assert all(len(row["payload_json"]) < 512 for row in rows)
        assert "private" not in json.dumps(diagnostics)
        assert completed == 6
    asyncio.run(scenario())
