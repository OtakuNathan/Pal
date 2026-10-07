"""Only real process results may become native Git exit codes at the gateway."""
from __future__ import annotations

import asyncio
import io
import json
import socket
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import msgpack
import pytest

from pal.bunshin import git_shim
from pal.bunshin.v2.role_gateway import RoleAssignmentGateway
from pal.bunshin.v2.submission_errors import role_gateway_error_kind
from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import ToolExecutionError, ToolRejectedError
from pal.foundation.sidecar import dispatch_sidecar_request
from pal.shared import RuntimeStatus


PRIVATE = "PRIVATE_COMMAND_PATH_TOKEN_OUTPUT_REASON"


def gateway_fixture(workspace, *, scope="src"):
    events = []
    gateway = RoleAssignmentGateway(SimpleNamespace(
        repository=SimpleNamespace(
            role_attempts=SimpleNamespace(read_role_attempt=Mock(
                return_value={"prompt_pack_ref": {"sha256": "fixture"}})),
            role_events=SimpleNamespace(record_worker_event=events.append),
        ),
        artifacts=SimpleNamespace(read_json=Mock(return_value={
            "workspace": {"repo_path": str(workspace), "write_path_scopes": [scope]},
        })),
    ))
    gateway.authorize = Mock(return_value={
        "attempt_id": "att_" + "a" * 24,
        "assignment": {"session_id": "inv_" + "b" * 24, "role": "implementation"},
    })
    return gateway, events


def invoke(gateway, command="status --short"):
    return gateway.call("git_read", {"cmd": command, "access_token": PRIVATE})


def assert_failed_without_process(events):
    assert [event["payload"]["stage"] for event in events] == ["started", "failed"]
    event = events[-1]["payload"]
    assert event["returncode"] is None
    assert not any(event["response_has_" + name] for name in ("returncode", "stdout", "stderr"))
    assert PRIVATE not in json.dumps(events)


def test_scoped_tool_refusal_is_not_a_native_exit_code(tmp_path):
    gateway, events = gateway_fixture(tmp_path)
    result = CapabilityResult(status=RuntimeStatus.FORBIDDEN, llm_text=PRIVATE,
        structured={"error_code": "GIT_COMMAND_BLOCKED"})
    # A wrapper refusal must stay on the error path even when it returns no
    # process fields. Literal Manager paths are covered by the scoped-read tests.
    with patch("pal.execution.git_tool._run_git") as run_git, patch(
        "pal.bunshin.v2.role_gateway.GitTool._invoke_scoped_read", return_value=result,
    ):
        with pytest.raises(ToolRejectedError, match="rejected.*before execution") as caught:
            invoke(gateway)
    run_git.assert_not_called()
    assert caught.value.error_code == "GIT_COMMAND_BLOCKED"
    assert caught.value.details == {}
    assert PRIVATE not in str(caught.value)
    assert_failed_without_process(events)


@pytest.mark.parametrize("status", [RuntimeStatus.FORBIDDEN, RuntimeStatus.INVALID])
def test_rejection_status_cannot_be_overridden_by_process_fields(tmp_path, status):
    gateway, events = gateway_fixture(tmp_path)
    result = CapabilityResult(status=status, llm_text=PRIVATE, structured={
        "error_code": "GIT_COMMAND_BLOCKED", "returncode": 0, "stdout": PRIVATE, "stderr": "",
    })
    with patch("pal.bunshin.v2.role_gateway.GitTool._invoke_scoped_read", return_value=result) as invoke_tool:
        with pytest.raises(ToolRejectedError):
            invoke(gateway)
    invoke_tool.assert_called_once()
    assert_failed_without_process(events)


@pytest.mark.parametrize("structured", [
    None, {}, {"error_code": "tool_failed"},
    {"stdout": "", "stderr": ""},
    {"returncode": 0, "stderr": ""},
    {"returncode": 0, "stdout": ""},
    {"returncode": None, "stdout": "", "stderr": ""},
    {"returncode": True, "stdout": "", "stderr": ""},
    {"returncode": "1", "stdout": "", "stderr": ""},
    {"returncode": 0, "stdout": None, "stderr": ""},
    {"returncode": 0, "stdout": "", "stderr": b""},
])
def test_missing_or_malformed_process_result_is_not_fabricated(tmp_path, structured):
    gateway, events = gateway_fixture(tmp_path)
    result = CapabilityResult(status=RuntimeStatus.ERROR, llm_text=PRIVATE, structured=structured)
    with patch("pal.bunshin.v2.role_gateway.GitTool._invoke_scoped_read", return_value=result):
        with pytest.raises(ToolExecutionError, match="valid process result") as caught:
            invoke(gateway)
    assert caught.value.error_code == "git_read_invalid_result"
    assert PRIVATE not in str(caught.value)
    assert_failed_without_process(events)


