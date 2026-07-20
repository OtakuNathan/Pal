from __future__ import annotations

import asyncio
import time
import unittest
from dataclasses import dataclass, field
from typing import Any

from pal.core import PalCore as _PalCoreBootstrap
from pal.execution.contracts import CapabilityDescriptor, CapabilityResult
from pal.execution.runtime import ExecutionRuntime
from pal.llm.contracts import CanonicalToolCall
from pal.shared import RuntimeStatus


@dataclass
class SlowSyncTool:
    name: str = "op_test_slow_sync"
    description: str = "slow sync test tool"
    args_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] = field(default_factory=dict)
    display_name: str = "Slow Sync"
    family: str = "test"
    tags: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        time.sleep(0.2)
        return CapabilityResult(status=RuntimeStatus.OK, text="ok", llm_text="ok", structured={})


class ExecutionRuntimeAsyncTests(unittest.TestCase):
    def test_execute_tool_async_offloads_sync_tool_fallback(self) -> None:
        async def run() -> None:
            runtime = ExecutionRuntime(sync_executor_max_workers=1)
            runtime.register_tool(SlowSyncTool())
            runtime.register_capability(
                CapabilityDescriptor(
                    name="slow_sync",
                    canonical_path="op_test_slow_sync",
                    family="test",
                    description="slow sync test tool",
                    source="test",
                    metadata={
                        "execution_semantics": {
                            "invocation_mode": "direct",
                            "effect_kind": "none",
                            "idempotency": "idempotent",
                            "retry_policy": "automatic",
                            "paging": "supported",
                        }
                    },
                ),
                lambda _call: CapabilityResult(status=RuntimeStatus.ERROR, text="wrong binder", llm_text="wrong binder"),
            )
            try:
                task = asyncio.create_task(runtime.execute_tool_async(CanonicalToolCall(name="slow_sync", args={})))
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                result = await task
                self.assertTrue(result.ok)
                self.assertEqual(result.text, "ok")
            finally:
                runtime.shutdown()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
