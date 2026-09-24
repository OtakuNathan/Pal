"""Result-side guidance layering: failure pick-best, action identity, and no
static-success reminders (task package v2 §5-§8).

These tests pin the target contract: static relations stay in descriptors,
failure recovery moves into typed result fields, and successful results carry
no capability reminders unless this result's facts justify them.
"""
from __future__ import annotations

import pytest

from pal.core import PalCore as _PalCoreBootstrap  # noqa: F401  (import-order bootstrap)
from pal.execution.contracts import CapabilityResult
from pal.execution.result_guidance import (
    MAX_RESULT_AFFORDANCES,
    action_key,
    normalize_affordances,
    resolve_failure_guidance,
)
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import (
    EffectKind,
    Idempotency,
    InvocationMode,
    NextToolHint,
    PagingMode,
    RetryPolicy,
    StrictToolModel,
    ToolExecutionError,
    ToolExecutionSemantics,
    ToolGuidance,
    ToolHandlerResult,
    ToolRejectedError,
    compile_tool_description,
)
from pal.shared import RuntimeStatus
from pal.shared.tool_protocol import (
    CompleteResult,
    EffectOutcome,
    FailedResult,
    RejectedResult,
    RetryDirective,
    ToolAffordance,
    new_tool_call,
)
from tests.capability_fixture import mount_test_capability


class EchoInput(StrictToolModel):
    value: str = "x"


class EchoOutput(StrictToolModel):
    echo: str = ""


def _guidance(**overrides) -> ToolGuidance:
    base = dict(
        purpose="Echo a value for result-guidance tests.",
        use_when="running result-guidance tests",
        do_not_use_when="outside result-guidance tests",
        failure_next_steps="inspect the failing test",
        next_tool_hints=(
            NextToolHint(name="search_tools", use_when="the right capability is unknown."),
        ),
    )
    base.update(overrides)
    return ToolGuidance(**base)


def _echo_semantics() -> ToolExecutionSemantics:
    return ToolExecutionSemantics(
        invocation_mode=InvocationMode.DIRECT,
        effect_kind=EffectKind.NONE,
        idempotency=Idempotency.IDEMPOTENT,
        retry_policy=RetryPolicy.AUTOMATIC,
        paging=PagingMode.NEVER,
    )


class TestDescriptorLayering:
    """A-group: static relations stay, failure handbooks leave descriptions."""

    def test_failure_next_steps_is_optional_and_old_declarations_stay_valid(self):
        empty = ToolGuidance(
            purpose="p",
            use_when="u",
            do_not_use_when="n",
            failure_next_steps="",
        )
        assert empty.failure_next_steps == ""
        assert _guidance().failure_next_steps == "inspect the failing test"
        for field_name in ("purpose", "use_when", "do_not_use_when"):
            fields = {
                "purpose": "p",
                "use_when": "u",
                "do_not_use_when": "n",
                field_name: " ",
            }
            with pytest.raises(ValueError, match=field_name):
                ToolGuidance(**fields)

    def test_description_keeps_static_relations_and_drops_failure_handbook(self):
        description = compile_tool_description(
            alias="echo",
            guidance=_guidance(),
            execution=_echo_semantics(),
            input_schema={},
            output_schema={},
            example=None,
            next_tool_lines=("search_tools — discovery route",),
        )
        assert "Purpose:" in description
        assert "Use when:" in description
        assert "Possible next tools:" in description
        assert "search_tools" in description
        assert "Failure next steps" not in description

    def test_registry_description_and_search_keep_static_relation(self, tmp_path):
        from pal.core import PalCore
        from pal.execution import register_with_core

        core = PalCore()
        register_with_core(core.context)
        core.publish_module_capabilities("execution")
        runtime = core.context.execution_runtime
        try:
            mount_test_capability(
                runtime,
                alias="echo",
                canonical_path="op_test_echo",
                InputModel=EchoInput,
                OutputModel=EchoOutput,
                examples=({"value": "x"},),
                handler=lambda payload: ToolHandlerResult(
                    output={"echo": payload.value}
                ),
                guidance=_guidance(),
            )
            record = runtime.registry_generation.indirect_aliases["echo"]
            assert "search_tools" in record.compiled_description
            assert "Failure next steps" not in record.compiled_description
            assert "search_tools" in record.search_document
            # Reading the tool shows the same stable description, not a
            # failure handbook (A03).
            read = runtime.invoke_direct_tool(
                new_tool_call(name="read_tool", args={"name": "echo"})
            )
            assert "search_tools" in read.llm_text
            assert "Failure next steps" not in read.llm_text
        finally:
            core.close()


