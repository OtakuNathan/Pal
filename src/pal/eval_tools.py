from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, ToolResultIR

from pal.shared.tool_protocol import new_tool_call

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pal.llm.conversions import message_ir_from_dict, tool_definition_ir_from_dict
from pal.llm.ir import (
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    MessageRole,
)
from pal.llm.serde import message_to_payload
from pal.runtime_app import open_runtime


DEFAULT_TOOLS_BENCHMARK = Path(__file__).parents[2] / "benchmarks" / "tools" / "v1.json"
_REDACTED_KEYS = {"api_key", "authorization", "credential", "credential_ref", "password", "secret", "token"}


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    category: str
    prompt: str
    expected_alias: str
    expected_first_alias: str
    seed_call: dict[str, Any] | None = None
    forbidden_aliases: tuple[str, ...] = ()
    confusable_pair: bool = False
    max_rounds: int = 5


def load_tools_benchmark(path: Path) -> tuple[dict[str, Any], list[EvalCase]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not str(payload.get("version") or "").strip():
        raise ValueError("tools eval manifest requires a version")
    cases = []
    for raw in list(payload.get("cases") or []):
        if not isinstance(raw, dict):
            continue
        raw_seed_call = raw.get("seed_call")
        seed_call = dict(raw_seed_call) if isinstance(raw_seed_call, dict) else None
        cases.append(
            EvalCase(
                case_id=str(raw.get("id") or "").strip(),
                category=str(raw.get("category") or "selection").strip(),
                prompt=str(raw.get("prompt") or "").strip(),
                expected_alias=str(raw.get("expected_alias") or "").strip(),
                expected_first_alias=str(raw.get("expected_first_alias") or raw.get("expected_alias") or "").strip(),
                seed_call=seed_call,
                forbidden_aliases=tuple(
                    str(alias).strip()
                    for alias in list(raw.get("forbidden_aliases") or [])
                    if str(alias).strip()
                ),
                confusable_pair=bool(raw.get("confusable_pair")),
                max_rounds=max(1, int(raw.get("max_rounds") or 5)),
            )
        )
    if not cases or any(
        not item.case_id or not item.prompt or not item.expected_alias or not item.expected_first_alias
        for item in cases
    ):
        raise ValueError("tools eval manifest cases require id, prompt, expected_alias, and a first alias")
    if any(
        item.seed_call is not None
        and (
            not str(item.seed_call.get("name") or "").strip()
            or not isinstance(item.seed_call.get("args", {}), dict)
        )
        for item in cases
    ):
        raise ValueError("tools eval seed_call requires a tool name and object args")
    return payload, cases


async def run_tools_eval(
    *,
    runtime_root: Path,
    manifest_path: Path = DEFAULT_TOOLS_BENCHMARK,
    output_path: Path | None = None,
    endpoint_id: str | None = None,
) -> dict[str, Any]:
    manifest, cases = load_tools_benchmark(manifest_path)
    handle = open_runtime(runtime_root)
    try:
        execution = handle.core.context.execution_runtime
        generation = execution.registry_generation
        tool_contracts = _plain_json(list(generation.provider_specs.values()))
        model = str(manifest.get("model") or "").strip() or None
        temperature = float(manifest.get("temperature", 0))
        reasoning = str(manifest.get("reasoning") or "medium")
        repetitions = int(manifest.get("repetitions") or 3)
        requested_endpoint_id = str(
            endpoint_id
            or manifest.get("endpoint_id")
            or handle.llm_runtime.active_endpoint_id
            or ""
        ).strip() or None
        expected_model_id: str | None = None
        if requested_endpoint_id is not None:
            target_endpoint = next(
                (
                    item
                    for item in handle.llm_runtime.endpoint_resolver.endpoints
                    if item.endpoint_id == requested_endpoint_id
                ),
                None,
            )
            if target_endpoint is None:
                raise ValueError(f"tools eval endpoint is not enabled: {requested_endpoint_id}")
            expected_model_id = str(target_endpoint.model_id)
        runs: list[dict[str, Any]] = []
        for case in cases:
            for repetition in range(1, repetitions + 1):
                runs.append(
                    await _run_case(
                        case=case,
                        repetition=repetition,
                        model=model,
                        temperature=temperature,
                        reasoning=reasoning,
                        tool_contracts=tool_contracts,
                        llm_runtime=handle.llm_runtime,
                        execution_runtime=execution,
                        endpoint_id=requested_endpoint_id,
                    )
                )
        report = _build_report(
            manifest=manifest,
            manifest_path=manifest_path,
            generation_hash=generation.generation_hash,
            tool_contracts=tool_contracts,
            runs=runs,
            requested_endpoint_id=requested_endpoint_id,
            expected_model_id=expected_model_id,
        )
        target = output_path or _default_report_path(runtime_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(_redact(report), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        report["report_path"] = str(target)
        return report
    finally:
        await handle.stop_async()


async def _run_case(
    *,
    case: EvalCase,
    repetition: int,
    model: str | None,
    temperature: float,
    reasoning: str,
    tool_contracts: list[dict[str, Any]],
    llm_runtime: Any,
    execution_runtime: Any,
    endpoint_id: str | None = None,
) -> dict[str, Any]:
    messages: list[LLMMessageIR] = [
        message_ir_from_dict({
            "role": "system",
            "content": (
                "Complete the task with the available tools. Use exact aliases. "
                "Indirect tools must be discovered and invoked with call_tool. "
                "Follow retry/effect guidance and never retry an unknown non-idempotent effect."
            ),
        }),
        message_ir_from_dict({"role": "user", "content": case.prompt}),
    ]
    calls: list[dict[str, Any]] = []
    actual_endpoint_ids: list[str] = []
    actual_model_ids: list[str] = []
    seed_call_trace: dict[str, Any] | None = None
    if case.seed_call is not None:
        seeded_tool_call = new_tool_call(
            name=str(case.seed_call.get("name") or ""),
            args=dict(case.seed_call.get("args") or {}),
        )
        seeded_result = await execution_runtime.execute_tool_async(
            seeded_tool_call,
            turn_id=f"eval:{case.case_id}:{repetition}:seed",
        )
        seeded_invocation = getattr(seeded_result, "invocation_result", None)
        seed_call_trace = {
            "provider_alias": seeded_tool_call.name,
            "effective_alias": (
                str(seeded_tool_call.args.get("name") or "")
                if seeded_tool_call.name == "call_tool"
                else str(seeded_tool_call.name or "")
            ),
            "args": dict(seeded_tool_call.args),
            "result_kind": str(getattr(seeded_invocation, "kind", "") or ""),
            "status": seeded_result.status,
            "effect": _enum_value(getattr(seeded_invocation, "effect", "")),
            "retry": _enum_value(getattr(seeded_invocation, "retry", "")),
        }
        messages.extend((
            LLMMessageIR(role=MessageRole.ASSISTANT, parts=(seeded_tool_call,)),
            _eval_tool_result_message(seeded_tool_call, seeded_result),
        ))
    first_result_kind = ""
    for round_index in range(case.max_rounds):
        metadata: dict[str, Any] = {
            "eval": "tools",
            "case_id": case.case_id,
            "repetition": repetition,
            "think_level": reasoning,
        }
        if endpoint_id is not None:
            metadata.update({
                "preferred_endpoint_id": endpoint_id,
                "preferred_endpoint_source": "tools_eval",
                "endpoint_fallback_policy": "none",
            })
        outcome = await llm_runtime.agenerate(
            LLMRequestIR(
                messages=tuple(messages),
                policy=GenerationPolicyIR(max_output_tokens=1024, temperature=temperature),
                model_hint=model,
                tools=tuple(tool_definition_ir_from_dict(item) for item in tool_contracts),
                metadata=metadata,
            )
        )
        actual_endpoint_id = str(getattr(outcome, "preferred_endpoint_id", None) or "").strip()
        actual_model_id = str(getattr(outcome, "preferred_model_id", None) or "").strip()
        if actual_endpoint_id:
            actual_endpoint_ids.append(actual_endpoint_id)
        if actual_model_id:
            actual_model_ids.append(actual_model_id)
        tool_calls = list(outcome.tool_calls or [])
        if not tool_calls:
            messages.append(outcome.response.message)
            break
        results = []
        for tool_call in tool_calls:
            result = await execution_runtime.execute_tool_async(tool_call, turn_id=f"eval:{case.case_id}:{repetition}")
            invocation = getattr(result, "invocation_result", None)
            kind = str(getattr(invocation, "kind", "") or "")
            if not first_result_kind:
                first_result_kind = kind
            effective_alias = (
                str(tool_call.args.get("name") or "")
                if tool_call.name == "call_tool"
                else str(tool_call.name or "")
            )
            calls.append(
                {
                    "round": round_index + 1,
                    "provider_alias": tool_call.name,
                    "effective_alias": effective_alias,
                    "args": dict(tool_call.args),
                    "result_kind": kind,
                    "status": result.status,
                    "effect": _enum_value(getattr(invocation, "effect", "")),
                    "retry": _enum_value(getattr(invocation, "retry", "")),
                }
            )
            results.append(result)
        messages.append(outcome.response.message)
        messages.extend(_eval_tool_result_message(call, result) for call, result in zip(tool_calls, results))
    effective = [item["effective_alias"] for item in calls]
    expected_positions = [index for index, alias in enumerate(effective) if alias == case.expected_alias]
    dangerous_retry = _detect_dangerous_retry(calls, forbidden_aliases=case.forbidden_aliases)
    return {
        "case_id": case.case_id,
        "category": case.category,
        "repetition": repetition,
        "expected_alias": case.expected_alias,
        "confusable_pair": case.confusable_pair,
        "expected_first_alias": case.expected_first_alias,
        "top_1_correct": bool(effective and effective[0] == case.expected_first_alias),
        "eventual_correct": bool(expected_positions),
        "first_pass_arguments": bool(expected_positions and calls[expected_positions[0]]["result_kind"] != "rejected"),
        "enum_repaired": bool(
            case.category == "enum_repair"
            and (
                (seed_call_trace is not None and seed_call_trace["result_kind"] == "rejected")
                or any(item["result_kind"] == "rejected" for item in calls)
            )
            and any(item["provider_alias"] == "read_tool" for item in calls)
            and expected_positions
            and calls[expected_positions[-1]]["result_kind"] != "rejected"
        ),
        "recovered": bool(
            case.category in {"detach_recovery", "not_found_recovery"}
            and expected_positions
            and any(item["provider_alias"] in {"search_tools", "read_tool"} for item in calls)
        ),
        "dangerous_retry": dangerous_retry,
        "additional_rounds": max(0, len({item["round"] for item in calls}) - 1),
        "first_result_kind": first_result_kind,
        "actual_endpoint_ids": sorted(set(actual_endpoint_ids)),
        "actual_model_ids": sorted(set(actual_model_ids)),
        "seed_call": seed_call_trace,
        "calls": calls,
        "transcript": _redact(messages),
    }


def _eval_tool_result_message(call: ToolCallIR, result: Any) -> LLMMessageIR:
    return LLMMessageIR(
        role=MessageRole.TOOL,
        parts=(
            ToolResultIR(
                call_id=call.call_id,
                name=call.name,
                content=str(getattr(result, "llm_text", "") or getattr(result, "text", "")),
                ok=bool(getattr(result, "ok", False)),
                status=str(getattr(result, "status", "") or "error"),
                structured=(
                    dict(result.structured)
                    if isinstance(getattr(result, "structured", None), dict)
                    else None
                ),
            ),
        ),
    )


def _detect_dangerous_retry(
    calls: list[dict[str, Any]],
    *,
    forbidden_aliases: tuple[str, ...] = (),
) -> bool:
    forbidden = set(forbidden_aliases)
    if forbidden and any(item.get("effective_alias") in forbidden for item in calls):
        return True
    for index, item in enumerate(calls[:-1]):
        alias = str(item.get("effective_alias") or "")
        if not alias:
            continue
        if item.get("effect") == "unknown" and item.get("retry") in {"reconcile_first", "do_not_retry"}:
            if any(str(later.get("effective_alias") or "") == alias for later in calls[index + 1 :]):
                return True
    return False


def _build_report(
    *,
    manifest: dict[str, Any],
    manifest_path: Path,
    generation_hash: str,
    tool_contracts: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    requested_endpoint_id: str | None = None,
    expected_model_id: str | None = None,
) -> dict[str, Any]:
    def rate(predicate: Any, subset: list[dict[str, Any]] | None = None) -> float:
        values = subset if subset is not None else runs
        return sum(1 for item in values if predicate(item)) / len(values) if values else 0.0

    confusable = [item for item in runs if item["confusable_pair"]]
    enum_cases = [item for item in runs if item["category"] == "enum_repair"]
    recovery_cases = [item for item in runs if item["category"] in {"detach_recovery", "not_found_recovery"}]
    dangerous_cases = [item for item in runs if item["category"] == "dangerous_retry"]
    first_pass_cases = [item for item in runs if item["category"] != "enum_repair"]
    indirect_cases = [item for item in runs if any(call["provider_alias"] == "call_tool" for call in item["calls"])]
    additional_rounds = sorted(item["additional_rounds"] for item in indirect_cases)
    median_rounds = (
        additional_rounds[len(additional_rounds) // 2]
        if additional_rounds
        else 0
    )
    description_chars = sum(
        len(str(contract.get("function", {}).get("description") or "")) for contract in tool_contracts
    )
    metrics = {
        "top_1_accuracy": rate(lambda item: item["top_1_correct"]),
        "eventual_accuracy": rate(lambda item: item["eventual_correct"]),
        "confusable_pair_accuracy": rate(lambda item: item["top_1_correct"], confusable),
        "first_pass_argument_rate": rate(lambda item: item["first_pass_arguments"], first_pass_cases),
        "enum_repair_rate": rate(lambda item: item["enum_repaired"], enum_cases),
        "detach_not_found_recovery_rate": rate(lambda item: item["recovered"], recovery_cases),
        "dangerous_retry_rate": rate(lambda item: item["dangerous_retry"], dangerous_cases),
        "tool_description_tokens_estimate": math.ceil(description_chars / 4),
        "indirect_median_additional_rounds": median_rounds,
    }
    thresholds = {
        "top_1_accuracy": 0.90,
        "eventual_accuracy": 0.90,
        "confusable_pair_accuracy": 0.85,
        "first_pass_argument_rate": 0.90,
        "enum_repair_rate": 0.90,
        "detach_not_found_recovery_rate": 0.90,
        "dangerous_retry_rate": 0.0,
        "indirect_median_additional_rounds": 2,
    }
    checks = {
        key: (
            metrics[key] <= limit
            if key in {"dangerous_retry_rate", "indirect_median_additional_rounds"}
            else metrics[key] >= limit
        )
        for key, limit in thresholds.items()
    }
    actual_endpoint_ids = sorted({
        str(endpoint_id)
        for run in runs
        for endpoint_id in list(run.get("actual_endpoint_ids") or [])
        if str(endpoint_id).strip()
    })
    actual_model_ids = sorted({
        str(model_id)
        for run in runs
        for model_id in list(run.get("actual_model_ids") or [])
        if str(model_id).strip()
    })
    if requested_endpoint_id is not None:
        checks["endpoint_provenance"] = all(
            run.get("actual_endpoint_ids") == [requested_endpoint_id]
            for run in runs
        )
    if expected_model_id is not None:
        checks["model_provenance"] = all(
            run.get("actual_model_ids") == [expected_model_id]
            for run in runs
        )
    baseline = dict(manifest.get("baseline") or {})
    if baseline.get("tool_description_tokens"):
        checks["description_tokens_vs_baseline"] = (
            metrics["tool_description_tokens_estimate"] <= float(baseline["tool_description_tokens"]) * 1.10
        )
    for metric in ("top_1_accuracy", "confusable_pair_accuracy", "first_pass_argument_rate"):
        if metric in baseline:
            checks[f"{metric}_vs_baseline"] = metrics[metric] >= float(baseline[metric]) - 0.05
    return {
        "schema_version": "pal.tools-eval-report.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest_version": manifest.get("version"),
        "manifest_path": str(manifest_path),
        "endpoint_id": requested_endpoint_id,
        "model": expected_model_id or manifest.get("model"),
        "manifest_model": manifest.get("model"),
        "actual_endpoint_ids": actual_endpoint_ids,
        "actual_model_ids": actual_model_ids,
        "temperature": manifest.get("temperature", 0),
        "reasoning": manifest.get("reasoning", "medium"),
        "repetitions": manifest.get("repetitions", 3),
        "registry_generation_hash": generation_hash,
        "metrics": metrics,
        "thresholds": thresholds,
        "checks": checks,
        "passed": all(checks.values()),
        "runs": runs,
    }


def _default_report_path(runtime_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return runtime_root / "data" / "eval" / "tools" / f"tools_eval_{stamp}.json"


def _plain_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _redact(value: Any) -> Any:
    if isinstance(value, LLMMessageIR):
        return _redact(message_to_payload(value))
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if _is_sensitive_key(key) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    if normalized in _REDACTED_KEYS:
        return True
    return normalized.startswith("api_key") or normalized.endswith(
        ("_api_key", "_authorization", "_credential", "_credential_ref", "_password", "_secret", "_token")
    )


def run_tools_eval_cli(
    *,
    runtime_root: Path,
    manifest_path: Path,
    output_path: Path | None,
    endpoint_id: str | None = None,
) -> int:
    import asyncio

    report = asyncio.run(
        run_tools_eval(
            runtime_root=runtime_root,
            manifest_path=manifest_path,
            output_path=output_path,
            endpoint_id=endpoint_id,
        )
    )
    print(json.dumps({
        key: report[key]
        for key in (
            "passed",
            "endpoint_id",
            "model",
            "actual_endpoint_ids",
            "actual_model_ids",
            "metrics",
            "checks",
            "report_path",
        )
    }, indent=2))
    return 0 if report["passed"] else 2


__all__ = ["DEFAULT_TOOLS_BENCHMARK", "load_tools_benchmark", "run_tools_eval", "run_tools_eval_cli"]
