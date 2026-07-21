from __future__ import annotations

import unittest

from pal.core import PalCore as _PalCoreBootstrap
from pal.execution.contracts import CapabilityCall, CapabilityResult
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import (
    CompleteResult,
    EffectKind,
    Idempotency,
    InvocationMode,
    PagingMode,
    RejectedResult,
    RetryPolicy,
    StrictToolModel,
    Tool,
    ToolExecutionSemantics,
    ToolGuidance,
)
from pal.llm.contracts import CanonicalToolCall
from pal.shared import RuntimeStatus, SINGLETON_TARGET


class EchoInput(StrictToolModel):
    value: str


class EchoOutput(StrictToolModel):
    echo: str


def echo_tool(
    *,
    alias: str = "echo",
    canonical_path: str = "op_test_echo",
    target_id: str | None = None,
) -> Tool:
    return Tool(
        alias=alias,
        canonical_path=canonical_path,
        family="test",
        source="test",
        target_id=target_id or SINGLETON_TARGET,
        InputModel=EchoInput,
        OutputModel=EchoOutput,
        guidance=ToolGuidance(
            purpose="Echo test input",
            use_when="testing exact alias routing",
            do_not_use_when="outside this test",
            failure_next_steps="correct the alias or input",
        ),
        execution=ToolExecutionSemantics(
            invocation_mode=InvocationMode.INDIRECT,
            effect_kind=EffectKind.NONE,
            idempotency=Idempotency.IDEMPOTENT,
            retry_policy=RetryPolicy.AUTOMATIC,
            paging=PagingMode.NEVER,
        ),
        search_text="echo exact alias routing test",
        handler=echo_handler,
        examples=({"value": "test"},),
    )


def echo_handler(value: EchoInput) -> CapabilityResult:
    return CapabilityResult(
        status=RuntimeStatus.OK,
        text=value.value,
        structured={"echo": value.value},
        llm_text=value.value,
    )


class CapabilityAliasRoutingTests(unittest.TestCase):
    def test_only_exact_alias_is_public_and_canonical_stays_manager_internal(self) -> None:
        runtime = ExecutionRuntime()
        runtime.register_tool(echo_tool())
        try:
            direct = runtime.execute_tool(CanonicalToolCall(name="echo", args={"value": "direct"}))
            exact = runtime.invoke_indirect_tool(CanonicalToolCall(name="echo", args={"value": "alias"}))
            heuristic = runtime.invoke_indirect_tool(CanonicalToolCall(name="repeat", args={"value": "legacy"}))
            canonical = runtime.invoke_indirect_tool(
                CanonicalToolCall(name="op_test_echo", args={"value": "canonical"})
            )
            manager = runtime.execute(CapabilityCall(name="op_test_echo", args={"value": "internal"}))

            self.assertIsInstance(direct.invocation_result, RejectedResult)
            self.assertEqual(direct.status, "wrong_invocation_mode")
            self.assertIsInstance(exact, CompleteResult)
            self.assertEqual(exact.output, {"echo": "alias"})
            self.assertIsInstance(heuristic, RejectedResult)
            self.assertEqual(heuristic.error_code, "unknown_tool")
            self.assertIsInstance(canonical, RejectedResult)
            self.assertEqual(canonical.error_code, "unknown_tool")
            self.assertEqual(manager.status, RuntimeStatus.OK)
            self.assertEqual(manager.structured, {"echo": "internal"})
        finally:
            runtime.shutdown()

    def test_unregister_replaces_generation_without_mutating_old_snapshot(self) -> None:
        runtime = ExecutionRuntime()
        runtime.register_tool(echo_tool())
        old_generation = runtime.registry_generation
        try:
            runtime.unregister_tool("echo")

            self.assertIsNot(runtime.registry_generation, old_generation)
            self.assertIn("echo", old_generation.indirect_aliases)
            self.assertNotIn("echo", runtime.registry_generation.indirect_aliases)
            rejected = runtime.invoke_indirect_tool(CanonicalToolCall(name="echo", args={"value": "gone"}))
            self.assertIsInstance(rejected, RejectedResult)
            self.assertEqual(rejected.error_code, "unknown_tool")
        finally:
            runtime.shutdown()

    def test_generation_wide_alias_conflict_is_atomic(self) -> None:
        runtime = ExecutionRuntime()
        runtime.register_tool(echo_tool(alias="shared"))
        before = runtime.registry_generation
        conflicting = echo_tool(
            alias="shared",
            canonical_path="op_test_second",
        )
        try:
            with self.assertRaisesRegex(ValueError, "tool alias conflict"):
                runtime.register_tool(conflicting)

            self.assertIs(runtime.registry_generation, before)
            self.assertIn("shared", runtime.registry_generation.indirect_aliases)
            self.assertIsNone(runtime.bound_action_index.get("op_test_second", SINGLETON_TARGET))
        finally:
            runtime.shutdown()

    def test_targeted_aliases_are_exact_and_do_not_create_a_base_fallback(self) -> None:
        runtime = ExecutionRuntime()
        for target_id in ("worker_a", "worker_b"):
            runtime.register_tool(
                echo_tool(
                    alias=f"echo__{target_id}",
                    canonical_path=f"op_test_echo__{target_id}",
                    target_id=target_id,
                )
            )
        try:
            selected = runtime.invoke_indirect_tool(
                CanonicalToolCall(name="echo__worker_b", args={"value": "worker_b"})
            )
            missing_base = runtime.invoke_indirect_tool(
                CanonicalToolCall(name="echo", args={"value": "worker_b"})
            )

            self.assertIsInstance(selected, CompleteResult)
            self.assertEqual(selected.output, {"echo": "worker_b"})
            self.assertIsInstance(missing_base, RejectedResult)
            self.assertEqual(missing_base.error_code, "unknown_tool")
        finally:
            runtime.shutdown()

    def test_non_provider_safe_alias_characters_reject_entire_candidate(self) -> None:
        runtime = ExecutionRuntime()
        before = runtime.registry_generation
        try:
            with self.assertRaisesRegex(ValueError, "invalid tool alias"):
                runtime.register_tool(
                    echo_tool(alias="echo::worker-a", target_id="worker-a")
                )
            self.assertIs(runtime.registry_generation, before)
        finally:
            runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