class TestFailurePickBest:
    """B-group: failure guidance selection is layered, not appended everywhere."""

    def _runtime(self, tmp_path, handler, guidance=None) -> ExecutionRuntime:
        runtime = ExecutionRuntime(runtime_root=tmp_path)
        resolved = guidance or _guidance()
        if resolved.next_tool_hints:
            # The bare runtime has no search_tools target compiled in.
            resolved = resolved.model_copy(update={"next_tool_hints": ()})
        mount_test_capability(
            runtime,
            alias="echo",
            canonical_path="op_test_echo",
            InputModel=EchoInput,
            OutputModel=EchoOutput,
            examples=({"value": "x"},),
            handler=handler,
            guidance=resolved,
            execution=_echo_semantics(),
        )
        return runtime

    def _invoke(self, runtime, args=None):
        return runtime.invoke_direct_tool(
            new_tool_call(name="echo", args=args or {"value": "x"})
        )

    def test_schema_rejection_has_no_business_fallback(self, tmp_path):
        calls: list[int] = []

        def handler(_call):
            calls.append(1)
            return ToolHandlerResult(output={})

        runtime = self._runtime(tmp_path, handler)
        try:
            result = self._invoke(runtime, args={"value": 1})  # wrong type
            assert result.kind == "rejected"
            assert calls == []
            assert "inspect the failing test" not in result.llm_text
            assert "recall_memory" not in result.llm_text
        finally:
            runtime.shutdown()

    def test_handler_recovery_wins_over_declared_fallback(self, tmp_path):
        def handler(_call):
            raise ToolExecutionError(
                "handler blew up",
                affordances=[
                    ToolAffordance(
                        tool="echo",
                        arguments={"value": "x"},
                        reason="retry this call with a fixed value",
                    )
                ],
                recovery_hint="Set value to a string before retrying.",
            )

        runtime = self._runtime(tmp_path, handler)
        try:
            result = self._invoke(runtime)
            assert result.kind == "failed"
            assert result.recovery_hint == "Set value to a string before retrying."
            assert [item.tool for item in result.affordances] == ["echo"]
            assert "inspect the failing test" not in result.llm_text
            assert "recall_memory" not in result.llm_text
        finally:
            runtime.shutdown()

    def test_declared_fallback_used_once_when_handler_silent(self, tmp_path):
        def handler(_call):
            raise RuntimeError("plain failure")

        runtime = self._runtime(tmp_path, handler)
        try:
            result = self._invoke(runtime)
            assert result.kind == "failed"
            assert result.recovery_hint == "inspect the failing test"
            # The recovery text renders once, through the single metadata
            # channel — never concatenated into the body.
            rendered = ExecutionRuntime._render_invocation_for_llm(result)
            assert rendered.count("inspect the failing test") == 1
            assert result.affordances == []
            assert "recall_memory" not in rendered
        finally:
            runtime.shutdown()

    def test_no_fallback_yields_facts_only(self, tmp_path):
        def handler(_call):
            raise RuntimeError("plain failure")

        runtime = self._runtime(
            tmp_path, handler, guidance=_guidance(failure_next_steps="")
        )
        try:
            result = self._invoke(runtime)
            assert result.kind == "failed"
            assert result.recovery_hint == ""
            assert result.affordances == []
            assert "recall_memory" not in result.llm_text
            assert "plain failure" in result.llm_text
        finally:
            runtime.shutdown()

    def test_capability_result_failure_keeps_owner_guidance(self, tmp_path):
        def handler(_call):
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text="business failure",
                structured={"error": "boom"},
                llm_text="business failure",
                recovery_hint="Fix the boom precondition.",
                affordances=[
                    ToolAffordance(
                        tool="echo", arguments={"value": "fixed"}, reason="the fixed retry"
                    )
                ],
            )

        runtime = self._runtime(tmp_path, handler)
        try:
            result = self._invoke(runtime)
            assert result.kind == "failed"
            assert result.recovery_hint == "Fix the boom precondition."
            assert [item.tool for item in result.affordances] == ["echo"]
        finally:
            runtime.shutdown()

    def test_capability_result_business_partial_can_carry_recovery(self, tmp_path):
        def handler(_call):
            return CapabilityResult(
                status=RuntimeStatus.OK,
                text="partial",
                structured={"echo": "partial"},
                llm_text="partial result (business status: partial)",
                recovery_hint="The primary server is not ready; diagnose it.",
                affordances=[
                    ToolAffordance(
                        tool="echo", arguments={"value": "diagnose"}, reason="server binding"
                    )
                ],
            )

        runtime = self._runtime(tmp_path, handler)
        try:
            result = self._invoke(runtime)
            assert result.kind == "complete"
            assert result.recovery_hint == "The primary server is not ready; diagnose it."
            assert [item.tool for item in result.affordances] == ["echo"]
        finally:
            runtime.shutdown()

    def test_plain_success_has_no_reminders(self, tmp_path):
        def handler(payload):
            return ToolHandlerResult(
                output={"echo": payload.value},
                llm_text="echo: x",
            )

        runtime = self._runtime(tmp_path, handler)
        try:
            result = self._invoke(runtime)
            assert result.kind == "complete"
            assert result.affordances == []
            assert result.recovery_hint == ""
            assert "recall_memory" not in result.llm_text
            assert "Failure next step" not in result.llm_text
            assert "search_tools" not in result.llm_text
        finally:
            runtime.shutdown()


