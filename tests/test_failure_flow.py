from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, new_tool_call

import asyncio
import json
import unittest
from unittest.mock import patch

from pal.core import PalCore, register_with_core as register_core_with_core
from pal.core.turns import _render_failure_primary_input
from pal.execution import CapabilityResult
from pal.execution.tool_facade import EmptyToolInput, StructuredToolOutput, ToolGuidance
from pal.execution.tool_semantics import DIRECT_NONE
from pal.failure import FAILURE_VERIFICATION_FAILED, FailureDraft, FailureSignal
from pal.failure.handler import FailureEventHandler
from pal.foundation import EventEnvelope
from pal.llm import generation_result_from_values
from pal.shared import EventKind, RuntimeStatus, SourceKind
from tests.capability_fixture import mount_test_capability


class _RecordingFailureLLM:
    def __init__(self) -> None:
        self.requests = []

    async def agenerate(self, request):
        self.requests.append(request)
        return generation_result_from_values(
            text=json.dumps(
                {
                    "verification_status": "failed",
                    "reason": "not recovered",
                    "why_blocked": "no safe recovery capability was available",
                    "current_blocker": "runtime blocker remains",
                    "recommended_next_step": "surface the original tool failure",
                }
            )
        )


class _ToolLoopFailureLLM:
    def __init__(self) -> None:
        self.requests = []

    async def agenerate(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return generation_result_from_values(
                text="",
                tool_calls=[new_tool_call(name="safe_probe", args={}, call_id="call_safe_probe")],
                finish_reason="tool_calls",
            )
        return generation_result_from_values(
            text=json.dumps(
                {
                    "verification_status": "failed",
                    "reason": "probe did not recover the runtime",
                    "why_blocked": "safe-mode probe was diagnostic only",
                    "current_blocker": "runtime blocker remains",
                    "recommended_next_step": "surface diagnostic evidence",
                }
            )
        )


class _ExplodingFailureLLM:
    def __init__(self) -> None:
        self.requests = []

    async def agenerate(self, request):
        self.requests.append(request)
        raise RuntimeError("safe-mode llm down")


class _RecordingFailureCore:
    def __init__(self) -> None:
        self.calls = []

    async def handle_failure_async(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _exploding_failure_program(draft, *, allowed_tools):
    _ = (draft, allowed_tools)
    raise RuntimeError("failure program broke")
    yield


def _unknown_effect_failure_program(draft, *, allowed_tools):
    _ = (draft, allowed_tools)
    yield object()


class FailureFlowTests(unittest.TestCase):
    def test_failure_flow_uses_isolated_safe_mode_prompt(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        llm = _RecordingFailureLLM()
        core.context.port_registry["llm:llm"] = llm

        asyncio.run(
            core.handle_failure_async(
                FailureSignal(
                    subsystem="execution",
                    component="run_shell",
                    failure_kind="capability_failure",
                    severity="medium",
                    primary_blocker="tool execution failed: TimeoutExpired",
                    evidence={"tool_name": "run_shell", "error": "TimeoutExpired"},
                ),
                origin="op_tool_call",
                conversation_context={"user_text": "SECRET USER REQUEST"},
            )
        )

        self.assertEqual(len(llm.requests), 1)
        request = llm.requests[0]
        system_text = request.messages[0].text
        user_text = request.messages[1].text

        self.assertIn("Pal Safe Mode", system_text)
        self.assertNotIn("<identity>", system_text)
        self.assertNotIn("<runtime_overlay>", system_text)
        self.assertNotIn("bounded self-healing inside PalCore", system_text)
        self.assertIn("run_shell", user_text)
        self.assertIn("TimeoutExpired", user_text)
        self.assertNotIn("SECRET USER REQUEST", user_text)
        self.assertEqual(request.metadata.get("prompt_profile"), "safe_mode")
        self.assertEqual(request.metadata.get("purpose"), "failure_flow")
        self.assertEqual(request.metadata.get("timeout_seconds"), 45.0)

    def test_socket_session_closed_reply_failure_is_ephemeral(self) -> None:
        core = _RecordingFailureCore()
        handler = FailureEventHandler(core=core)

        asyncio.run(
            handler.handle(
                EventEnvelope(
                    event_kind=EventKind.REPLY_FAILED,
                    source_kind=SourceKind.CHANNEL,
                    payload={
                        "reply_id": "reply-1",
                        "endpoint_id": "sock-1",
                        "reason": "socket session is closed",
                        "permanent": True,
                    },
                ),
                context=None,
            )
        )

        self.assertEqual(core.calls, [])

    def test_non_socket_reply_failure_still_enters_failure_flow(self) -> None:
        core = _RecordingFailureCore()
        handler = FailureEventHandler(core=core)

        asyncio.run(
            handler.handle(
                EventEnvelope(
                    event_kind=EventKind.REPLY_FAILED,
                    source_kind=SourceKind.CHANNEL,
                    payload={
                        "reply_id": "reply-1",
                        "endpoint_id": "tg-1",
                        "reason": "telegram application not running",
                    },
                ),
                context=None,
            )
        )

        self.assertEqual(len(core.calls), 1)

    def test_duplicate_delivery_failures_share_one_failure_flow(self) -> None:
        core = _RecordingFailureCore()
        handler = FailureEventHandler(core=core, duplicate_window_seconds=60.0)
        event = EventEnvelope(
            event_kind=EventKind.REPLY_FAILED,
            source_kind=SourceKind.CHANNEL,
            payload={
                "reply_id": "reply-1",
                "endpoint_id": "tg-1",
                "reason": "adapter_unavailable",
            },
        )

        asyncio.run(handler.handle(event, context=None))
        asyncio.run(
            handler.handle(
                EventEnvelope(
                    event_kind=EventKind.REPLY_FAILED,
                    source_kind=SourceKind.CHANNEL,
                    payload={**event.payload, "reply_id": "reply-2"},
                ),
                context=None,
            )
        )

        self.assertEqual(len(core.calls), 1)

    def test_delivery_failure_flow_can_retry_at_cooldown_boundary(self) -> None:
        handler = FailureEventHandler(
            core=_RecordingFailureCore(),
            duplicate_window_seconds=60.0,
        )
        payload = {
            "reply_id": "reply-1",
            "endpoint_id": "tg-1",
            "reason": "adapter_unavailable",
        }

        with patch(
            "pal.failure.handler.time.monotonic",
            side_effect=(0.0, 60.0),
        ):
            first_duplicate = handler._is_duplicate_delivery_failure(payload)
            boundary_duplicate = handler._is_duplicate_delivery_failure(payload)

        self.assertFalse(first_duplicate)
        self.assertFalse(boundary_duplicate)

    def test_failure_flow_llm_exception_returns_failed_verification(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        llm = _ExplodingFailureLLM()
        core.context.port_registry["llm:llm"] = llm

        result = asyncio.run(
            core.handle_failure_async(
                FailureSignal(
                    subsystem="execution",
                    component="run_shell",
                    failure_kind="capability_failure",
                    severity="medium",
                    primary_blocker="tool execution failed: TimeoutExpired",
                    evidence={"tool_name": "run_shell"},
                ),
                origin="op_tool_call",
                conversation_context={"user_text": "SECRET USER REQUEST"},
            )
        )

        self.assertEqual(result.verification.status, FAILURE_VERIFICATION_FAILED)
        self.assertIn("Failure safe-mode LLM request failed", result.verification.reason)
        self.assertEqual(len(llm.requests), 1)
        request = llm.requests[0]
        self.assertEqual(request.metadata.get("prompt_profile"), "safe_mode")
        self.assertIn("Pal Safe Mode", request.messages[0].text)
        self.assertNotIn("SECRET USER REQUEST", request.messages[1].text)

    def test_failure_flow_outer_guard_catches_orchestration_exception(self) -> None:
        core = PalCore()
        register_core_with_core(core)

        async def explode(draft, *, allowed_tools):
            _ = (draft, allowed_tools)
            raise RuntimeError("safe-mode orchestration broke")

        core.failure_orchestrator._run_failure_flow_async = explode

        result = asyncio.run(
            core.handle_failure_async(
                FailureSignal(
                    subsystem="execution",
                    component="run_shell",
                    failure_kind="capability_failure",
                    severity="medium",
                    primary_blocker="tool execution failed: TimeoutExpired",
                    evidence={"tool_name": "run_shell"},
                ),
                origin="op_tool_call",
            )
        )

        self.assertEqual(result.verification.status, FAILURE_VERIFICATION_FAILED)
        self.assertIn("Failure safe-mode flow crashed during orchestration", result.verification.reason)
        self.assertIsNotNone(result.report)

    def test_failure_flow_program_exception_returns_failed_verification(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        core.context.port_registry["llm:llm"] = _RecordingFailureLLM()
        draft = core.failure_orchestrator.failure_runtime().begin_draft(
            FailureSignal(
                subsystem="execution",
                component="run_shell",
                failure_kind="capability_failure",
                severity="medium",
                primary_blocker="tool execution failed: TimeoutExpired",
                evidence={"tool_name": "run_shell"},
            )
        )

        with patch("pal.core.failure_orchestrator.failure_turn_program", side_effect=_exploding_failure_program):
            outcome = asyncio.run(core.failure_orchestrator._run_failure_flow_async(draft, allowed_tools=[]))

        self.assertEqual(outcome.verification.status, FAILURE_VERIFICATION_FAILED)
        self.assertIn("Failure safe-mode flow crashed during program", outcome.verification.reason)

    def test_failure_flow_unknown_effect_returns_failed_verification(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        core.context.port_registry["llm:llm"] = _RecordingFailureLLM()
        draft = core.failure_orchestrator.failure_runtime().begin_draft(
            FailureSignal(
                subsystem="execution",
                component="run_shell",
                failure_kind="capability_failure",
                severity="medium",
                primary_blocker="tool execution failed: TimeoutExpired",
                evidence={"tool_name": "run_shell"},
            )
        )

        with patch("pal.core.failure_orchestrator.failure_turn_program", side_effect=_unknown_effect_failure_program):
            outcome = asyncio.run(core.failure_orchestrator._run_failure_flow_async(draft, allowed_tools=[]))

        self.assertEqual(outcome.verification.status, FAILURE_VERIFICATION_FAILED)
        self.assertIn("Failure flow yielded unsupported effect: object", outcome.verification.reason)

    def test_failure_flow_tool_loop_stays_inside_safe_mode_projection(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        llm = _ToolLoopFailureLLM()
        core.context.port_registry["llm:llm"] = llm

        def safe_probe(value: EmptyToolInput):
            _ = value
            return CapabilityResult(
                status=RuntimeStatus.OK,
                text="capability definition",
                llm_text="capability definition",
                structured={
                    "capability": {
                        "name": "run_shell",
                        "description": "very long shell tool description",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "cmd": {
                                    "type": "string",
                                    "description": "full schema must not enter verify prompt",
                                }
                            },
                        },
                        "required_params": ["cmd"],
                    }
                },
            )

        mount_test_capability(
            core.context.execution_runtime,
                alias="safe_probe",
                canonical_path="op_test_safe_probe",
                family="failure_test",
                source="test",
                InputModel=EmptyToolInput,
                OutputModel=StructuredToolOutput,
                guidance=ToolGuidance(
                    purpose="Inspect a deterministic safe-mode probe.",
                    use_when="the failure flow needs a side-effect-free diagnostic",
                    do_not_use_when="the runtime is not in this failure-flow test",
                    failure_next_steps="surface the diagnostic failure",
                ),
                execution=DIRECT_NONE,
                handler=safe_probe,
        )
        draft = core.failure_orchestrator.failure_runtime().begin_draft(
            FailureSignal(
                subsystem="execution",
                component="run_shell",
                failure_kind="capability_failure",
                severity="medium",
                primary_blocker="tool execution failed: TimeoutExpired",
                evidence={"tool_name": "run_shell"},
            )
        )

        asyncio.run(
            core.failure_orchestrator._run_failure_flow_async(
                draft,
                allowed_tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "safe_probe",
                            "description": "safe mode diagnostic probe",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            )
        )

        self.assertEqual(len(llm.requests), 3)
        for request in llm.requests:
            self.assertEqual(request.metadata.get("prompt_profile"), "safe_mode")
            self.assertIn("Pal Safe Mode", request.messages[0].text)
            self.assertNotIn("<identity>", request.messages[0].text)
            self.assertNotIn("<runtime_overlay>", request.messages[0].text)
            rendered = json.dumps([message.text for message in request.messages], ensure_ascii=False)
            self.assertNotIn("input_schema", rendered)
            self.assertNotIn("full schema must not enter verify prompt", rendered)

        verify_request = llm.requests[1]
        self.assertEqual(verify_request.tools, ())

        verify_payload = verify_request.messages[1].text
        self.assertIn('"kind": "capability_contract"', verify_payload)
        self.assertIn('"name": "run_shell"', verify_payload)
        maintain_payload = llm.requests[2].messages[1].text
        self.assertIn('"stage": "maintain"', maintain_payload)
        self.assertIn('"kind": "capability_contract"', maintain_payload)
        self.assertIn('"name": "run_shell"', maintain_payload)
        self.assertEqual(draft.attempted_actions, ["safe_probe"])

    def test_failure_packet_projects_maintenance_outcomes_for_llm(self) -> None:
        draft = FailureDraft(
            subsystem="execution",
            component="run_shell",
            failure_kind="capability_failure",
            severity="medium",
            primary_blocker="tool execution failed: TimeoutExpired",
            maintenance_outcomes=[
                {
                    "action_name": "read_tool",
                    "status": "ok",
                    "ok": True,
                    "text": "capability definition",
                    "structured": {
                        "capability": {
                            "name": "run_shell",
                            "description": "very long shell tool description",
                            "input_schema": {
                                "type": "object",
                                "properties": {
                                    "cmd": {
                                        "type": "string",
                                        "description": "do not leak this full schema into safe-mode prompt",
                                    }
                                },
                            },
                            "required_params": ["cmd"],
                        }
                    },
                },
                {
                    "action_name": "module_exec_tools",
                    "status": "ok",
                    "ok": True,
                    "text": "execution tools",
                    "structured": {"tools": [{"name": f"tool_{index}", "description": "large"} for index in range(30)]},
                },
            ],
        )

        payload = json.loads(
            _render_failure_primary_input(
                draft,
                allowed_tools=[],
                observations=[],
                stage="verify",
            )
        )

        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertIn('"kind": "capability_contract"', rendered)
        self.assertIn('"name": "run_shell"', rendered)
        self.assertIn('"kind": "tool_inventory"', rendered)
        self.assertIn('"tool_count": 30', rendered)
        self.assertNotIn("input_schema", rendered)
        self.assertNotIn("do not leak this full schema", rendered)
        self.assertNotIn("very long shell tool description", rendered)


if __name__ == "__main__":
    unittest.main()