def test_non_process_status_cannot_become_native_success(tmp_path):
    gateway, events = gateway_fixture(tmp_path)
    result = CapabilityResult(status=RuntimeStatus.QUEUED, llm_text=PRIVATE, structured={
        "returncode": 0, "stdout": "", "stderr": "",
    })
    with patch("pal.bunshin.v2.role_gateway.GitTool._invoke_scoped_read", return_value=result):
        with pytest.raises(ToolExecutionError):
            invoke(gateway)
    assert_failed_without_process(events)


@pytest.mark.parametrize("code,stdout,stderr", [
    (0, "", ""), (0, "é\n", "warning\n"),
    (1, "", ""), (128, "partial\n", "fatal\n"), (-9, "", "interrupted\n"),
])
def test_native_process_fields_survive_without_coercion(tmp_path, code, stdout, stderr):
    gateway, events = gateway_fixture(tmp_path)
    result = CapabilityResult(
        status=RuntimeStatus.OK if code == 0 else RuntimeStatus.ERROR, llm_text=PRIVATE,
        structured={"returncode": code, "stdout": stdout, "stderr": stderr,
                    "classification": {"operation_kind": "read"}},
    )
    with patch("pal.bunshin.v2.role_gateway.GitTool._invoke_scoped_read", return_value=result):
        response = invoke(gateway)
    assert response == result.structured
    assert events[-1]["payload"]["stage"] == "returned"
    assert events[-1]["payload"]["returncode"] == code
    assert PRIVATE not in json.dumps(events)


@pytest.mark.parametrize("command,code", [
    ("status --short", 0), ("grep missing_pattern", 1),
])
def test_real_git_empty_success_and_no_match_remain_native(tmp_path, command, code):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    gateway, events = gateway_fixture(tmp_path)
    result = invoke(gateway, command)
    assert result["returncode"] == code
    assert result["stdout"] == result["stderr"] == ""
    assert events[-1]["payload"]["stage"] == "returned"


@pytest.mark.parametrize("command,expected_code,refuse", [
    (["status", "--short"], git_shim.GIT_TRAP_EXIT_CODE, True),
    (["status", "--short"], 0, False),
    (["grep", "missing_pattern"], 1, False),
    (["status", "--definitely-not-a-git-option"], 129, False),
])
def test_gateway_rpc_shim_roundtrip_preserves_refusal_and_native_results(
    tmp_path, command, expected_code, refuse,
):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "untracked.txt").write_text("fixture\n", encoding="utf-8")
    gateway, events = gateway_fixture(tmp_path)
    client_socket, server_socket = socket.socketpair()
    responses = []
    errors = []

    async def call(method, params):
        if refuse:
            result = CapabilityResult(status=RuntimeStatus.FORBIDDEN, llm_text=PRIVATE,
                structured={"error_code": "GIT_COMMAND_BLOCKED"})
            with patch("pal.bunshin.v2.role_gateway.GitTool._invoke_scoped_read", return_value=result):
                return gateway.call(method, params)
        return gateway.call(method, params)

    def serve():
        try:
            with server_socket:
                server_socket.settimeout(2)
                size = int.from_bytes(git_shim._recv_exact(server_socket, 4), "big")
                request = msgpack.unpackb(git_shim._recv_exact(server_socket, size), raw=False)
                response = asyncio.run(dispatch_sidecar_request(
                    request, call, error_kind=role_gateway_error_kind,
                ))
                responses.append(response)
                packed = msgpack.packb(response, use_bin_type=True)
                server_socket.sendall(len(packed).to_bytes(4, "big") + packed)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve)
    thread.start()
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        with (
            patch.dict("os.environ", {
                git_shim.PAL_BUNSHIN_RUNTIME_ROOT_ENV: str(tmp_path),
                git_shim.ROLE_GATEWAY_TOKEN_ENV: PRIVATE,
            }),
            patch("pal.bunshin.git_shim.os.getcwd", return_value=str(tmp_path)),
            patch("pal.bunshin.git_shim._open_role_gateway", return_value=client_socket),
            patch("sys.stdout", stdout), patch("sys.stderr", stderr),
        ):
            code = git_shim.main(command)
    finally:
        thread.join(timeout=3)
        client_socket.close()
    assert not thread.is_alive()
    assert errors == []
    assert code == expected_code
    if expected_code == git_shim.GIT_TRAP_EXIT_CODE:
        assert stdout.getvalue() == ""
        assert not responses[0]["ok"] and "result" not in responses[0]
        assert responses[0]["error"]["kind"] == "role_gateway"
        assert "ToolRejectedError" in stderr.getvalue()
        assert "rejected the scoped command before execution" in stderr.getvalue()
        assert PRIVATE not in stderr.getvalue()
        assert_failed_without_process(events)
    else:
        assert responses[0]["ok"]
        result = responses[0]["result"]
        assert result["returncode"] == expected_code
        assert stdout.getvalue() == result["stdout"]
        assert stderr.getvalue() == result["stderr"]
        if expected_code == 0:
            assert "src/" in stdout.getvalue()
        elif expected_code == 129:
            assert stderr.getvalue()
            assert "read-only Git gateway failed" not in stderr.getvalue()