class TestDirectResultCarriers:
    """B15: recovery fields survive across carriers and exception paths."""

    def test_complete_result_carries_recovery_hint(self):
        result = CompleteResult(
            output={"x": 1},
            effect=EffectOutcome.NONE,
            llm_text="done",
            recovery_hint="nothing to recover",
        )
        assert result.recovery_hint == "nothing to recover"

    def test_failed_result_carries_recovery_hint(self):
        result = FailedResult(
            error_code="boom",
            error="boom",
            effect=EffectOutcome.UNKNOWN,
            retry=RetryDirective.SAFE,
            llm_text="boom",
            recovery_hint="check state",
        )
        assert result.recovery_hint == "check state"

    def test_rejected_result_carries_recovery_hint(self):
        result = RejectedResult(
            error_code="nope",
            error="nope",
            retry=RetryDirective.CORRECT_INPUT,
            llm_text="nope",
            recovery_hint="correct the input",
        )
        assert result.recovery_hint == "correct the input"

    def test_wire_roundtrip_preserves_guidance_fields(self):
        import json

        from pal.shared.tool_protocol import TOOL_INVOCATION_RESULT_ADAPTER

        failed = FailedResult(
            error_code="boom",
            error="boom",
            effect=EffectOutcome.UNKNOWN,
            retry=RetryDirective.SAFE,
            llm_text="boom",
            affordances=[ToolAffordance(tool="echo", arguments={"value": "x"}, reason="the fixed retry")],
            recovery_hint="check state",
        )
        payload = json.loads(TOOL_INVOCATION_RESULT_ADAPTER.dump_json(failed))
        assert payload["recovery_hint"] == "check state"
        wire = TOOL_INVOCATION_RESULT_ADAPTER.dump_json(failed)
        restored = TOOL_INVOCATION_RESULT_ADAPTER.validate_json(wire)
        assert restored.recovery_hint == "check state"
        assert restored.affordances == failed.affordances

        rejected = RejectedResult(
            error_code="nope",
            error="nope",
            retry=RetryDirective.CORRECT_INPUT,
            llm_text="nope",
            recovery_hint="correct the input",
        )
        restored_rejected = TOOL_INVOCATION_RESULT_ADAPTER.validate_json(
            TOOL_INVOCATION_RESULT_ADAPTER.dump_json(rejected)
        )
        assert restored_rejected.recovery_hint == "correct the input"

        # Old payloads without the new field remain readable (F01).
        legacy = {key: value for key, value in payload.items() if key != "recovery_hint"}
        restored_legacy = TOOL_INVOCATION_RESULT_ADAPTER.validate_json(json.dumps(legacy))
        assert restored_legacy.recovery_hint == ""

    def test_exception_types_carry_recovery_hint(self):
        exc = ToolExecutionError("x", recovery_hint="fix x")
        assert exc.recovery_hint == "fix x"
        rejected = ToolRejectedError("y", recovery_hint="fix y")
        assert rejected.recovery_hint == "fix y"
        legacy = ToolExecutionError("z")
        assert legacy.recovery_hint == ""

    def test_handler_result_carries_recovery_hint(self):
        payload = ToolHandlerResult(output={}, recovery_hint="fix it")
        assert payload.recovery_hint == "fix it"
        assert ToolHandlerResult(output={}).recovery_hint == ""


