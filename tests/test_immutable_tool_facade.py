from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, new_tool_call

import asyncio
import json
import unittest
from typing import Literal

from pydantic import Field

from pal.core import PalCore as _PalCoreBootstrap
from pal.execution.contracts import CapabilityResult, ToolCallBudget
from pal.execution.runtime import ExecutionRuntime
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
    ToolExecutionError,
    ToolExecutionSemantics,
    ToolGuidance,
    ToolHandlerResult,
)
from pal.shared import RuntimeStatus, SINGLETON_TARGET
from tests.capability_fixture import (
    build_test_capability_handle,
    mount_test_capability,
    unmount_test_capability,
)


class EchoInput(StrictToolModel):
    value: str = Field(min_length=1)
    mode: Literal["plain", "upper"] = "plain"


class EchoOutput(StrictToolModel):
    echo: str


class NullableInput(StrictToolModel):
    value: str
    optional: str | None = None


def _guidance() -> ToolGuidance:
    return ToolGuidance(
        purpose="Echo one validated value.",
        use_when="a focused test needs a deterministic output",
        do_not_use_when="the input should be sent somewhere external",
        failure_next_steps="correct the input before retrying",
    )


def _execution(
    mode: InvocationMode = InvocationMode.INDIRECT,
    *,
    effect: EffectKind = EffectKind.NONE,
    idempotency: Idempotency = Idempotency.IDEMPOTENT,
    retry: RetryPolicy = RetryPolicy.AUTOMATIC,
    paging: PagingMode = PagingMode.NEVER,
) -> ToolExecutionSemantics:
    return ToolExecutionSemantics(
        invocation_mode=mode,
        effect_kind=effect,
        idempotency=idempotency,
        retry_policy=retry,
        paging=paging,
    )


def _echo_kwargs(
    *,
    alias: str = "echo",
    canonical_path: str = "op_test_echo",
    handler=None,
    mode: InvocationMode = InvocationMode.INDIRECT,
    effect: EffectKind = EffectKind.NONE,
    idempotency: Idempotency = Idempotency.IDEMPOTENT,
    retry: RetryPolicy = RetryPolicy.AUTOMATIC,
    paging: PagingMode = PagingMode.NEVER,
) -> dict[str, object]:
    return {
        "alias": alias,
        "canonical_path": canonical_path,
        "InputModel": EchoInput,
        "OutputModel": EchoOutput,
        "guidance": _guidance(),
        "execution": _execution(
            mode,
            effect=effect,
            idempotency=idempotency,
            retry=retry,
            paging=paging,
        ),
        "handler": handler or (lambda value: {"echo": value.value}),
        "examples": ({"value": "hello", "mode": "plain"},),
    }


class ImmutableToolFacadeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.runtime = ExecutionRuntime()

    async def asyncTearDown(self) -> None:
        self.runtime.shutdown()

    async def test_generation_compiles_one_descriptor_binding_and_description(self) -> None:
        mount_test_capability(self.runtime, **_echo_kwargs())
        record = self.runtime.registry_generation.indirect_aliases["echo"]

        self.assertEqual(record.binding.descriptor.aliases, ("echo",))
        self.assertEqual(record.canonical_path, "op_test_echo")
        self.assertIn("Use when:", record.compiled_description)
        self.assertIn("Do not use when:", record.compiled_description)
        self.assertIn("invocation_mode=indirect", record.compiled_description)
        self.assertIn('["plain", "upper"]', record.compiled_description)
        self.assertIn("Valid example:", record.compiled_description)
        self.assertIn("Output shape:", record.compiled_description)
        self.assertNotIn("Input schema:", record.compiled_description)
        self.assertEqual(
            self.runtime.registry_generation.capability_index.aliases["echo"],
            "op_test_echo",
        )
        self.assertIs(
            self.runtime.registry_generation.canonical_bindings.get(
                "op_test_echo",
                SINGLETON_TARGET,
            ).descriptor,
            record.binding.descriptor,
        )

    async def test_direct_and_indirect_surfaces_are_isolated(self) -> None:
        mount_test_capability(
            self.runtime,
            **_echo_kwargs(
                alias="direct_echo",
                canonical_path="op_test_direct_echo",
                mode=InvocationMode.DIRECT,
            ),
        )
        mount_test_capability(self.runtime, **_echo_kwargs())

        self.assertIn("direct_echo", self.runtime.registry_generation.provider_specs)
        self.assertNotIn("echo", self.runtime.registry_generation.provider_specs)
        wrong_direct = self.runtime.invoke_direct_tool(
            new_tool_call(name="echo", args={"value": "x"})
        )
        wrong_indirect = self.runtime.invoke_indirect_tool(
            new_tool_call(name="direct_echo", args={"value": "x"})
        )
        self.assertIsInstance(wrong_direct, RejectedResult)
        self.assertEqual(wrong_direct.error_code, "wrong_invocation_mode")
        self.assertIsInstance(wrong_indirect, RejectedResult)
        self.assertEqual(wrong_indirect.error_code, "wrong_invocation_mode")
        self.assertTrue(wrong_direct.affordances)
        self.assertTrue(wrong_indirect.affordances)

    async def test_canonical_path_is_never_an_llm_invocation_name(self) -> None:
        mount_test_capability(self.runtime, **_echo_kwargs())
        result = self.runtime.invoke_indirect_tool(
            new_tool_call(
                name="op_test_echo",
                args={"value": "hidden"},
            )
        )
        self.assertIsInstance(result, RejectedResult)
        self.assertEqual(result.error_code, "unknown_tool")
        self.assertEqual(
            self.runtime.project_llm_text("Use op_test_echo."),
            "Use echo.",
        )

    async def test_model_dict_and_json_outputs_normalize_identically(self) -> None:
        outputs = (
            EchoOutput(echo="same"),
            {"echo": "same"},
            json.dumps({"echo": "same"}),
        )
        normalized: list[dict[str, object]] = []
        for index, output in enumerate(outputs):
            alias = f"echo_{index}"
            mount_test_capability(
                self.runtime,
                **_echo_kwargs(
                    alias=alias,
                    canonical_path=f"op_test_{alias}",
                    handler=lambda _value, output=output: output,
                ),
            )
            result = self.runtime.invoke_indirect_tool(
                new_tool_call(name=alias, args={"value": "same"})
            )
            self.assertIsInstance(result, CompleteResult)
            normalized.append(dict(result.output))
        self.assertEqual(normalized, [{"echo": "same"}] * 3)

    async def test_explicit_null_is_forwarded_but_omitted_default_is_not(self) -> None:
        observed: list[tuple[set[str], str | None]] = []

        def handler(value: NullableInput) -> dict[str, str]:
            observed.append((set(value.model_fields_set), value.optional))
            return {"echo": value.value}

        mount_test_capability(
            self.runtime,
            alias="nullable_echo",
            canonical_path="op_test_nullable_echo",
            InputModel=NullableInput,
            OutputModel=EchoOutput,
            guidance=_guidance(),
            execution=_execution(),
            handler=handler,
            examples=({"value": "hello", "optional": None},),
        )

        omitted = self.runtime.invoke_indirect_tool(
            new_tool_call(name="nullable_echo", args={"value": "omitted"})
        )
        explicit = await self.runtime.invoke_indirect_tool_async(
            new_tool_call(
                name="nullable_echo",
                args={"value": "explicit", "optional": None},
            )
        )

        self.assertIsInstance(omitted, CompleteResult)
        self.assertIsInstance(explicit, CompleteResult)
        self.assertEqual(
            observed,
            [
                ({"value"}, None),
                ({"value", "optional"}, None),
            ],
        )

    async def test_effectful_success_requires_receipt(self) -> None:
        mount_test_capability(
            self.runtime,
            **_echo_kwargs(
                alias="write_echo",
                canonical_path="op_test_write_echo",
                effect=EffectKind.LOCAL_WRITE,
                idempotency=Idempotency.NON_IDEMPOTENT,
                retry=RetryPolicy.NEVER_AUTOMATIC,
            ),
        )
        result = self.runtime.invoke_indirect_tool(
            new_tool_call(name="write_echo", args={"value": "x"})
        )
        self.assertIsInstance(result, FailedResult)
        self.assertEqual(result.error_code, "missing_effect_receipt")
        self.assertEqual(result.effect, EffectOutcome.UNKNOWN)
        self.assertEqual(result.retry, RetryDirective.DO_NOT_RETRY)

    async def test_effect_receipt_and_exception_drive_retry_mechanically(self) -> None:
        def applied(value: EchoInput) -> ToolHandlerResult:
            return ToolHandlerResult(
                output={"echo": value.value},
                llm_text="applied",
                effect_receipt=EffectReceipt(
                    outcome=EffectOutcome.APPLIED,
                    receipt={"operation_id": "once"},
                ),
            )

        mount_test_capability(
            self.runtime,
            **_echo_kwargs(
                alias="applied_echo",
                canonical_path="op_test_applied_echo",
                handler=applied,
                effect=EffectKind.EXTERNAL_WRITE,
                idempotency=Idempotency.KEYED_IDEMPOTENT,
                retry=RetryPolicy.RECONCILE_FIRST,
            ),
        )
        complete = self.runtime.invoke_indirect_tool(
            new_tool_call(name="applied_echo", args={"value": "x"})
        )
        self.assertIsInstance(complete, CompleteResult)
        self.assertEqual(complete.effect, EffectOutcome.APPLIED)

        def failed(_value: EchoInput):
            raise ToolExecutionError(
                "provider outcome is unknown",
                effect_receipt=EffectReceipt(
                    outcome=EffectOutcome.UNKNOWN,
                    receipt={},
                ),
            )

        mount_test_capability(
            self.runtime,
            **_echo_kwargs(
                alias="uncertain_echo",
                canonical_path="op_test_uncertain_echo",
                handler=failed,
                effect=EffectKind.EXTERNAL_WRITE,
                idempotency=Idempotency.NON_IDEMPOTENT,
                retry=RetryPolicy.NEVER_AUTOMATIC,
            ),
        )
        uncertain = self.runtime.invoke_indirect_tool(
            new_tool_call(name="uncertain_echo", args={"value": "x"})
        )
        self.assertIsInstance(uncertain, FailedResult)
        self.assertEqual(uncertain.effect, EffectOutcome.UNKNOWN)
        self.assertEqual(uncertain.retry, RetryDirective.DO_NOT_RETRY)

    async def test_validation_rejects_before_handler_and_output_failure_is_tagged(self) -> None:
        calls = 0

        def handler(_value: EchoInput):
            nonlocal calls
            calls += 1
            return {"wrong": True}

        mount_test_capability(
            self.runtime,
            **_echo_kwargs(handler=handler),
        )
        invalid = self.runtime.invoke_indirect_tool(
            new_tool_call(name="echo", args={"value": 7})
        )
        self.assertIsInstance(invalid, RejectedResult)
        self.assertEqual(invalid.effect, EffectOutcome.NOT_STARTED)
        self.assertEqual(calls, 0)

        bad_output = self.runtime.invoke_indirect_tool(
            new_tool_call(name="echo", args={"value": "x"})
        )
        self.assertIsInstance(bad_output, FailedResult)
        self.assertEqual(bad_output.error_code, "output_validation_failed")
        self.assertEqual(calls, 1)

    async def test_capability_failure_repeats_failure_guidance_and_recovery_affordance(self) -> None:
        def blocked(_value: EchoInput) -> CapabilityResult:
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text="echo backend unavailable",
                llm_text="echo backend unavailable",
                structured={"error_code": "backend_unavailable"},
            )

        mount_test_capability(
            self.runtime,
            **_echo_kwargs(handler=blocked),
        )

        result = self.runtime.invoke_indirect_tool(
            new_tool_call(name="echo", args={"value": "x"})
        )

        self.assertIsInstance(result, FailedResult)
        self.assertIn("Failure next steps: correct the input before retrying", result.llm_text)
        self.assertEqual(result.details["failure_next_steps"], "correct the input before retrying")
        self.assertEqual(result.affordances[0].tool, "read_tool")
        self.assertEqual(result.affordances[0].arguments, {"name": "echo"})

    async def test_paging_happens_after_complete_output_validation(self) -> None:
        mount_test_capability(
            self.runtime,
            **_echo_kwargs(
                handler=lambda _value: {"echo": "x" * 5000},
                paging=PagingMode.SUPPORTED,
            ),
        )
        self.runtime.begin_tool_result_turn(
            turn_id="turn-1",
            scope_key="role-session",
            input_id="input-1",
        )
        result = self.runtime.invoke_indirect_tool(
            new_tool_call(
                name="echo",
                args={"value": "x"},
                call_id="call-page",
            ),
            budget=ToolCallBudget(
                max_output_chars=500,
                preview_chars=300,
            ),
            turn_id="turn-1",
        )
        self.assertIsInstance(result, PagedResult)
        self.assertEqual(result.result_handle["result_ref"], "call-page")
        self.assertTrue(result.affordances)
        page = self.runtime.read_tool_result_page(
            result_ref="call-page",
            page=2,
            turn_id="turn-1",
        )
        self.assertIsNotNone(page)
        self.assertEqual(page.state, "ok")

    async def test_generation_swap_is_atomic_and_old_call_can_finish(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(value: EchoInput):
            started.set()
            await release.wait()
            return {"echo": value.value}

        handle = mount_test_capability(
            self.runtime,
            **_echo_kwargs(
                alias="slow_echo",
                canonical_path="op_test_slow_echo",
                handler=slow,
                mode=InvocationMode.DIRECT,
            ),
        )
        old_generation = self.runtime.registry_generation
        running = asyncio.create_task(
            self.runtime.invoke_direct_tool_async(
                new_tool_call(name="slow_echo", args={"value": "old"}),
                generation=old_generation,
            )
        )
        await started.wait()
        unmount_test_capability(self.runtime, handle)
        rejected = await self.runtime.invoke_direct_tool_async(
            new_tool_call(name="slow_echo", args={"value": "new"})
        )
        self.assertIsInstance(rejected, RejectedResult)
        release.set()
        completed = await running
        self.assertIsInstance(completed, CompleteResult)
        self.assertEqual(completed.output, {"echo": "old"})

    async def test_alias_conflict_rejects_whole_candidate_generation(self) -> None:
        mount_test_capability(self.runtime, **_echo_kwargs(alias="shared"))
        before = self.runtime.registry_generation
        conflicting = build_test_capability_handle(
            **_echo_kwargs(
                alias="shared",
                canonical_path="op_test_other",
            )
        )
        with self.assertRaisesRegex(ValueError, "tool alias conflict"):
            self.runtime.mount_subtree(conflicting)
        self.assertIs(self.runtime.registry_generation, before)
        self.assertIsNone(
            self.runtime.bound_action_index.get(
                "op_test_other",
                SINGLETON_TARGET,
            )
        )


if __name__ == "__main__":
    unittest.main()
