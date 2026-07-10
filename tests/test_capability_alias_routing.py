from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Any

from pal.core import PalCore as _PalCoreBootstrap
from pal.execution.contracts import CapabilityCall, CapabilityDescriptor, CapabilityResult
from pal.execution.runtime import ExecutionRuntime
from pal.llm.contracts import CanonicalToolCall
from pal.shared import RuntimeStatus, SINGLETON_TARGET


@dataclass
class EchoTool:
    name: str = "op_test_echo"
    description: str = "Echo test input"
    args_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] = field(default_factory=dict)
    display_name: str = "Echo"
    family: str = "test"
    tags: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        value = str(args.get("value") or "")
        return CapabilityResult(status=RuntimeStatus.OK, text=value, llm_text=value or "empty")


def echo_descriptor(*, name: str = "echo", canonical_path: str = "op_test_echo", aliases: tuple[str, ...] = ()) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        name=name,
        canonical_path=canonical_path,
        family="test",
        description="Echo test input",
        source="test",
        aliases=aliases,
    )


def unused_provider_binding(_call: CapabilityCall) -> CapabilityResult:
    return CapabilityResult(status=RuntimeStatus.ERROR, text="wrong binding", llm_text="wrong binding")


class CapabilityAliasRoutingTests(unittest.TestCase):
    def test_alias_and_canonical_path_resolve_to_the_same_binding(self) -> None:
        runtime = ExecutionRuntime()
        runtime.register_tool(EchoTool())
        runtime.register_capability(echo_descriptor(aliases=("repeat",)), unused_provider_binding)
        try:
            alias_path = runtime.resolve_llm_tool_name("repeat")
            canonical_path = runtime.resolve_llm_tool_name("op_test_echo")

            self.assertEqual(alias_path, "op_test_echo")
            self.assertEqual(canonical_path, "op_test_echo")
            self.assertIs(
                runtime.bound_action_index.get(alias_path, SINGLETON_TARGET),
                runtime.bound_action_index.get(canonical_path, SINGLETON_TARGET),
            )
            alias_result = runtime.execute_tool(CanonicalToolCall(name="repeat", args={"value": "alias"}))
            canonical_result = runtime.execute_tool(CanonicalToolCall(name="op_test_echo", args={"value": "canonical"}))
            self.assertEqual(alias_result.text, "alias")
            self.assertEqual(canonical_result.text, "canonical")
            self.assertEqual(alias_result.name, "op_test_echo")
        finally:
            runtime.shutdown()

    def test_unregistered_resident_tool_cannot_bypass_canonical_binding(self) -> None:
        runtime = ExecutionRuntime()
        runtime.register_tool(EchoTool())
        runtime.register_capability(echo_descriptor(aliases=("repeat",)), unused_provider_binding)
        runtime.unregister_capability("echo")
        try:
            self.assertIn("op_test_echo", runtime.tools)
            self.assertEqual(runtime.resolve_llm_tool_name("repeat"), "repeat")
            self.assertIsNone(runtime.bound_action_index.get("op_test_echo", SINGLETON_TARGET))
            alias_result = runtime.execute_tool(CanonicalToolCall(name="repeat", args={"value": "blocked"}))
            canonical_result = runtime.execute_tool(CanonicalToolCall(name="op_test_echo", args={"value": "blocked"}))
            self.assertEqual(alias_result.status, "unknown_tool")
            self.assertEqual(canonical_result.status, "unknown_tool")
        finally:
            runtime.shutdown()

    def test_alias_conflict_fails_without_registering_second_binding(self) -> None:
        runtime = ExecutionRuntime()
        runtime.register_capability(echo_descriptor(aliases=("repeat",)), unused_provider_binding)
        conflicting = echo_descriptor(name="other", canonical_path="op_test_other", aliases=("repeat",))
        try:
            with self.assertRaisesRegex(ValueError, "alias already routes"):
                runtime.register_capability(conflicting, unused_provider_binding)

            self.assertEqual(runtime.resolve_llm_tool_name("repeat"), "op_test_echo")
            self.assertIsNotNone(runtime.bound_action_index.get("op_test_echo", SINGLETON_TARGET))
            self.assertIsNone(runtime.bound_action_index.get("op_test_other", SINGLETON_TARGET))
            self.assertNotIn("other", runtime.compiled_capability_index.records)
        finally:
            runtime.shutdown()

    def test_alias_cannot_shadow_an_existing_canonical_path(self) -> None:
        runtime = ExecutionRuntime()
        runtime.register_capability(echo_descriptor(), unused_provider_binding)
        conflicting = echo_descriptor(name="other", canonical_path="op_test_other", aliases=("op_test_echo",))
        try:
            with self.assertRaisesRegex(ValueError, "alias conflicts with a canonical path"):
                runtime.register_capability(conflicting, unused_provider_binding)

            self.assertEqual(runtime.resolve_llm_tool_name("op_test_echo"), "op_test_echo")
            self.assertIsNotNone(runtime.bound_action_index.get("op_test_echo", SINGLETON_TARGET))
            self.assertIsNone(runtime.bound_action_index.get("op_test_other", SINGLETON_TARGET))
        finally:
            runtime.shutdown()

    def test_canonical_path_cannot_shadow_an_existing_alias(self) -> None:
        runtime = ExecutionRuntime()
        runtime.register_capability(echo_descriptor(aliases=("repeat",)), unused_provider_binding)
        conflicting = echo_descriptor(name="other", canonical_path="repeat")
        try:
            with self.assertRaisesRegex(ValueError, "canonical path is already registered as an alias"):
                runtime.register_capability(conflicting, unused_provider_binding)

            self.assertEqual(runtime.resolve_llm_tool_name("repeat"), "op_test_echo")
            self.assertIsNone(runtime.bound_action_index.get("repeat", SINGLETON_TARGET))
        finally:
            runtime.shutdown()

    def test_exact_instance_descriptor_name_can_be_unregistered(self) -> None:
        runtime = ExecutionRuntime()
        descriptor = CapabilityDescriptor(
            name="echo::worker-a",
            canonical_path="op_test_echo",
            family="test",
            description="Echo for one worker",
            source="test",
            target_id="worker-a",
            aliases=("repeat worker-a",),
        )
        runtime.register_capability(descriptor, unused_provider_binding)
        try:
            self.assertIsNotNone(runtime.get_capability_spec("echo::worker-a"))
            runtime.unregister_capability("echo::worker-a")

            self.assertNotIn("echo::worker-a", runtime.compiled_capability_index.records)
            self.assertIsNone(runtime.bound_action_index.get("op_test_echo", "worker-a"))
            self.assertEqual(runtime.resolve_llm_tool_name("repeat worker-a"), "repeat worker-a")
        finally:
            runtime.shutdown()

    def test_instance_base_name_routes_to_canonical_binding_by_target_id(self) -> None:
        runtime = ExecutionRuntime()
        descriptors = [
            CapabilityDescriptor(
                name=f"echo::{target_id}",
                canonical_path="op_test_echo",
                family="test",
                description="Echo for one worker",
                source="test",
                target_id=target_id,
            )
            for target_id in ("worker-a", "worker-b")
        ]

        def invoke(call: CapabilityCall) -> CapabilityResult:
            return CapabilityResult(
                status=RuntimeStatus.OK,
                text=str(call.args.get("target_id") or ""),
                llm_text=str(call.args.get("target_id") or ""),
            )

        try:
            for descriptor in descriptors:
                runtime.register_capability(descriptor, invoke)

            self.assertEqual(runtime.resolve_llm_tool_name("echo"), "op_test_echo")
            selected = runtime.execute(CapabilityCall(name="echo", args={"target_id": "worker-b"}))
            missing_target = runtime.execute(CapabilityCall(name="echo"))

            self.assertEqual(selected.status, RuntimeStatus.OK)
            self.assertEqual(selected.text, "worker-b")
            self.assertEqual(missing_target.status, RuntimeStatus.INVALID)
            self.assertEqual(missing_target.structured["available_target_ids"], ["worker-a", "worker-b"])
        finally:
            runtime.shutdown()

    def test_explicit_alias_takes_precedence_over_conflicting_instance_base_projection(self) -> None:
        runtime = ExecutionRuntime()
        instance = CapabilityDescriptor(
            name="show::task-a",
            canonical_path="intro_task_show",
            family="test",
            description="Show task",
            source="test",
            target_id="task-a",
        )
        module = CapabilityDescriptor(
            name="show",
            canonical_path="intro_module_show",
            family="test",
            description="Show module",
            source="test",
        )
        try:
            runtime.register_capability(instance, unused_provider_binding)
            runtime.register_capability(module, unused_provider_binding)

            self.assertEqual(runtime.resolve_llm_tool_name("show"), "intro_module_show")
            self.assertEqual(runtime.resolve_llm_tool_name("show::task-a"), "intro_task_show")

            runtime.unregister_capability("show")
            self.assertEqual(runtime.resolve_llm_tool_name("show"), "intro_task_show")
        finally:
            runtime.shutdown()

    def test_unregistered_canonical_path_can_be_reused_as_an_alias(self) -> None:
        runtime = ExecutionRuntime()
        runtime.register_capability(echo_descriptor(), unused_provider_binding)
        runtime.unregister_capability("echo")
        replacement = echo_descriptor(name="other", canonical_path="op_test_other", aliases=("op_test_echo",))
        try:
            runtime.register_capability(replacement, unused_provider_binding)

            self.assertEqual(runtime.resolve_llm_tool_name("op_test_echo"), "op_test_other")
            self.assertNotIn("op_test_echo", runtime.compiled_capability_index.by_canonical)
        finally:
            runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
