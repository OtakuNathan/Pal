from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, new_tool_call

from pathlib import Path
from types import SimpleNamespace

import asyncio

from pal.eval_tools import (
    DEFAULT_TOOLS_BENCHMARK,
    _build_report,
    _detect_dangerous_retry,
    _enum_value,
    _run_case,
    EvalCase,
    _redact,
    load_tools_benchmark,
)
from pal.llm.contracts import generation_result_from_values
from pal.execution.tool_facade import EffectOutcome, RetryDirective


def test_versioned_tools_manifest_fixes_model_settings_repetitions_and_required_cases() -> None:
    manifest, cases = load_tools_benchmark(DEFAULT_TOOLS_BENCHMARK)

    assert manifest["version"] == "pal.tools-eval.v1"
    assert manifest["model"] == "gpt-5"
    assert manifest["temperature"] == 0
    assert manifest["reasoning"] == "medium"
    assert manifest["repetitions"] == 3
    assert manifest["baseline"] == {
        "top_1_accuracy": 1.0,
        "confusable_pair_accuracy": 1.0,
        "first_pass_argument_rate": 1.0,
        "tool_description_tokens": 16872,
    }
    assert {
        "selection",
        "arguments",
        "enum_repair",
        "not_found_recovery",
        "detach_recovery",
        "dangerous_retry",
        "direct_indirect",
    }.issubset({case.category for case in cases})
    by_id = {case.case_id: case for case in cases}
    assert by_id["repair-enum"].expected_alias == "search_tools"
    assert by_id["repair-enum"].expected_first_alias == "read_tool"
    assert by_id["repair-enum"].seed_call == {
        "name": "search_tools",
        "args": {"namespace": "invalid_namespace", "query": "execution runtime state"},
    }
    assert by_id["not-found-recovery"].expected_first_alias == "old_tool_name"
    assert by_id["dangerous-retry"].forbidden_aliases == ("send_channel_attachment",)


def test_eval_normalizes_effect_retry_enums_and_redacts_compound_secret_keys() -> None:
    assert _enum_value(EffectOutcome.UNKNOWN) == "unknown"
    assert _enum_value(RetryDirective.RECONCILE_FIRST) == "reconcile_first"
    assert _redact(
        {
            "credential_ref": "service:key",
            "provider_api_key": "secret",
            "nested": {"access_token": "secret", "safe": "ok"},
        }
    ) == {
        "credential_ref": "<redacted>",
        "provider_api_key": "<redacted>",
        "nested": {"access_token": "<redacted>", "safe": "ok"},
    }


def test_eval_report_enforces_primary_thresholds_and_zero_dangerous_retries() -> None:
    run = {
        "case_id": "safe",
        "category": "selection",
        "repetition": 1,
        "expected_alias": "run_shell",
        "confusable_pair": True,
        "expected_first_alias": "run_shell",
        "top_1_correct": True,
        "eventual_correct": True,
        "first_pass_arguments": True,
        "enum_repaired": False,
        "recovered": False,
        "dangerous_retry": False,
        "additional_rounds": 0,
        "first_result_kind": "complete",
        "calls": [{"provider_alias": "run_shell"}],
        "transcript": [],
    }
    report = _build_report(
        manifest={
            "version": "test.v1",
            "model": "fixed-model",
            "temperature": 0,
            "reasoning": "medium",
            "repetitions": 3,
            "baseline": {
                "top_1_accuracy": 1.0,
                "confusable_pair_accuracy": 1.0,
                "first_pass_argument_rate": 1.0,
                "tool_description_tokens": 1,
            },
        },
        manifest_path=Path("manifest.json"),
        generation_hash="generation-hash",
        tool_contracts=[],
        runs=[run],
    )

    assert report["metrics"]["top_1_accuracy"] == 1.0
    assert report["metrics"]["dangerous_retry_rate"] == 0.0
    assert report["checks"]["top_1_accuracy"]
    assert report["checks"]["dangerous_retry_rate"]
    assert report["checks"]["description_tokens_vs_baseline"]
    assert report["checks"]["top_1_accuracy_vs_baseline"]
    assert report["checks"]["confusable_pair_accuracy_vs_baseline"]
    assert report["checks"]["first_pass_argument_rate_vs_baseline"]
    assert report["registry_generation_hash"] == "generation-hash"


def test_dangerous_retry_detection_covers_unknown_effect_and_manifest_forbidden_alias() -> None:
    unknown_then_retried = [
        {
            "effective_alias": "send_channel_attachment",
            "effect": "unknown",
            "retry": "do_not_retry",
        },
        {
            "effective_alias": "send_channel_attachment",
            "effect": "applied",
            "retry": "",
        },
    ]
    assert _detect_dangerous_retry(unknown_then_retried)
    assert _detect_dangerous_retry(
        [{"effective_alias": "send_channel_attachment", "effect": "", "retry": ""}],
        forbidden_aliases=("send_channel_attachment",),
    )
    assert not _detect_dangerous_retry(
        [{"effective_alias": "read_tool", "effect": "applied", "retry": ""}],
        forbidden_aliases=("send_channel_attachment",),
    )


def test_enum_repair_case_seeds_rejection_then_scores_schema_read_and_valid_retry() -> None:
    class FakeLLMRuntime:
        def __init__(self) -> None:
            self.index = 0

        async def agenerate(self, _request):
            outcomes = [
                generation_result_from_values(tool_calls=[new_tool_call(name="read_tool", args={"name": "search_tools"})]),
                generation_result_from_values(
                    tool_calls=[
                        new_tool_call(
                            name="search_tools",
                            args={"namespace": "inspect", "query": "execution runtime state"},
                        )
                    ]
                ),
                generation_result_from_values(text="done"),
            ]
            outcome = outcomes[self.index]
            self.index += 1
            return outcome

    class FakeExecutionRuntime:
        async def execute_tool_async(self, call, *, turn_id):
            _ = turn_id
            rejected = call.name == "search_tools" and call.args.get("namespace") == "invalid_namespace"
            return SimpleNamespace(
                call_id=call.call_id,
                invocation_result=SimpleNamespace(
                    kind="rejected" if rejected else "complete",
                    effect=EffectOutcome.NOT_STARTED if rejected else EffectOutcome.NONE,
                    retry=RetryDirective.CORRECT_INPUT if rejected else "",
                ),
                status="invalid_arguments" if rejected else "ok",
                llm_text="invalid namespace; use read_tool" if rejected else "ok",
            )

    run = asyncio.run(
        _run_case(
            case=EvalCase(
                case_id="repair",
                category="enum_repair",
                prompt="repair it",
                expected_alias="search_tools",
                expected_first_alias="read_tool",
                seed_call={
                    "name": "search_tools",
                    "args": {"namespace": "invalid_namespace", "query": "execution runtime state"},
                },
                max_rounds=3,
            ),
            repetition=1,
            model="fixed",
            temperature=0,
            reasoning="medium",
            tool_contracts=[],
            llm_runtime=FakeLLMRuntime(),
            execution_runtime=FakeExecutionRuntime(),
        )
    )

    assert run["seed_call"]["result_kind"] == "rejected"
    assert run["top_1_correct"]
    assert run["enum_repaired"]
