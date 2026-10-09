"""Recovery must receive diagnostic values, not just their shapes."""

import copy
import json
import asyncio
from unittest.mock import patch
from types import SimpleNamespace

from pal.core.turns import ToolObservation, _render_failure_primary_input
from pal.failure import FailureDraft
from tests.test_failure_flow import _core_with_failure_runtime, _ToolLoopFailureLLM


def test_failure_packet_preserves_diagnostic_values_and_complete_causes():
    state = {
        "enabled": False, "healthy": True, "returncode": 0,
        "elapsed": 0.0, "missing": None, "empty": "",
        "values": [False, True, 0, 0.0, None, ""],
        "items": [{"index": index, "ok": index != 19} for index in range(20)],
        "nested": {"cause": {"code": "EACCES", "path": "/tmp/report"}},
        "message": "context " * 100 + "ROOT CAUSE: permission denied",
        "fields": {f"field_{index}": index for index in range(20)},
        "tools": [{"name": "probe", "available": False}],
        "capability": {"name": "probe", "error": "backend unavailable"},
        "payload": {"nested": False},
    }
    original = copy.deepcopy(state)
    draft = FailureDraft(
        subsystem="execution", component="probe", failure_kind="capability_failure",
        severity="medium", primary_blocker="probe failed", evidence=state,
        maintenance_outcomes=[{
            "action_name": "probe", "status": "error", "ok": False,
            "text": state["message"], "structured": state,
        }],
    )
    packet = json.loads(_render_failure_primary_input(
        draft, allowed_tools=[], stage="verify",
        observations=[ToolObservation(
            tool_name="probe", ok=False, summary=state["message"], structured=state,
        )],
    ))
    for projected in (
        packet["failure"]["evidence"],
        packet["failure"]["maintenance_outcomes"][0]["structured_summary"],
        packet["recent_observations"][0]["structured_summary"],
    ):
        assert json.dumps(projected) == json.dumps(original)
    assert packet["recent_observations"][0]["summary"] == state["message"]
    assert packet["failure"]["maintenance_outcomes"][0]["text"] == state["message"]
    assert state == original


def test_failure_packet_preserves_nested_cause_while_redacting_credentials():
    evidence = {"nested": [{"api_key": "PRIVATE_CANARY", "enabled": False,
        "error": "token=PRIVATE_CANARY; backend unavailable"}]}
    draft = FailureDraft(
        subsystem="execution", component="probe", failure_kind="capability_failure",
        severity="medium", primary_blocker="probe failed", evidence=evidence,
    )
    rendered = _render_failure_primary_input(draft, allowed_tools=[], observations=[], stage="diagnose")
    assert "PRIVATE_CANARY" not in rendered
    result = json.loads(rendered)["failure"]["evidence"]["nested"][0]
    assert result["enabled"] is False
    assert result["error"] == "token=[redacted]; backend unavailable"
    assert evidence["nested"][0]["api_key"] == "PRIVATE_CANARY"


def test_safe_mode_tool_exception_chain_reaches_verification_request():
    core = _core_with_failure_runtime()
    llm = _ToolLoopFailureLLM()
    core.context.port_registry["llm:llm"] = llm
    draft = FailureDraft(
        subsystem="execution", component="probe", failure_kind="capability_failure",
        severity="medium", primary_blocker="probe failed",
    )

    async def fail(*args, **kwargs):
        try:
            raise PermissionError("cannot read report; token=PRIVATE_CANARY")
        except PermissionError as exc:
            raise RuntimeError("transport wrapper failed") from exc

    with patch("pal.execution.runtime.ExecutionRuntime.execute_tool_async", side_effect=fail):
        asyncio.run(core.failure_orchestrator._run_failure_flow_async(draft, allowed_tools=[]))
    assert len(llm.requests) == 3
    for request in llm.requests[1:]:
        packet = json.loads(request.messages[1].text)
        for text in (
            packet["recent_observations"][0]["summary"],
            packet["failure"]["maintenance_outcomes"][0]["text"],
        ):
            assert "PermissionError" in text
            assert "cannot read report" in text
            assert "RuntimeError" in text
            assert "transport wrapper failed" in text
            assert "PRIVATE_CANARY" not in text


def test_safe_mode_model_exception_keeps_root_cause_without_stack():
    core = _core_with_failure_runtime()

    async def fail(*args, **kwargs):
        try:
            raise ConnectionError("endpoint refused connection token=PRIVATE_CANARY")
        except ConnectionError as exc:
            raise RuntimeError("model request failed") from exc

    core.context.port_registry["llm:llm"] = SimpleNamespace(agenerate=fail)
    draft = FailureDraft(
        subsystem="execution", component="probe", failure_kind="capability_failure",
        severity="medium", primary_blocker="probe failed",
    )
    try:
        result = asyncio.run(core.failure_orchestrator._run_failure_flow_async(draft, allowed_tools=[]))
        for text in (result.verification.reason, result.enriched_fields["current_blocker"]):
            assert "endpoint refused connection" in text
            assert "model request failed" in text
            assert "PRIVATE_CANARY" not in text
            assert "Traceback" not in text
    finally:
        core.close()
