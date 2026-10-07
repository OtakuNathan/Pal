"""Authenticated Git telemetry preserves the existing call and contains no content."""
from __future__ import annotations

import asyncio
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from pal.bunshin.git_gateway_diagnostics import (
    GitGatewayDiagnostic,
    MAX_GIT_GATEWAY_DIAGNOSTICS,
    MAX_GIT_DIAGNOSTIC_BYTES,
    MAX_GIT_DIAGNOSTIC_SESSIONS,
)
from pal.bunshin.role_gateway import RoleAssignmentGateway
from pal.execution.contracts import CapabilityResult
from pal.shared import RuntimeStatus


ATTEMPT = "att_" + "a" * 24
SESSION = "inv_" + "b" * 24
SECRET = "PRIVATE_COMMAND_PATH_TOKEN_OUTPUT_ERROR_REASONING"
FIELDS = {
    "attempt_id", "session_id", "subcommand", "stage", "returncode",
    "stdout_bytes", "stderr_bytes", "response_has_returncode",
    "response_has_stdout", "response_has_stderr", "exception_kind",
}


def gateway_fixture(*, response=None, error=None, storage_error=None):
    events = []
    def record(event):
        events.append(event)
        if storage_error:
            raise storage_error
    repository = SimpleNamespace(role_events=SimpleNamespace(record_worker_event=record))
    gateway = RoleAssignmentGateway(SimpleNamespace(repository=repository))
    gateway.authorize = Mock(return_value={
        "attempt_id": ATTEMPT,
        "assignment": {"session_id": SESSION, "role": "implementation"},
    })
    gateway._git_read = Mock(return_value=response, side_effect=error)
    return gateway, events


def invoke(gateway, command="status --short", **params):
    return gateway.call("git_read", {
        "access_token": SECRET, "cmd": command, "cwd": SECRET,
        "attempt_id": SECRET, "session_id": SECRET, **params,
    })


def assert_content_free(events):
    assert SECRET not in json.dumps(events)
    for event in events:
        assert set(event) == {"event_kind", "invocation_id", "payload"}
        assert event["event_kind"] == "git_gateway_diagnostic"
        assert event["invocation_id"] == event["payload"]["session_id"]
        assert set(event["payload"]) == FIELDS
        assert len(json.dumps(event)) < 700


@pytest.mark.parametrize("code", [0, 1, -9])
def test_result_identity_shape_and_unicode_lengths_without_content(code):
    result = {"returncode": code, "stdout": "é" + SECRET, "stderr": SECRET,
              "classification": {"raw": SECRET, "tokens": [SECRET]}, "private": SECRET}
    gateway, events = gateway_fixture(response=result)
    assert invoke(gateway, "git --no-pager status -- " + SECRET) is result
    gateway._git_read.assert_called_once()
    authenticated, params = gateway._git_read.call_args.args
    assert authenticated["attempt_id"] == ATTEMPT
    assert params["cmd"] == "git --no-pager status -- " + SECRET
    assert "access_token" not in params
    assert [e["payload"]["stage"] for e in events] == ["started", "returned"]
    item = events[-1]["payload"]
    assert item["returncode"] == code and item["subcommand"] == "status"
    assert item["stdout_bytes"] == len(result["stdout"].encode("utf-8"))
    assert item["stderr_bytes"] == len(result["stderr"])
    assert all(item["response_has_" + key] for key in ("returncode", "stdout", "stderr"))
    assert item["attempt_id"] == ATTEMPT and item["session_id"] == SESSION
    assert_content_free(events)


@pytest.mark.parametrize("command,subcommand", [
    *((name + " -- " + SECRET, name) for name in (
        "status", "diff", "log", "rev-list", "show", "blame", "branch",
        "grep", "ls-files", "rev-parse")),
    ("/bin/git --no-pager diff", "diff"), ("--no-pager log", "log"),
    ("push " + SECRET, "unknown"), (SECRET, "unknown"), ("'", "unknown"),
    (SECRET * 1000, "unknown"), (None, "unknown"), ([SECRET], "unknown"),
])
def test_subcommand_is_a_fixed_token(command, subcommand):
    gateway, events = gateway_fixture(response={})
    invoke(gateway, command)
    assert [item["payload"]["subcommand"] for item in events] == [subcommand] * 2
    assert_content_free(events)


@pytest.mark.parametrize("error,kind", [
    (ValueError(SECRET), "ValueError"), (PermissionError(SECRET), "PermissionError"),
    (TimeoutError(SECRET), "TimeoutError"), (OSError(SECRET), "OSError"),
    (FileNotFoundError(SECRET), "FileNotFoundError"), (RuntimeError(SECRET), "RuntimeError"),
    (KeyError(SECRET), "KeyError"), (TypeError(SECRET), "TypeError"),
    (subprocess.TimeoutExpired(SECRET, 1, output=SECRET, stderr=SECRET), "TimeoutExpired"),
    (type(SECRET, (ValueError,), {})(SECRET), "other"),
    (KeyboardInterrupt(SECRET), "other"),
    (asyncio.CancelledError(SECRET), "other"),
])
def test_same_exception_propagates_once_without_message_or_dynamic_class(error, kind):
    gateway, events = gateway_fixture(error=error)
    with pytest.raises(type(error)) as raised:
        invoke(gateway)
    assert raised.value is error
    gateway._git_read.assert_called_once()
    assert [item["payload"]["stage"] for item in events] == ["started", "failed"]
    item = events[-1]["payload"]
    assert item["exception_kind"] == kind
    assert item["returncode"] is None
    assert not any(item["response_has_" + key] for key in ("returncode", "stdout", "stderr"))
    assert_content_free(events)


