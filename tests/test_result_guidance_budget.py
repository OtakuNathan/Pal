"""P2-A/P2-B budget tests: operation output is bounded, and the
long-line shell hint fires only for real source reads (task package v2 §9-§10).

The truncation budget covers body, status, and recovery metadata. Validated
optional affordances are delivered outside that budget.
"""
from __future__ import annotations

import asyncio

from pal.core import PalCore
from pal.execution import register_with_core
from pal.execution.contracts import ToolCallBudget
from pal.execution.tool_facade import (
    EffectKind,
    Idempotency,
    InvocationMode,
    PagingMode,
    RetryPolicy,
    StrictToolModel,
    ToolExecutionError,
    ToolExecutionSemantics,
    ToolGuidance,
    ToolHandlerResult,
)
from pal.shared.tool_protocol import CompleteResult, EffectOutcome, ToolAffordance, new_tool_call
from tests.capability_fixture import mount_test_capability

BIG = 50_000
LIMIT = 2_000


class BoomInput(StrictToolModel):
    value: str = "x"


class BoomOutput(StrictToolModel):
    ok: bool = True


def _big_text() -> str:
    return "boom-" + "x" * BIG


def _core():
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities("execution")
    return core


def _mount_boom(runtime, *, handler, alias="boom", mode=InvocationMode.DIRECT) -> None:
    mount_test_capability(
        runtime,
        alias=alias,
        canonical_path=f"op_test_{alias}",
        InputModel=BoomInput,
        OutputModel=BoomOutput,
        examples=({"value": "x"},),
        handler=handler,
        execution=ToolExecutionSemantics(
            invocation_mode=mode,
            effect_kind=EffectKind.NONE,
            idempotency=Idempotency.IDEMPOTENT,
            retry_policy=RetryPolicy.AUTOMATIC,
            paging=PagingMode.NEVER,
        ),
        guidance=ToolGuidance(
            purpose="Raise or return big payloads.",
            use_when="budget tests",
            do_not_use_when="outside budget tests",
            failure_next_steps="",
        ),
    )