class TestActionIdentity:
    """C-group: semantic action keys and single-result dedup."""

    def test_call_tool_wrapper_and_direct_share_key(self):
        direct = ToolAffordance(tool="echo", arguments={"value": "x"}, reason="a")
        wrapped = ToolAffordance(
            tool="call_tool",
            arguments={"name": "echo", "args": {"value": "x"}},
            reason="b",
        )
        assert action_key(direct) == action_key(wrapped)

    def test_read_tool_is_a_different_action(self):
        execute = ToolAffordance(tool="echo", arguments={"value": "x"}, reason="a")
        inspect = ToolAffordance(tool="read_tool", arguments={"name": "echo"}, reason="b")
        assert action_key(execute) != action_key(inspect)

    def test_target_and_argument_differences_do_not_merge(self):
        first = ToolAffordance(tool="doctor", arguments={"name": "clangd"}, reason="a")
        second = ToolAffordance(tool="doctor", arguments={"name": "pyright"}, reason="b")
        assert action_key(first) != action_key(second)
        no_args = ToolAffordance(tool="doctor", arguments={}, reason="c")
        assert action_key(first) != action_key(no_args)

    def test_key_order_is_canonical_but_array_order_is_not(self):
        first = ToolAffordance(tool="t", arguments={"a": 1, "b": 2}, reason="r")
        second = ToolAffordance(tool="t", arguments={"b": 2, "a": 1}, reason="r")
        assert action_key(first) == action_key(second)
        ordered = ToolAffordance(tool="t", arguments={"items": [1, 2]}, reason="r")
        reordered = ToolAffordance(tool="t", arguments={"items": [2, 1]}, reason="r")
        assert action_key(ordered) != action_key(reordered)

    def test_value_types_are_not_equivalenced(self):
        missing = ToolAffordance(tool="t", arguments={}, reason="r")
        null = ToolAffordance(tool="t", arguments={"v": None}, reason="r")
        false = ToolAffordance(tool="t", arguments={"v": False}, reason="r")
        zero = ToolAffordance(tool="t", arguments={"v": 0}, reason="r")
        text = ToolAffordance(tool="t", arguments={"v": "0"}, reason="r")
        keys = {action_key(item) for item in (missing, null, false, zero, text)}
        assert len(keys) == 5

    def test_dedup_keeps_first_specific_reason_without_concatenation(self):
        candidates = [
            ToolAffordance(tool="echo", arguments={"value": "x"}, reason="first specific reason"),
            ToolAffordance(
                tool="call_tool",
                arguments={"name": "echo", "args": {"value": "x"}},
                reason="second generic",
            ),
        ]
        merged = normalize_affordances(candidates)
        assert len(merged) == 1
        assert merged[0].reason == "first specific reason"

    def test_cap_limits_candidates(self):
        candidates = [
            ToolAffordance(tool=f"t{i}", arguments={"i": i}, reason="r") for i in range(6)
        ]
        merged = normalize_affordances(candidates)
        assert len(merged) == MAX_RESULT_AFFORDANCES
        assert [item.tool for item in merged] == ["t0", "t1", "t2"]

    def test_normalization_is_idempotent(self):
        candidates = [
            ToolAffordance(tool="echo", arguments={"value": "x"}, reason="first"),
            ToolAffordance(
                tool="call_tool",
                arguments={"name": "echo", "args": {"value": "x"}},
                reason="second",
            ),
            ToolAffordance(tool="t", arguments={"a": 1}, reason="third"),
        ]
        once = normalize_affordances(candidates)
        twice = normalize_affordances(once)
        assert once == twice


