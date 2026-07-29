from __future__ import annotations

import asyncio
import time
import unittest

from pal.core import PalCore as _PalCoreBootstrap
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import (
    EffectKind,
    EmptyToolInput,
    EmptyToolOutput,
    Idempotency,
    InvocationMode,
    PagingMode,
    RetryPolicy,
    ToolExecutionSemantics,
    ToolGuidance,
)
from pal.llm.contracts import CanonicalToolCall
from pal.shared import RuntimeStatus
from tests.capability_fixture import mount_test_capability


def mount_slow_sync_tool(runtime: ExecutionRuntime) -> None:
    def invoke(_args: EmptyToolInput) -> dict[str, object]:
        time.sleep(0.2)
        return {}

    mount_test_capability(
        runtime,
        alias="slow_sync",
        canonical_path="op_test_slow_sync",
        InputModel=EmptyToolInput,
        OutputModel=EmptyToolOutput,
        guidance=ToolGuidance(
            purpose="slow sync test tool",
            use_when="testing sync executor offload",
            do_not_use_when="outside this test",
            failure_next_steps="inspect the test failure",
        ),
        execution=ToolExecutionSemantics(
            invocation_mode=InvocationMode.DIRECT,
            effect_kind=EffectKind.NONE,
            idempotency=Idempotency.IDEMPOTENT,
            retry_policy=RetryPolicy.AUTOMATIC,
            paging=PagingMode.NEVER,
        ),
        search_text="slow sync",
        handler=invoke,
    )


class ExecutionRuntimeAsyncTests(unittest.TestCase):
    def test_execute_tool_async_offloads_sync_tool_fallback(self) -> None:
        async def run() -> None:
            runtime = ExecutionRuntime(sync_executor_max_workers=1)
            mount_slow_sync_tool(runtime)
            try:
                task = asyncio.create_task(runtime.execute_tool_async(CanonicalToolCall(name="slow_sync", args={})))
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                result = await task
                self.assertTrue(result.ok)
                self.assertEqual(result.text, "{}")
            finally:
                runtime.shutdown()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