class TestEveryExitIsBounded:
    def test_sync_handler_exception_is_bounded(self, tmp_path):
        core = _core()
        runtime = core.context.execution_runtime

        def handler(_call):
            raise RuntimeError(_big_text())

        _mount_boom(runtime, handler=handler)
        try:
            result = runtime.execute_tool(
                new_tool_call(name="boom", args={}, call_id="d1"),
                budget=ToolCallBudget(max_output_chars=LIMIT, preview_chars=200),
                turn_id="d1",
            )
            assert not result.ok
            assert "RuntimeError" in result.llm_text
            assert len(result.llm_text) <= LIMIT
        finally:
            core.close()

    def test_async_handler_exception_is_bounded(self, tmp_path):
        core = _core()
        runtime = core.context.execution_runtime

        async def handler(_call):
            raise RuntimeError(_big_text())

        _mount_boom(runtime, handler=handler)
        try:
            result = asyncio.run(
                runtime.execute_tool_async(
                    new_tool_call(name="boom", args={}, call_id="d2"),
                    budget=ToolCallBudget(max_output_chars=LIMIT, preview_chars=200),
                    turn_id="d2",
                )
            )
            assert not result.ok
            assert len(result.llm_text) <= LIMIT
        finally:
            core.close()

    def test_huge_error_payloads_are_bounded(self, tmp_path):
        core = _core()
        runtime = core.context.execution_runtime

        def handler(_call):
            raise ToolExecutionError(
                _big_text(),
                details={"blob": "y" * BIG},
                affordances=[
                    ToolAffordance(
                        tool="boom",
                        arguments={"blob": "z" * BIG},
                        reason="r" * BIG,
                    )
                ],
            )

        _mount_boom(runtime, handler=handler)
        try:
            result = runtime.execute_tool(
                new_tool_call(name="boom", args={}, call_id="d3"),
                budget=ToolCallBudget(max_output_chars=LIMIT, preview_chars=200),
                turn_id="d3",
            )
            assert not result.ok
            assert len(result.llm_text) <= LIMIT
        finally:
            core.close()

    def test_pre_invocation_rejection_with_long_input_is_bounded(self, tmp_path):
        core = _core()
        runtime = core.context.execution_runtime
        calls: list[int] = []

        def handler(_call):
            calls.append(1)
            return ToolHandlerResult(output={"ok": True})

        _mount_boom(runtime, handler=handler)
        try:
            result = runtime.execute_tool(
                new_tool_call(
                    name="boom", args={"value": ["v" * BIG]}, call_id="d4"  # wrong type
                ),
                budget=ToolCallBudget(max_output_chars=LIMIT, preview_chars=200),
                turn_id="d4",
            )
            assert not result.ok
            assert calls == []
            assert len(result.llm_text) <= LIMIT
        finally:
            core.close()

    def test_direct_huge_complete_result_is_bounded(self, tmp_path):
        core = _core()
        runtime = core.context.execution_runtime

        def handler(_call):
            return CompleteResult(
                output={"text": _big_text()},
                effect=EffectOutcome.NONE,
                llm_text=_big_text(),
            )

        _mount_boom(runtime, handler=handler)
        try:
            result = runtime.execute_tool(
                new_tool_call(name="boom", args={}, call_id="d5"),
                budget=ToolCallBudget(max_output_chars=LIMIT, preview_chars=200),
                turn_id="d5",
            )
            assert result.ok
            assert len(result.llm_text) <= LIMIT
        finally:
            core.close()

    def test_call_tool_inner_failure_is_attributed_once(self, tmp_path):
        core = _core()
        runtime = core.context.execution_runtime

        def handler(_call):
            raise ToolExecutionError("inner business failure", error_code="inner_boom")

        _mount_boom(runtime, handler=handler, alias="boom_inner", mode=InvocationMode.INDIRECT)
        try:
            result = runtime.execute_tool(
                new_tool_call(
                    name="call_tool", args={"name": "boom_inner", "args": {}}, call_id="d7"
                ),
                budget=ToolCallBudget(max_output_chars=LIMIT, preview_chars=200),
                turn_id="d7",
            )
            assert not result.ok
            assert result.status == "inner_boom"
            assert result.llm_text.count("inner business failure") == 1
            assert len(result.llm_text) <= LIMIT
        finally:
            core.close()

    def test_large_valid_affordance_is_outside_body_budget(self, tmp_path):
        core = _core()
        runtime = core.context.execution_runtime

        def handler(_call):
            return ToolHandlerResult(
                output={"ok": True},
                llm_text="short body",
                affordances=[
                    ToolAffordance(
                        tool="boom",
                        arguments={"value": "w" * BIG},
                        reason="oversized action",
                    )
                ],
            )

        _mount_boom(runtime, handler=handler)
        try:
            result = runtime.execute_tool(
                new_tool_call(name="boom", args={}, call_id="d8"),
                budget=ToolCallBudget(max_output_chars=LIMIT, preview_chars=200),
                turn_id="d8",
            )
            assert result.ok
            assert result.invocation_result.llm_text == "short body"
            assert result.invocation_result.snapshot_refs == ()
            assert result.invocation_result.affordances[0].arguments == {"value": "w" * BIG}
            assert len(result.llm_text) > LIMIT
            assert "oversized action" in result.llm_text
        finally:
            core.close()

    def test_optional_actions_do_not_truncate_body(self, tmp_path):
        core = _core()
        runtime = core.context.execution_runtime
        body = "b" * 1_800
        reason = "specific guidance. " * 20

        def handler(_call):
            return ToolHandlerResult(
                output={"ok": True},
                llm_text=body,
                affordances=[
                    ToolAffordance(tool="boom", arguments={"value": str(i)}, reason=reason)
                    for i in range(3)
                ],
            )

        _mount_boom(runtime, handler=handler)
        try:
            result = runtime.execute_tool(
                new_tool_call(name="boom", args={}, call_id="d10"),
                budget=ToolCallBudget(max_output_chars=LIMIT, preview_chars=200),
                turn_id="d10",
            )
            assert result.ok
            assert result.invocation_result.llm_text == body
            assert len(result.invocation_result.affordances) == 3
            assert result.invocation_result.snapshot_refs == ()
            assert len(result.llm_text) > LIMIT
        finally:
            core.close()

    def test_long_body_keeps_preview_with_large_optional_action(self):
        core = _core()
        runtime = core.context.execution_runtime
        body = "operation succeeded\n" + "b" * 3000
        _mount_boom(runtime, mode=InvocationMode.INDIRECT, handler=lambda _: ToolHandlerResult(
            output={"ok": True}, llm_text=body,
            affordances=[ToolAffordance(tool="read_file", arguments={"file_path": "/" + "x" * 1750},
                                       reason="optional continuation")],
        ))
        try:
            call = new_tool_call(name="call_tool", args={"name": "boom", "args": {}}, call_id="large-guidance")
            budget = ToolCallBudget(max_output_chars=LIMIT, preview_chars=200)
            result = runtime.execute_tool(call, budget=budget, turn_id="large-guidance")
            invocation = result.invocation_result
            assert "operation succeeded" in invocation.llm_text
            assert len(invocation.affordances) == 1
            assert len(invocation.llm_text + runtime._rendered_guidance_tail(
                invocation, include_affordances=False)) <= LIMIT
            assert len(invocation.snapshot_refs) == 1
            assert invocation.llm_text.count("Complete output") <= 1
            assert runtime._finalize_invocation_result(
                runtime.registry_generation, call, invocation, budget=budget, turn_id="large-guidance"
            ) == invocation
        finally:
            core.close()