class TestResolveFailureGuidance:
    def test_handler_hint_wins(self):
        hint, affordances = resolve_failure_guidance(
            handler_recovery_hint="handler hint",
            handler_affordances=[ToolAffordance(tool="t", arguments={}, reason="r")],
            declared_failure_next_steps="declared fallback",
        )
        assert hint == "handler hint"
        assert len(affordances) == 1

    def test_declared_fallback_fills_empty_hint(self):
        hint, affordances = resolve_failure_guidance(
            handler_recovery_hint="",
            handler_affordances=[],
            declared_failure_next_steps="declared fallback",
        )
        assert hint == "declared fallback"
        assert affordances == []

    def test_handler_affordances_suppress_declared_fallback(self):
        hint, affordances = resolve_failure_guidance(
            handler_recovery_hint="",
            handler_affordances=[ToolAffordance(tool="t", arguments={}, reason="specific action")],
            declared_failure_next_steps="declared fallback",
        )
        assert hint == ""
        assert len(affordances) == 1

    def test_all_empty_is_valid(self):
        hint, affordances = resolve_failure_guidance(
            handler_recovery_hint="",
            handler_affordances=[],
            declared_failure_next_steps="",
        )
        assert hint == ""
        assert affordances == []


class TestGuidanceFailureDegrades:
    """B12: a broken guidance layer never changes operation facts."""

    def test_helper_exception_keeps_facts_and_reduces_to_bare_result(self, tmp_path, monkeypatch):
        def handler(_call):
            raise ToolExecutionError(
                "business boom",
                error_code="biz_boom",
                details={"trace": "kept"},
            )

        runtime = ExecutionRuntime(runtime_root=tmp_path)
        mount_test_capability(
            runtime,
            alias="echo",
            canonical_path="op_test_echo",
            InputModel=EchoInput,
            OutputModel=EchoOutput,
            examples=({"value": "x"},),
            handler=handler,
            guidance=_guidance(next_tool_hints=()),
        )

        def broken(self, generation, result):
            raise RuntimeError("guidance helper exploded")

        monkeypatch.setattr(ExecutionRuntime, "_resolve_result_guidance", broken)
        try:
            result = runtime.invoke_indirect_tool(
                new_tool_call(name="echo", args={"value": "x"})
            )
            assert result.kind == "failed"
            assert result.error_code == "biz_boom"
            assert result.effect == EffectOutcome.NONE
            assert result.retry == RetryDirective.SAFE
            assert "business boom" in result.llm_text
            assert result.details == {"trace": "kept"}
            # Degraded guidance is empty, never a fabricated replacement.
            assert result.affordances == []
            assert result.recovery_hint == ""
        finally:
            runtime.shutdown()