@pytest.mark.parametrize("result", [
    {}, None, {"stdout": None, "stderr": 7},
    {"returncode": True}, {"returncode": SECRET}, {"returncode": 2**100},
])
def test_missing_and_invalid_response_fields_are_only_shape_metadata(result):
    gateway, events = gateway_fixture(response=result)
    assert invoke(gateway) is result
    item = events[-1]["payload"]
    assert item["returncode"] is None
    assert item["stdout_bytes"] == item["stderr_bytes"] == 0
    for key in ("returncode", "stdout", "stderr"):
        assert item["response_has_" + key] is (isinstance(result, dict) and key in result)
    assert_content_free(events)


def test_byte_counts_saturate_without_copying_or_modifying_output():
    result = {"stdout": "😀" * (MAX_GIT_DIAGNOSTIC_BYTES // 4 + 1),
              "stderr": b"x" * (MAX_GIT_DIAGNOSTIC_BYTES + 1)}
    gateway, events = gateway_fixture(response=result)
    assert invoke(gateway) is result
    assert events[-1]["payload"]["stdout_bytes"] == MAX_GIT_DIAGNOSTIC_BYTES
    assert events[-1]["payload"]["stderr_bytes"] == MAX_GIT_DIAGNOSTIC_BYTES
    assert_content_free(events)


@pytest.mark.parametrize("field,value", [
    ("attempt_id", SECRET), ("session_id", SECRET), ("attempt_id", ATTEMPT + "\n"),
    ("returncode", True), ("returncode", 2**31), ("returncode", -(2**31) - 1),
    ("stdout_bytes", -1), ("stderr_bytes", MAX_GIT_DIAGNOSTIC_BYTES + 1),
    ("exception_kind", SECRET), ("subcommand", SECRET), ("stage", SECRET),
    ("response_has_stdout", "true"), ("cmd", SECRET), ("classification", {"raw": SECRET}),
])
def test_schema_rejects_content_unbounded_values_and_wrong_types(field, value):
    payload = {"attempt_id": ATTEMPT, "session_id": SESSION, "stage": "started"}
    with pytest.raises(ValidationError):
        GitGatewayDiagnostic.model_validate({**payload, field: value})


def test_failed_authentication_and_other_methods_cannot_emit_diagnostics():
    gateway, events = gateway_fixture(response={})
    error = ValueError(SECRET)
    gateway.authorize.side_effect = error
    with pytest.raises(ValueError) as raised:
        invoke(gateway)
    assert raised.value is error
    gateway.authorize.side_effect = None
    with pytest.raises(ValueError):
        gateway.call("unknown_method", {"access_token": SECRET})
    gateway._git_read.assert_not_called()
    assert events == []


@pytest.mark.parametrize("role", [
    "verifier", "architect", "architecture_reviewer", "coder", "producer",
    "software_engineering.coder", "software_engineering.v2_coder",
    "implementation ", "Implementation", "", None,
])
def test_only_authenticated_implementation_role_emits_diagnostics(role):
    result = {"returncode": 0, "stdout": SECRET, "stderr": ""}
    gateway, events = gateway_fixture(response=result)
    assignment = gateway.authorize.return_value["assignment"]
    if role is None:
        assignment.pop("role")
    else:
        assignment["role"] = role
    assignment["role_profile_id"] = "software_engineering.v2_coder"
    assert invoke(gateway, role="implementation") is result
    gateway._git_read.assert_called_once()
    assert events == []
    assert gateway._git_diagnostics._counts == {}


@pytest.mark.parametrize("key", ["attempt_id", "session_id"])
def test_invalid_authenticated_identity_suppresses_only_diagnostics(key):
    result = {}
    gateway, events = gateway_fixture(response=result)
    if key == "attempt_id":
        gateway.authorize.return_value[key] = SECRET
    else:
        gateway.authorize.return_value["assignment"][key] = SECRET
    assert invoke(gateway) is result
    gateway._git_read.assert_called_once()
    assert events == []


@pytest.mark.parametrize("failure", [None, ValueError(SECRET)])
def test_storage_failures_remain_bounded_and_preserve_result_or_exception(failure):
    result = {}
    gateway, events = gateway_fixture(response=result, error=failure, storage_error=OSError(SECRET))
    calls = MAX_GIT_GATEWAY_DIAGNOSTICS + 10
    for _ in range(calls):
        if failure:
            with pytest.raises(ValueError) as raised:
                invoke(gateway)
            assert raised.value is failure
        else:
            assert invoke(gateway) is result
    assert gateway._git_read.call_count == calls
    assert len(events) == MAX_GIT_GATEWAY_DIAGNOSTICS
    assert_content_free(events)


def test_projection_failure_does_not_repeat_call_or_replace_result():
    result = {"stdout": SECRET}
    gateway, events = gateway_fixture(response=result)
    with patch("pal.bunshin.git_gateway_diagnostics._byte_count", side_effect=ValueError(SECRET)):
        assert invoke(gateway) is result
    gateway._git_read.assert_called_once()
    assert [item["payload"]["stage"] for item in events] == ["started"]


def test_concurrent_calls_share_one_budget():
    result = {}
    gateway, events = gateway_fixture(response=result)
    calls = MAX_GIT_GATEWAY_DIAGNOSTICS + 20
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(item is result for item in executor.map(lambda _: invoke(gateway), range(calls)))
    assert len(events) == MAX_GIT_GATEWAY_DIAGNOSTICS
    assert gateway._git_read.call_count == calls
    assert_content_free(events)


def test_session_budget_memory_is_bounded_without_eviction():
    gateway, events = gateway_fixture(response={})
    for index in range(MAX_GIT_DIAGNOSTIC_SESSIONS + 3):
        gateway.authorize.return_value["assignment"]["session_id"] = f"inv_{index:024x}"
        invoke(gateway)
    assert len(gateway._git_diagnostics._counts) == MAX_GIT_DIAGNOSTIC_SESSIONS
    assert len(events) == MAX_GIT_DIAGNOSTIC_SESSIONS * 2


def test_existing_git_operation_runs_once_and_raw_classification_is_not_recorded(tmp_path):
    gateway, events = gateway_fixture()
    del gateway._git_read
    gateway.service.repository.role_attempts = SimpleNamespace(
        read_role_attempt=Mock(return_value={"prompt_pack_ref": {"sha256": "fixture"}}))
    gateway.service.artifacts = SimpleNamespace(read_json=Mock(return_value={
        "workspace": {"repo_path": str(tmp_path)}}))
    result = CapabilityResult(status=RuntimeStatus.ERROR, llm_text=SECRET,
        structured={"returncode": 1, "stdout": SECRET,
        "stderr": SECRET, "classification": {"raw": SECRET}})
    plan = object()
    with patch("pal.bunshin.role_gateway.GitTool") as tool, patch(
        "pal.bunshin.role_gateway.scoped_role_git_read_plan", return_value=plan
    ):
        tool.return_value._invoke_scoped_read.return_value = result
        response = invoke(gateway, cwd=str(tmp_path))
        tool.return_value._invoke_scoped_read.assert_called_once_with(plan, cwd=tmp_path)
    assert response == result.structured
    assert events[-1]["payload"]["returncode"] == 1
    assert_content_free(events)


def test_durable_cap_survives_gateway_and_repository_reconstruction(tmp_path):
    from pal.bunshin.contracts import AggregateType
    from pal.bunshin.service import BunshinWorkflowService
    from pal.bunshin.submission_drafts import AUTHORING_CONTRACT_VERSION

    service = BunshinWorkflowService(tmp_path)
    prompt = service.artifacts.put_json({"fixture": True}, artifact_type="RolePromptPackArtifact")
    lease = service.repository.leases.claim_lease("implementation:node", SESSION, ttl_seconds=60)
    service.repository.role_invocations.record_role_invocation(
        invocation_id=SESSION, workflow_id="workflow", aggregate_type=AggregateType.DAG_NODE_RUN,
        aggregate_id="node", lease_resource_key=lease.resource_key, fencing_token=lease.fencing_token,
        role="implementation", mode="produce", role_profile_id="software_engineering.coder",
        family_binding_sha="binding", authoring_contract_version=AUTHORING_CONTRACT_VERSION,
        prompt_pack_ref=prompt.to_dict(),
    )
    for _ in range(2):
        gateway, _events = gateway_fixture(response={"returncode": 0, "stdout": SECRET, "stderr": ""})
        gateway.service = BunshinWorkflowService(tmp_path)
        for _ in range(MAX_GIT_GATEWAY_DIAGNOSTICS):
            invoke(gateway)
    with service.repository.database.read_connection() as connection:
        rows = connection.execute("SELECT invocation_id, payload_json FROM bunshin_v2_worker_events "
            "WHERE event_kind='git_gateway_diagnostic'").fetchall()
        progress = connection.execute("SELECT last_completed_turn FROM bunshin_v2_role_invocations "
            "WHERE invocation_id=?", (SESSION,)).fetchone()[0]
    assert len(rows) == MAX_GIT_GATEWAY_DIAGNOSTICS
    assert all(row["invocation_id"] == SESSION for row in rows)
    assert all(json.loads(row["payload_json"])["attempt_id"] == ATTEMPT for row in rows)
    assert SECRET not in json.dumps([dict(row) for row in rows])
    assert progress == 0