class TestLongLineHintOnlyForRealReads:
    def _core(self):
        core = PalCore()
        register_with_core(core.context)
        core.publish_module_capabilities("execution")
        return core

    def test_single_long_source_line_read_keeps_shell_hint(self, tmp_path):
        core = self._core()
        runtime = core.context.execution_runtime
        path = tmp_path / "long.txt"
        path.write_text("y" * 10_000)
        try:
            result = runtime.execute_tool(
                new_tool_call(name="read_file", args={"file_path": str(path)}, call_id="d14"),
                budget=ToolCallBudget(max_output_chars=1_500, preview_chars=300),
                turn_id="d14",
            )
            assert result.ok
            assert "focused shell edit" in result.llm_text
        finally:
            core.close()

    def test_many_short_lines_read_does_not_report_long_line(self, tmp_path):
        core = self._core()
        runtime = core.context.execution_runtime
        path = tmp_path / "wide.txt"
        path.write_text("".join(f"line-{n:04d}\n" for n in range(1, 900)))
        try:
            result = runtime.execute_tool(
                new_tool_call(name="read_file", args={"file_path": str(path)}, call_id="d15"),
                budget=ToolCallBudget(max_output_chars=1_400, preview_chars=300),
                turn_id="d15",
            )
            assert result.ok
            assert len(result.llm_text) <= 1_400
            assert "exceeds this delivery budget" not in result.llm_text
            assert "focused shell edit" not in result.llm_text
        finally:
            core.close()

    def test_short_lines_with_huge_edit_diff_has_no_long_line_hint(self, tmp_path):
        core = self._core()
        runtime = core.context.execution_runtime
        path = tmp_path / "edits.txt"
        path.write_text("".join(f"short-{n:03d}\n" for n in range(50)))
        try:
            read = runtime.execute_tool(
                new_tool_call(name="read_file", args={"file_path": str(path)}, call_id="d16r"),
                turn_id="d16",
            )
            assert read.ok
            runtime.commit_tool_delivery(
                turn_id="d16", result_id="d16r", context_delivery=dict(read.context_delivery)
            )
            big_replacement = "".join(f"replacement-{n:04d} line\n" for n in range(400))
            result = runtime.execute_tool(
                new_tool_call(
                    name="edit_file",
                    args={
                        "file_path": str(path),
                        "edits": [
                            {"old_string": "short-000\n", "new_string": big_replacement}
                        ],
                    },
                    call_id="d16",
                ),
                budget=ToolCallBudget(max_output_chars=1_400, preview_chars=300),
                turn_id="d16",
            )
            assert result.ok
            assert len(result.llm_text) <= 1_400
            # The patch proof is long, but no source line is: no shell-edit
            # hint may claim one (D16).
            assert "focused shell edit" not in result.llm_text
            assert "A source line exceeds" not in result.llm_text
            assert "replacement-0000" in path.read_text()
        finally:
            core.close()

    def test_write_proof_length_is_not_a_source_line(self, tmp_path):
        core = self._core()
        runtime = core.context.execution_runtime
        path = tmp_path / "written.txt"
        path.write_text("seed\n")
        try:
            read = runtime.execute_tool(
                new_tool_call(name="read_file", args={"file_path": str(path)}, call_id="d17r"),
                turn_id="d17",
            )
            assert read.ok
            runtime.commit_tool_delivery(
                turn_id="d17", result_id="d17r", context_delivery=dict(read.context_delivery)
            )
            payload = "".join(f"written-{n:04d}\n" for n in range(500))
            result = runtime.execute_tool(
                new_tool_call(
                    name="write_file",
                    args={"file_path": str(path), "content": payload},
                    call_id="d17",
                ),
                budget=ToolCallBudget(max_output_chars=1_400, preview_chars=300),
                turn_id="d17",
            )
            assert result.ok
            assert len(result.llm_text) <= 1_400
            assert "focused shell edit" not in result.llm_text
        finally:
            core.close()

    def test_normal_short_read_has_no_incidental_hints(self, tmp_path):
        core = self._core()
        runtime = core.context.execution_runtime
        path = tmp_path / "tiny.txt"
        path.write_text("hello world\n")
        try:
            result = runtime.execute_tool(
                new_tool_call(name="read_file", args={"file_path": str(path)}, call_id="d19"),
                turn_id="d19",
            )
            assert result.ok
            assert "focused shell edit" not in result.llm_text
            assert "exceeds this delivery budget" not in result.llm_text
        finally:
            core.close()

    def test_unicode_emoji_bom_budget_maps_spans_not_decorations(self, tmp_path):
        core = self._core()
        runtime = core.context.execution_runtime
        # Emoji, CJK, and a leading BOM stress the char accounting: budgeted
        # characters must map to real delivered source ranges, never to
        # decoration or undelivered content.
        path = tmp_path / "unicode.txt"
        path.write_bytes(
            b"\xef\xbb\xbf"
            + "".join(f"🦆 line-ČĆ-{n:03d} 中文\n" for n in range(1, 301)).encode("utf-8")
        )
        try:
            result = runtime.execute_tool(
                new_tool_call(name="read_file", args={"file_path": str(path)}, call_id="d12"),
                budget=ToolCallBudget(max_output_chars=1_500, preview_chars=300),
                turn_id="d12",
            )
            assert result.ok
            assert len(result.llm_text) <= 1_500
            runtime.commit_tool_delivery(
                turn_id="d12", result_id="d12", context_delivery=dict(result.context_delivery)
            )
            delivery = result.context_delivery
            assert delivery["complete_file"] is False
            context = runtime.logical_context_for_turn("d12")
            grant = runtime.logical_state.file_grant(
                execution_lifetime_id=context.execution_lifetime_id,
                file_key=str(path.resolve()),
                digest=delivery["digest"],
            )
            assert grant is not None and not grant.complete
            # Every granted line is really present in the delivered preview,
            # and nothing beyond it leaked in.
            delivered = result.llm_text
            for start, end in grant.covered_ranges:
                for number in range(start, end + 1):
                    assert f"line-ČĆ-{number:03d}" in delivered
            covered = {number for start, end in grant.covered_ranges for number in range(start, end + 1)}
            # A middle line outside both preview windows must be absent.
            middle_uncovered = next(
                number
                for number in range(20, 281)
                if number not in covered
            )
            assert f"line-ČĆ-{middle_uncovered:03d} 中文" not in delivered
            # The BOM is part of line one's content, not an extra span.
            assert delivery["spans"], "expected at least one delivered span"
        finally:
            core.close()