class TestScopedRoleFiltering:
    """H12/C08: suggested actions respect the captured role view."""

    def test_role_forbidden_action_is_filtered_and_static_relation_degrades(self, tmp_path):
        import asyncio

        from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
        from pal.shared.tool_protocol import ToolAffordance as Affordance

        base = ExecutionRuntime(runtime_root=tmp_path)
        try:
            mount_test_capability(
                base,
                alias="lsp_doctor",
                execution=_echo_semantics(),
                canonical_path="op_test_doctor",
                InputModel=EchoInput,
                OutputModel=EchoOutput,
                examples=({"value": "x"},),
                handler=lambda payload: ToolHandlerResult(output={"echo": payload.value}),
                guidance=_guidance(next_tool_hints=()),
            )

            def probe_handler(payload):
                return CapabilityResult(
                    status=RuntimeStatus.OK,
                    text="probe done",
                    structured={"echo": payload.value},
                    llm_text="probe done",
                    affordances=(
                        Affordance(
                            tool="lsp_doctor",
                            arguments={"value": "clangd"},
                            reason="bound recovery the role cannot use",
                        ),
                    ),
                )

            mount_test_capability(
                base,
                alias="probe",
                canonical_path="op_test_probe",
                InputModel=EchoInput,
                OutputModel=EchoOutput,
                examples=({"value": "x"},),
                handler=probe_handler,
                execution=ToolExecutionSemantics(
                    invocation_mode=InvocationMode.DIRECT,
                    effect_kind=EffectKind.NONE,
                    idempotency=Idempotency.IDEMPOTENT,
                    retry_policy=RetryPolicy.AUTOMATIC,
                    paging=PagingMode.NEVER,
                ),
                guidance=ToolGuidance(
                    purpose="probe",
                    use_when="scoped filter tests",
                    do_not_use_when="outside scoped filter tests",
                    failure_next_steps="",
                    next_tool_hints=(
                        NextToolHint(name="lsp_doctor", use_when="diagnosis is needed."),
                    ),
                ),
            )

            scoped = BunshinScopedExecutionRuntime(base, ["op_test_probe"])
            # The scoped descriptor keeps the static relation but degrades its
            # routing honestly; the global descriptor routes normally.
            spec = {
                item["function"]["name"]: item["function"]
                for item in scoped.build_llm_tool_contracts()
            }
            assert "probe" in spec
            assert "lsp_doctor" not in spec
            assert "not available in the current tool surface" in spec["probe"]["description"]

            # Dynamic suggestions resolve against the captured role view only.
            scoped_result = asyncio.run(
                scoped.execute_tool_async(
                    new_tool_call(name="probe", args={"value": "x"}), turn_id="t"
                )
            )
            assert scoped_result.ok
            assert scoped_result.invocation_result.affordances == []

            # The same producer result keeps its action in the resident view,
            # where the target tool genuinely resolves.
            global_result = base.execute_tool(
                new_tool_call(name="probe", args={"value": "x"}), turn_id="t"
            )
            assert global_result.ok
            assert [item.tool for item in global_result.invocation_result.affordances] == [
                "lsp_doctor"
            ]
        finally:
            base.shutdown()


