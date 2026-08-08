from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, new_tool_call

import unittest
from types import SimpleNamespace

from pal.core import PalCore as _PalCoreBootstrap
from pal.execution.contracts import CapabilityCall, CapabilityResult
from pal.execution.capability_compiler import compile_provider_subtree
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_semantics import INDIRECT_NONE
from pal.execution.tool_facade import (
    CompleteResult,
    EffectKind,
    Idempotency,
    InvocationMode,
    PagingMode,
    RejectedResult,
    RetryPolicy,
    StrictToolModel,
    ToolExecutionSemantics,
    ToolGuidance,
)
from pal.shared import (
    OPERATION_NAMESPACE,
    RuntimeStatus,
    SINGLETON_TARGET,
    capability_action,
    capability_node,
)
from tests.capability_fixture import (
    build_test_capability_handle,
    mount_test_capability,
    unmount_test_capability,
)


class EchoInput(StrictToolModel):
    value: str


class EchoOutput(StrictToolModel):
    echo: str


def echo_capability(
    *,
    alias: str = "echo",
    canonical_path: str = "op_test_echo",
    target_id: str | None = None,
) -> dict[str, object]:
    return dict(
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
    def test_provider_capability_requires_exactly_one_declared_alias(self) -> None:
        @capability_node(
            namespace=OPERATION_NAMESPACE,
            scope="test",
            kind="module",
            source="test",
            target_kind="module",
        )
        class MissingAliasProvider:
            @capability_action(
                namespace=OPERATION_NAMESPACE,
                scope="test",
                action_name="ping",
                execution=INDIRECT_NONE,
            )
            def ping(self, call: CapabilityCall) -> CapabilityResult:
                return echo_handler(EchoInput(value=str(call.args.get("value") or "")))

        @capability_node(
            namespace=OPERATION_NAMESPACE,
            scope="test",
            kind="module",
            source="test",
            target_kind="module",
        )
        class MultipleAliasProvider:
            @capability_action(
                namespace=OPERATION_NAMESPACE,
                scope="test",
                action_name="ping",
                aliases=("ping", "legacy_ping"),
                execution=INDIRECT_NONE,
            )
            def ping(self, call: CapabilityCall) -> CapabilityResult:
                return echo_handler(EchoInput(value=str(call.args.get("value") or "")))

        for provider in (MissingAliasProvider(), MultipleAliasProvider()):
            with self.subTest(provider=provider.__class__.__name__):
                with self.assertRaisesRegex(ValueError, "exactly one non-empty alias"):
                    compile_provider_subtree(
                        provider,
                        module_id="test",
                        lifecycle_scope="runtime",
                        detachable=False,
                    )

    def test_only_exact_alias_is_public_and_canonical_stays_manager_internal(self) -> None:
        runtime = ExecutionRuntime()
        mount_test_capability(runtime, **echo_capability())
        try:
            direct = runtime.execute_tool(new_tool_call(name="echo", args={"value": "direct"}))
            exact = runtime.invoke_indirect_tool(new_tool_call(name="echo", args={"value": "alias"}))
            heuristic = runtime.invoke_indirect_tool(new_tool_call(name="repeat", args={"value": "legacy"}))
            canonical = runtime.invoke_indirect_tool(
                new_tool_call(name="op_test_echo", args={"value": "canonical"})
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

    def test_generation_projects_only_known_aliases_and_never_guesses_unknown_names(self) -> None:
        runtime = ExecutionRuntime()
        mount_test_capability(runtime, **echo_capability())
        try:
            descriptor = runtime.registry_generation.indirect_aliases["echo"].binding.descriptor
            self.assertEqual(descriptor.aliases, ("echo",))
            self.assertEqual(runtime.project_llm_text("Call op_test_echo now."), "Call echo now.")
            self.assertEqual(
                runtime.project_llm_text("Call op_missing_legacy now."),
                "Call [unavailable tool reference] now.",
            )
        finally:
            runtime.shutdown()

    def test_unregister_replaces_generation_without_mutating_old_snapshot(self) -> None:
        runtime = ExecutionRuntime()
        handle = mount_test_capability(runtime, **echo_capability())
        old_generation = runtime.registry_generation
        try:
            unmount_test_capability(runtime, handle)

            self.assertIsNot(runtime.registry_generation, old_generation)
            self.assertIn("echo", old_generation.indirect_aliases)
            self.assertNotIn("echo", runtime.registry_generation.indirect_aliases)
            rejected = runtime.invoke_indirect_tool(new_tool_call(name="echo", args={"value": "gone"}))
            self.assertIsInstance(rejected, RejectedResult)
            self.assertEqual(rejected.error_code, "unknown_tool")
        finally:
            runtime.shutdown()

    def test_generation_wide_alias_conflict_is_atomic(self) -> None:
        runtime = ExecutionRuntime()
        mount_test_capability(runtime, **echo_capability(alias="shared"))
        before = runtime.registry_generation
        conflicting = build_test_capability_handle(
            **echo_capability(
                alias="shared",
                canonical_path="op_test_second",
            )
        )
        try:
            with self.assertRaisesRegex(ValueError, "tool alias conflict"):
                runtime.mount_subtree(conflicting)

            self.assertIs(runtime.registry_generation, before)
            self.assertIn("shared", runtime.registry_generation.indirect_aliases)
            self.assertIsNone(runtime.bound_action_index.get("op_test_second", SINGLETON_TARGET))
        finally:
            runtime.shutdown()

    def test_targeted_capability_exposes_one_name_parameterized_alias(self) -> None:
        @capability_node(
            namespace=OPERATION_NAMESPACE,
            scope="provider",
            kind="provider",
            source="test",
            target_kind="provider",
            iterable_resolver="iter_providers",
            target_id_resolver="provider_name",
            target_label_resolver="provider_name",
        )
        class TargetedProvider:
            @staticmethod
            def iter_providers() -> list[str]:
                return ["worker_a", "worker_b"]

            @staticmethod
            def provider_name(value: str) -> str:
                return value

            @capability_action(
                namespace=OPERATION_NAMESPACE,
                scope="provider",
                action_name="echo",
                aliases=("echo",),
                InputModel=EchoInput,
                OutputModel=EchoOutput,
                execution=INDIRECT_NONE,
                guidance=ToolGuidance(
                    purpose="Echo through one named provider.",
                    use_when="Testing parameterized target routing.",
                    do_not_use_when="No provider name is known.",
                    failure_next_steps="List provider names before retrying.",
                ),
                examples=({"value": "test"},),
            )
            def echo(self, call: CapabilityCall) -> CapabilityResult:
                rendered = f"{call.args['target_id']}:{call.args['value']}"
                return CapabilityResult(
                    status=RuntimeStatus.OK,
                    text=rendered,
                    structured={"echo": rendered},
                    llm_text=rendered,
                )

        runtime = ExecutionRuntime()
        subtree = compile_provider_subtree(
            TargetedProvider(),
            module_id="test",
            lifecycle_scope="runtime",
            detachable=False,
        )
        handle = SimpleNamespace(mounted_subtree=subtree)
        runtime.mount_subtree(handle)
        try:
            selected = runtime.invoke_indirect_tool(
                new_tool_call(name="echo", args={"name": "worker_b", "value": "hello"})
            )
            leaked_alias = runtime.invoke_indirect_tool(
                new_tool_call(name="echo__worker_b", args={"name": "worker_b", "value": "hello"})
            )

            self.assertIsInstance(selected, CompleteResult)
            self.assertEqual(selected.output, {"echo": "worker_b:hello"})
            self.assertEqual(set(runtime.registry_generation.indirect_aliases), {"echo"})
            self.assertEqual(
                runtime.registry_generation.indirect_aliases["echo"].input_schema["required"],
                ["value", "name"],
            )
            self.assertIsInstance(leaked_alias, RejectedResult)
            self.assertEqual(leaked_alias.error_code, "unknown_tool")
        finally:
            runtime.shutdown()

    def test_non_provider_safe_alias_characters_reject_entire_candidate(self) -> None:
        runtime = ExecutionRuntime()
        before = runtime.registry_generation
        try:
            with self.assertRaisesRegex(ValueError, "invalid tool alias"):
                mount_test_capability(
                    runtime,
                    **echo_capability(alias="echo::worker-a", target_id="worker-a"),
                )
            self.assertIs(runtime.registry_generation, before)
        finally:
            runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
