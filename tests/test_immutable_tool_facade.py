from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from pal.core.runtime import PalCore
from pal.execution.capabilities import register_with_core as register_execution_with_core
from pal.execution.tool_facade import (
    CompleteResult,
    EffectKind,
    EffectOutcome,
    EffectReceipt,
    FailedResult,
    Idempotency,
    InvocationMode,
    PagedResult,
    PagingMode,
    RejectedResult,
    RetryDirective,
    RetryPolicy,
    StrictToolModel,
    Tool,
    ToolExecutionSemantics,
    ToolExecutionError,
    ToolGuidance,
    ToolHandlerResult,
    derive_retry_directive,
)
from pal.llm.contracts import CanonicalToolCall
from pal.execution.tool_registry import _example_from_schema, model_from_json_schema, subtree_for_tool


class EchoInput(StrictToolModel):
    value: str
    shape: Literal["short", "long"] = "short"


class EchoOutput(StrictToolModel):
    echo: str


def tool(
    *,
    alias: str,
    canonical_path: str,
    handler,
    mode: InvocationMode = InvocationMode.DIRECT,
    effect: EffectKind = EffectKind.NONE,
    idempotency: Idempotency = Idempotency.IDEMPOTENT,
    retry: RetryPolicy = RetryPolicy.AUTOMATIC,
) -> Tool:
    return Tool(
        alias=alias,
        canonical_path=canonical_path,
        InputModel=EchoInput,
        OutputModel=EchoOutput,
        guidance=ToolGuidance(
            purpose="Echo one strict string.",
            use_when="The caller needs a deterministic echo.",
            do_not_use_when="The caller needs a file or external operation.",
            failure_next_steps="Correct the input and follow returned affordances.",
        ),
        execution=ToolExecutionSemantics(
            invocation_mode=mode,
            effect_kind=effect,
            idempotency=idempotency,
            retry_policy=retry,
            paging=PagingMode.SUPPORTED,
        ),
        search_text="strict echo string",
        handler=handler,
        examples=({"value": "hello", "shape": "short"},),
        module_id="test_tools",
    )


class ImmutableToolFacadeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.core = PalCore()

    def test_description_contains_semantics_enum_example_and_shapes(self) -> None:
        self.core.context.execution_runtime.register_tool(
            tool(alias="echo", canonical_path="op_test_echo", handler=lambda value: {"echo": value.value})
        )
        record = self.core.context.execution_runtime.registry_generation.direct_aliases["echo"]
        self.assertIn("invocation_mode=direct", record.description)
        self.assertIn("effect_kind=none", record.description)
        self.assertIn('"short"', record.description)
        self.assertIn('"value": "hello"', record.description)
        self.assertIn("Input schema:", record.description)
        self.assertIn("Output shape:", record.description)
        self.assertNotIn("op_test_echo", record.description)

        search_record = self.core.context.execution_runtime.registry_generation.search_records["echo"]
        self.assertEqual(
            set(search_record),
            {"alias", "search_text", "invocation_mode", "input_shape"},
        )

    def test_model_dict_and_json_outputs_use_one_adapter(self) -> None:
        values = [
            EchoOutput(echo="hello"),
            {"echo": "hello"},
            json.dumps({"echo": "hello"}),
        ]
        results = []
        for index, value in enumerate(values):
            runtime = PalCore().context.execution_runtime
            runtime.register_tool(
                tool(
                    alias=f"echo_{index}",
                    canonical_path=f"op_test_echo_{index}",
                    handler=lambda _input, output=value: output,
                )
            )
            result = runtime.execute_tool(CanonicalToolCall(name=f"echo_{index}", args={"value": "hello"}))
            self.assertIsInstance(result.invocation_result, CompleteResult)
            results.append(result.invocation_result.model_dump(mode="json"))
        for result in results[1:]:
            self.assertEqual(result["output"], results[0]["output"])

    def test_strict_input_extra_wrong_type_and_enum_are_rejected(self) -> None:
        runtime = self.core.context.execution_runtime
        runtime.register_tool(tool(alias="echo", canonical_path="op_test_echo", handler=lambda value: {"echo": value.value}))
        for args in (
            {"value": 1},
            {"value": "ok", "extra": True},
            {"value": "ok", "shape": "wide"},
        ):
            result = runtime.execute_tool(CanonicalToolCall(name="echo", args=args))
            self.assertIsInstance(result.invocation_result, RejectedResult)
            self.assertEqual(result.status, "invalid_arguments")
            self.assertEqual(result.invocation_result.effect, EffectOutcome.NOT_STARTED)

    def test_direct_indirect_and_canonical_paths_are_isolated(self) -> None:
        register_execution_with_core(self.core.context)
        self.core.publish_module_capabilities("execution")
        runtime = self.core.context.execution_runtime
        runtime.register_tool(
            tool(
                alias="hidden_echo",
                canonical_path="op_test_hidden_echo",
                handler=lambda value: {"echo": value.value},
                mode=InvocationMode.INDIRECT,
            )
        )
        wrong = runtime.execute_tool(CanonicalToolCall(name="hidden_echo", args={"value": "x"}))
        self.assertEqual(wrong.status, "wrong_invocation_mode")
        called = runtime.execute_tool(
            CanonicalToolCall(name="call_tool", args={"name": "hidden_echo", "args": {"value": "x"}})
        )
        self.assertTrue(called.ok)
        direct_through_call = runtime.execute_tool(
            CanonicalToolCall(name="call_tool", args={"name": "run_shell", "args": {"cmd": "true"}})
        )
        self.assertEqual(direct_through_call.status, "wrong_invocation_mode")
        recursive = runtime.execute_tool(
            CanonicalToolCall(name="call_tool", args={"name": "call_tool", "args": {}})
        )
        self.assertEqual(recursive.status, "wrong_invocation_mode")
        canonical = runtime.execute_tool(CanonicalToolCall(name="op_test_hidden_echo", args={"value": "x"}))
        self.assertEqual(canonical.status, "unknown_tool")

    def test_effect_and_retry_matrix_is_mechanically_derived(self) -> None:
        automatic = ToolExecutionSemantics(
            invocation_mode=InvocationMode.DIRECT,
            effect_kind=EffectKind.LOCAL_READ,
            idempotency=Idempotency.IDEMPOTENT,
            retry_policy=RetryPolicy.AUTOMATIC,
            paging=PagingMode.NEVER,
        )
        reconcile = ToolExecutionSemantics(
            invocation_mode=InvocationMode.DIRECT,
            effect_kind=EffectKind.EXTERNAL_WRITE,
            idempotency=Idempotency.NON_IDEMPOTENT,
            retry_policy=RetryPolicy.RECONCILE_FIRST,
            paging=PagingMode.NEVER,
        )
        never = ToolExecutionSemantics(
            invocation_mode=InvocationMode.DIRECT,
            effect_kind=EffectKind.LOCAL_WRITE,
            idempotency=Idempotency.IDEMPOTENT,
            retry_policy=RetryPolicy.NEVER_AUTOMATIC,
            paging=PagingMode.NEVER,
        )
        cases = (
            (automatic, EffectOutcome.NOT_APPLIED, RetryDirective.SAFE),
            (automatic, EffectOutcome.UNKNOWN, RetryDirective.SAFE),
            (automatic, EffectOutcome.APPLIED, RetryDirective.SAFE),
            (reconcile, EffectOutcome.UNKNOWN, RetryDirective.RECONCILE_FIRST),
            (reconcile, EffectOutcome.APPLIED, RetryDirective.DO_NOT_RETRY),
            (never, EffectOutcome.NOT_APPLIED, RetryDirective.DO_NOT_RETRY),
        )
        for semantics, outcome, expected in cases:
            with self.subTest(outcome=outcome, retry_policy=semantics.retry_policy):
                self.assertEqual(derive_retry_directive(semantics, outcome), expected)
        with self.assertRaises(ValueError):
            ToolExecutionSemantics(
                invocation_mode=InvocationMode.DIRECT,
                effect_kind=EffectKind.EXTERNAL_WRITE,
                idempotency=Idempotency.NON_IDEMPOTENT,
                retry_policy=RetryPolicy.AUTOMATIC,
                paging=PagingMode.NEVER,
            )

    def test_output_validation_failure_is_failed_without_fabricated_output(self) -> None:
        runtime = self.core.context.execution_runtime
        runtime.register_tool(
            tool(
                alias="bad_output",
                canonical_path="op_test_bad_output",
                handler=lambda _value: {"echo": 42},
            )
        )
        result = runtime.execute_tool(
            CanonicalToolCall(name="bad_output", args={"value": "hello"})
        )
        self.assertIsInstance(result.invocation_result, FailedResult)
        self.assertEqual(result.invocation_result.error_code, "output_validation_failed")
        self.assertNotIn("output", result.structured)

    def test_effect_receipt_and_unknown_effect_drive_retry(self) -> None:
        runtime = self.core.context.execution_runtime
        runtime.register_tool(
            tool(
                alias="send_once",
                canonical_path="op_test_send_once",
                handler=lambda _value: {"echo": "sent"},
                effect=EffectKind.EXTERNAL_WRITE,
                idempotency=Idempotency.NON_IDEMPOTENT,
                retry=RetryPolicy.RECONCILE_FIRST,
            )
        )
        missing = runtime.execute_tool(CanonicalToolCall(name="send_once", args={"value": "x"}))
        self.assertIsInstance(missing.invocation_result, FailedResult)
        self.assertEqual(missing.invocation_result.effect, EffectOutcome.UNKNOWN)
        self.assertEqual(missing.invocation_result.retry, RetryDirective.RECONCILE_FIRST)

        runtime.unregister_tool("send_once")
        runtime.register_tool(
            tool(
                alias="send_once",
                canonical_path="op_test_send_once",
                handler=lambda _value: ToolHandlerResult(
                    output={"echo": "sent"},
                    effect_receipt=EffectReceipt(
                        outcome=EffectOutcome.APPLIED,
                        receipt={"request_id": "one"},
                    ),
                ),
                effect=EffectKind.EXTERNAL_WRITE,
                idempotency=Idempotency.NON_IDEMPOTENT,
                retry=RetryPolicy.RECONCILE_FIRST,
            )
        )
        complete = runtime.execute_tool(CanonicalToolCall(name="send_once", args={"value": "x"}))
        self.assertIsInstance(complete.invocation_result, CompleteResult)
        self.assertEqual(complete.invocation_result.effect, EffectOutcome.APPLIED)

    def test_failure_receipt_proves_not_applied_and_allows_safe_retry(self) -> None:
        def fail_after_rejection(_value: EchoInput) -> None:
            raise ToolExecutionError(
                "upstream rejected before applying the write",
                error_code="upstream_rejected",
                effect_receipt=EffectReceipt(
                    outcome=EffectOutcome.NOT_APPLIED,
                    receipt={"request_id": "rejected-one"},
                ),
            )

        runtime = self.core.context.execution_runtime
        runtime.register_tool(
            tool(
                alias="send_once",
                canonical_path="op_test_send_once",
                handler=fail_after_rejection,
                effect=EffectKind.EXTERNAL_WRITE,
                idempotency=Idempotency.NON_IDEMPOTENT,
                retry=RetryPolicy.RECONCILE_FIRST,
            )
        )
        failed = runtime.execute_tool(CanonicalToolCall(name="send_once", args={"value": "x"}))
        self.assertIsInstance(failed.invocation_result, FailedResult)
        self.assertEqual(failed.invocation_result.effect, EffectOutcome.NOT_APPLIED)
        self.assertEqual(failed.invocation_result.retry, RetryDirective.SAFE)

    def test_alias_conflict_does_not_replace_generation(self) -> None:
        runtime = self.core.context.execution_runtime
        runtime.register_tool(tool(alias="echo", canonical_path="op_test_echo", handler=lambda value: {"echo": value.value}))
        before = runtime.registry_generation
        with self.assertRaises(ValueError):
            runtime.register_tool(
                tool(alias="echo", canonical_path="op_other_echo", handler=lambda value: {"echo": value.value})
            )
        self.assertIs(runtime.registry_generation, before)
        self.assertEqual(runtime.execute_tool(CanonicalToolCall(name="echo", args={"value": "ok"})).structured, {"echo": "ok"})

    async def test_old_generation_call_completes_after_unregister_and_new_call_rejects(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(value: EchoInput) -> dict[str, str]:
            started.set()
            await release.wait()
            return {"echo": value.value}

        runtime = self.core.context.execution_runtime
        runtime.register_tool(tool(alias="slow_echo", canonical_path="op_test_slow_echo", handler=slow))
        task = asyncio.create_task(
            runtime.execute_tool_async(CanonicalToolCall(name="slow_echo", args={"value": "old"}))
        )
        await started.wait()
        runtime.unregister_tool("slow_echo")
        fresh = await runtime.execute_tool_async(CanonicalToolCall(name="slow_echo", args={"value": "new"}))
        self.assertEqual(fresh.status, "unknown_tool")
        release.set()
        old = await task
        self.assertTrue(old.ok)
        self.assertEqual(old.structured, {"echo": "old"})

    def test_concurrent_attach_detach_only_publishes_complete_generations(self) -> None:
        runtime = self.core.context.execution_runtime
        facade = tool(
            alias="flapping_echo",
            canonical_path="op_test_flapping_echo",
            handler=lambda value: {"echo": value.value},
        )
        subtree = subtree_for_tool(facade)
        handle = SimpleNamespace(mounted_subtree=subtree)
        errors: list[BaseException] = []
        stop = threading.Event()

        def writer() -> None:
            try:
                for _ in range(40):
                    runtime.mount_subtree(handle)
                    runtime.unmount_subtree(handle)
            except BaseException as exc:  # pragma: no cover - retained for assertion detail
                errors.append(exc)
            finally:
                stop.set()

        def observer() -> None:
            try:
                while not stop.is_set():
                    generation = runtime.registry_generation
                    record = generation.direct_aliases.get("flapping_echo")
                    if record is not None:
                        self.assertEqual(record.canonical_path, "op_test_flapping_echo")
                        self.assertIsNotNone(
                            generation.canonical_bindings.get(record.canonical_path, record.target_id)
                        )
                        self.assertIn("flapping_echo", generation.provider_specs)
                    else:
                        self.assertNotIn("flapping_echo", generation.provider_specs)
            except BaseException as exc:  # pragma: no cover - retained for assertion detail
                errors.append(exc)

        writer_thread = threading.Thread(target=writer)
        observer_thread = threading.Thread(target=observer)
        observer_thread.start()
        writer_thread.start()
        writer_thread.join()
        observer_thread.join()
        self.assertEqual(errors, [])
        self.assertNotIn("flapping_echo", runtime.registry_generation.direct_aliases)

    def test_generation_projections_are_immutable(self) -> None:
        runtime = self.core.context.execution_runtime
        runtime.register_tool(tool(alias="echo", canonical_path="op_test_echo", handler=lambda value: {"echo": value.value}))
        generation = runtime.registry_generation
        with self.assertRaises(TypeError):
            generation.direct_aliases["other"] = generation.direct_aliases["echo"]
        with self.assertRaises(TypeError):
            generation.provider_specs["echo"]["function"]["name"] = "changed"
        with self.assertRaises(TypeError):
            generation.capability_registry.descriptors["echo"].parameters_schema["properties"]["value"][
                "type"
            ] = "integer"
        subtree = generation.mounted_subtrees["__tool__:op_test_echo"]
        with self.assertRaises(AttributeError):
            subtree.descriptors.append(generation.capability_registry.descriptors["echo"])

    def test_validated_output_is_paged_without_partial_output_model(self) -> None:
        class LargeOutput(StrictToolModel):
            lines: list[str]

        class EmptyInput(StrictToolModel):
            pass

        with tempfile.TemporaryDirectory(prefix="pal_tool_facade_") as tmp:
            runtime = self.core.context.execution_runtime
            runtime.runtime_root = Path(tmp)
            runtime.register_tool(
                Tool(
                    alias="large_result",
                    canonical_path="op_test_large_result",
                    InputModel=EmptyInput,
                    OutputModel=LargeOutput,
                    guidance=ToolGuidance(
                        purpose="Return a large validated list.",
                        use_when="Paging is under test.",
                        do_not_use_when="A small result is sufficient.",
                        failure_next_steps="Read the next exact page.",
                    ),
                    execution=ToolExecutionSemantics(
                        invocation_mode=InvocationMode.DIRECT,
                        effect_kind=EffectKind.NONE,
                        idempotency=Idempotency.IDEMPOTENT,
                        retry_policy=RetryPolicy.AUTOMATIC,
                        paging=PagingMode.SUPPORTED,
                    ),
                    search_text="large paged result",
                    handler=lambda _value: {"lines": ["x" * 80 for _ in range(20)]},
                )
            )
            from pal.execution.contracts import ToolCallBudget

            result = runtime.execute_tool(
                CanonicalToolCall(name="large_result", args={}, call_id="call_large"),
                budget=ToolCallBudget(max_output_chars=300, preview_chars=256),
            )
            self.assertIsInstance(result.invocation_result, PagedResult)
            payload = result.invocation_result.model_dump(mode="json")
            self.assertNotIn("output", payload)
            self.assertEqual(payload["result_handle"]["result_ref"], "call_large")
            self.assertEqual(payload["affordances"][0]["tool"], "read_tool_result")
            self.assertNotIn("backing_path", payload["result_handle"])

    def test_paging_works_without_runtime_root_and_expired_handle_has_recovery(self) -> None:
        class LargeOutput(StrictToolModel):
            text: str

        class EmptyInput(StrictToolModel):
            pass

        runtime = self.core.context.execution_runtime
        runtime.register_tool(
            Tool(
                alias="memory_page",
                canonical_path="op_test_memory_page",
                InputModel=EmptyInput,
                OutputModel=LargeOutput,
                guidance=ToolGuidance(
                    purpose="Return a large in-memory result.",
                    use_when="A pager without a runtime root is under test.",
                    do_not_use_when="No result is needed.",
                    failure_next_steps="Read the exact next page.",
                ),
                execution=ToolExecutionSemantics(
                    invocation_mode=InvocationMode.DIRECT,
                    effect_kind=EffectKind.NONE,
                    idempotency=Idempotency.IDEMPOTENT,
                    retry_policy=RetryPolicy.AUTOMATIC,
                    paging=PagingMode.SUPPORTED,
                ),
                search_text="memory paging",
                handler=lambda _value: {"text": "x" * 1000},
            )
        )
        from pal.execution.contracts import ToolCallBudget

        page = runtime.execute_tool(
            CanonicalToolCall(name="memory_page", args={}, call_id="memory-ref"),
            budget=ToolCallBudget(max_output_chars=300, preview_chars=256),
        )
        self.assertIsInstance(page.invocation_result, PagedResult)
        self.assertIsNotNone(runtime.read_tool_result_page(result_ref="memory-ref", page=2))

        register_execution_with_core(self.core.context)
        self.core.publish_module_capabilities("execution")
        later = runtime.execute_tool(
            CanonicalToolCall(name="read_tool_result", args={"result_ref": "memory-ref", "page": 2})
        )
        self.assertIsInstance(later.invocation_result, CompleteResult)
        self.assertIn("page_text", later.structured)
        self.assertTrue(later.invocation_result.affordances)

        expired = runtime.execute_tool(
            CanonicalToolCall(name="read_tool_result", args={"result_ref": "missing-ref"})
        )
        self.assertIsInstance(expired.invocation_result, FailedResult)
        self.assertEqual(expired.invocation_result.retry, RetryDirective.DO_NOT_RETRY)
        self.assertTrue(expired.invocation_result.affordances)

    def test_dynamic_nested_example_resolves_pydantic_defs(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "changes": {
                    "type": "object",
                    "properties": {"enabled": {"type": "boolean"}},
                    "required": ["enabled"],
                    "additionalProperties": False,
                }
            },
            "required": ["changes"],
            "additionalProperties": False,
        }
        model = model_from_json_schema("NestedDynamicInput", schema, input_contract=True)
        generated = model.model_json_schema(mode="validation")
        example = _example_from_schema(generated)
        self.assertEqual(example, {"changes": {"enabled": False}})
        model.model_validate(example, strict=True)


if __name__ == "__main__":
    unittest.main()
