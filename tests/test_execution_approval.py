from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, new_tool_call

import asyncio
import unittest

from pal.core import PalCore as _PalCoreBootstrap
from pal.execution.approval import ApprovalExecutionDecorator, ExecutionApprovalRequest
from pal.shared import ToolExecutionResult


class RecordingRuntime:
    def __init__(self) -> None:
        self.calls: list[ToolCallIR] = []

    async def execute_tool_async(self, call: ToolCallIR, **_kwargs) -> ToolExecutionResult:
        self.calls.append(call)
        return ToolExecutionResult(name=call.name, ok=True, text="ok", llm_text="ok", status="ok")


class ApprovalExecutionDecoratorTests(unittest.TestCase):
    def test_executes_delegate_after_acceptance(self) -> None:
        async def scenario() -> None:
            delegate = RecordingRuntime()
            requested: list[str] = []
            runtime = ApprovalExecutionDecorator(
                delegate=delegate,
                classify=lambda _call: ExecutionApprovalRequest(
                    title="Confirm",
                    risk="high",
                    impact="Changes external state",
                ),
                request=lambda request, _call: requested.append(request.risk) or "accept",
            )

            result = await runtime.execute_tool_async(new_tool_call(name="op_test", args={}))

            self.assertTrue(result.ok)
            self.assertEqual(requested, ["high"])
            self.assertEqual([call.name for call in delegate.calls], ["op_test"])

        asyncio.run(scenario())

    def test_rejection_does_not_execute_delegate(self) -> None:
        async def scenario() -> None:
            delegate = RecordingRuntime()
            runtime = ApprovalExecutionDecorator(
                delegate=delegate,
                classify=lambda _call: ExecutionApprovalRequest(
                    title="Confirm",
                    risk="high",
                    impact="Changes external state",
                ),
                request=lambda _request, _call: "reject",
            )

            result = await runtime.execute_tool_async(new_tool_call(name="op_test", args={}))

            self.assertFalse(result.ok)
            self.assertEqual(result.structured["reason"], "approval_not_accepted")
            self.assertEqual(delegate.calls, [])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