class TestGuidanceReviewRegressions:
    @pytest.mark.parametrize("carrier", ["success", "failure", "exception", "rejection"])
    def test_malformed_candidates_do_not_replace_operation_result(self, carrier, tmp_path):
        from pal.execution.tool_facade import EffectReceipt
        from tests.test_result_guidance_budget import _core, _mount_boom

        core = _core()
        runtime = core.context.execution_runtime
        # CapabilityResult and exception carriers are not Pydantic models.
        # Bad candidates must be isolated before constructing the typed result.
        candidates = [
            {"tool": "read_file", "arguments": "invalid"},
            ToolAffordance(tool="boom", arguments={"value": object()}, reason="bad JSON"),
            ToolAffordance(tool="boom", arguments={"value": "fixed"}, reason="valid recovery"),
        ]

        def handler(_):
            if carrier == "exception":
                raise ToolExecutionError("original failure", error_code="original_error", affordances=candidates)
            if carrier == "rejection":
                raise ToolRejectedError("original failure", error_code="original_error", affordances=candidates)
            return CapabilityResult(
                status=RuntimeStatus.OK if carrier == "success" else RuntimeStatus.ERROR,
                text="original result",
                llm_text="original result",
                structured={"ok": True} if carrier == "success" else {"error_code": "original_error", "trace": "retained"},
                effect_receipt=EffectReceipt(outcome=EffectOutcome.NONE),
                affordances=candidates,
            )

        _mount_boom(runtime, handler=handler)
        try:
            result = runtime.execute_tool(new_tool_call(name="boom", args={}))
            invocation = result.invocation_result
            assert result.ok == (carrier == "success")
            if carrier != "success":
                assert invocation.error_code == "original_error"
            if carrier == "failure":
                assert invocation.details["trace"] == "retained"
            assert "original" in invocation.llm_text
            assert len(invocation.affordances) == 1
            assert invocation.affordances[0].arguments == {"value": "fixed"}
        finally:
            core.close()

    @pytest.mark.parametrize("carrier", ["failure", "exception"])
    def test_failure_guidance_helper_exception_preserves_failure(self, monkeypatch, carrier):
        from tests.test_result_guidance_budget import _core, _mount_boom

        core = _core()
        runtime = core.context.execution_runtime

        def handler(_):
            if carrier == "exception":
                raise ToolExecutionError("original failure", error_code="original_error", details={"trace": "retained"})
            return CapabilityResult(status=RuntimeStatus.ERROR, text="original failure", llm_text="original failure",
                                    structured={"error_code": "original_error", "trace": "retained"})

        def broken(**kwargs):
            raise RuntimeError("guidance broke")

        _mount_boom(runtime, handler=handler)
        monkeypatch.setattr("pal.execution.runtime.resolve_failure_guidance", broken)
        try:
            result = runtime.execute_tool(new_tool_call(name="boom", args={}))
            assert result.invocation_result.error_code == "original_error"
            assert result.invocation_result.effect == EffectOutcome.NONE
            assert result.invocation_result.retry == RetryDirective.SAFE
            assert result.invocation_result.details["trace"] == "retained"
            assert "original failure" in result.llm_text
            assert result.invocation_result.affordances == []
        finally:
            core.close()

    def test_validate_before_cap_and_project_routes_without_executing(self):
        from tests.test_result_guidance_budget import _core, _mount_boom

        core = _core()
        runtime = core.context.execution_runtime
        calls = []
        _mount_boom(runtime, alias="indirect", mode=InvocationMode.INDIRECT,
                    handler=lambda _: calls.append("indirect"))
        candidates = [
            ToolAffordance(tool="missing", arguments={}, reason="unknown"),
            ToolAffordance(tool="read_file", arguments={"not_file_path": 123}, reason="bad schema"),
            ToolAffordance(tool="call_tool", arguments={"name": "indirect", "args": {"value": 123}}, reason="bad inner schema"),
            ToolAffordance(tool="indirect", arguments={"value": "x"}, reason="valid indirect"),
            ToolAffordance(tool="call_tool", arguments={"name": "indirect", "args": {"value": "x"}}, reason="duplicate"),
            ToolAffordance(tool="call_tool", arguments={"name": "boom", "args": {"value": "x"}}, reason="valid direct"),
        ]
        _mount_boom(runtime, handler=lambda _: ToolHandlerResult(output={"ok": True}, llm_text="done", affordances=candidates))
        try:
            result = runtime.execute_tool(new_tool_call(name="boom", args={}))
            actions = result.invocation_result.affordances
            assert [(item.tool, item.arguments) for item in actions] == [
                ("call_tool", {"name": "indirect", "args": {"value": "x"}}),
                ("boom", {"value": "x"}),
            ]
            assert calls == []
            finalized = runtime._finalize_invocation_result(
                runtime.registry_generation, new_tool_call(name="boom", args={}),
                result.invocation_result, budget=None, turn_id=None,
            )
            assert finalized == result.invocation_result
        finally:
            core.close()
