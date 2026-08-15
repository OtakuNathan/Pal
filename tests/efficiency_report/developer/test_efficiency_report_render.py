"""Focused developer tests for the efficiency_report rendering module."""

from __future__ import annotations

import json

from pal.bunshin.v2.efficiency_metrics import (
    MetricValue,
    RoleEfficiencyTotals,
    WorkflowEfficiencyMetrics,
)
from pal.bunshin.v2.efficiency_report import render_json, render_text


def _available(value: float | int) -> MetricValue:
    return MetricValue(value=value, available=True)


def _unavailable(reason: str) -> MetricValue:
    return MetricValue(value=None, available=False, reason=reason)


def _sample_metrics() -> WorkflowEfficiencyMetrics:
    return WorkflowEfficiencyMetrics(
        workflow_id="wf-123",
        tool_batches=_available(12),
        singleton_ratio=_available(5 / 12),
        longest_singleton_streak=_available(3),
        llm_rounds=_available(29),
        token_splits={
            "input_tokens": _available(123_456),
            "output_tokens": _available(4_321),
            "cache_read_tokens": _unavailable(
                "cache token columns not recorded by this storage version"
            ),
            "cache_write_tokens": _unavailable(
                "cache token columns not recorded by this storage version"
            ),
        },
        latency_totals={
            "llm_latency_ms": _available(250_000),
            "tool_latency_ms": _available(480_000),
            "wall_latency_ms": _available(900_000),
        },
        per_role=(
            RoleEfficiencyTotals(
                role="architect",
                invocations=2,
                llm_rounds=11,
                input_tokens=_available(50_000),
                output_tokens=_available(1_000),
                cache_read_tokens=_unavailable("no cache telemetry"),
                cache_write_tokens=_unavailable("no cache telemetry"),
                llm_latency_ms=_available(90_000),
                tool_latency_ms=_available(120_000),
                wall_latency_ms=_available(300_000),
            ),
            RoleEfficiencyTotals(
                role="builder",
                invocations=5,
                llm_rounds=18,
                input_tokens=_available(73_456),
                output_tokens=_available(3_321),
                cache_read_tokens=_unavailable("no cache telemetry"),
                cache_write_tokens=_unavailable("no cache telemetry"),
                llm_latency_ms=_available(160_000),
                tool_latency_ms=_available(360_000),
                wall_latency_ms=_available(600_000),
            ),
        ),
        unavailable_metrics=("cache_read_tokens", "cache_write_tokens"),
    )


def _fully_unavailable_metrics() -> WorkflowEfficiencyMetrics:
    reason = "no recorded telemetry for this workflow"
    return WorkflowEfficiencyMetrics(
        workflow_id="wf-empty",
        tool_batches=_unavailable(reason),
        singleton_ratio=_unavailable(reason),
        longest_singleton_streak=_unavailable(reason),
        llm_rounds=_unavailable(reason),
        token_splits={
            "input_tokens": _unavailable(reason),
            "output_tokens": _unavailable(reason),
            "cache_read_tokens": _unavailable(reason),
            "cache_write_tokens": _unavailable(reason),
        },
        latency_totals={
            "llm_latency_ms": _unavailable(reason),
            "tool_latency_ms": _unavailable(reason),
            "wall_latency_ms": _unavailable(reason),
        },
        per_role=(),
        unavailable_metrics=(
            "tool_batches",
            "singleton_ratio",
            "longest_singleton_streak",
            "llm_rounds",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "llm_latency_ms",
            "tool_latency_ms",
            "wall_latency_ms",
        ),
    )


def test_render_json_encodes_available_metrics_as_plain_numbers() -> None:
    document = json.loads(render_json(_sample_metrics()))
    assert document["workflow_id"] == "wf-123"
    assert document["tool_batches"] == 12
    assert document["singleton_ratio"] == 5 / 12
    assert document["longest_singleton_streak"] == 3
    assert document["llm_rounds"] == 29
    assert document["token_splits"]["input_tokens"] == 123_456
    assert document["latency_totals"]["wall_latency_ms"] == 900_000


def test_render_json_encodes_unavailable_as_null_with_reason() -> None:
    document = json.loads(render_json(_sample_metrics()))
    assert document["token_splits"]["cache_read_tokens"] is None
    assert (
        document["token_splits"]["cache_read_tokens_reason"]
        == "cache token columns not recorded by this storage version"
    )
    assert document["token_splits"]["cache_write_tokens"] is None
    assert isinstance(document["token_splits"]["cache_write_tokens_reason"], str)
    assert document["unavailable_metrics"] == [
        "cache_read_tokens",
        "cache_write_tokens",
    ]


def test_render_json_is_stable_and_deterministic_for_equal_inputs() -> None:
    metrics = _sample_metrics()
    first = render_json(metrics)
    second = render_json(_sample_metrics())
    assert first == second
    assert json.loads(first) == json.loads(second)


def test_render_json_covers_per_role_totals() -> None:
    document = json.loads(render_json(_sample_metrics()))
    roles = document["per_role"]
    assert [entry["role"] for entry in roles] == ["architect", "builder"]
    builder = roles[1]
    assert builder["invocations"] == 5
    assert builder["llm_rounds"] == 18
    assert builder["input_tokens"] == 73_456
    assert builder["cache_read_tokens"] is None
    assert builder["cache_read_tokens_reason"] == "no cache telemetry"


def test_render_text_shows_measured_values_and_unavailable_reasons() -> None:
    text = render_text(_sample_metrics())
    assert "wf-123" in text
    assert "Tool batches: 12" in text
    assert f"Singleton ratio: {5 / 12}" in text
    assert "LLM rounds: 29" in text
    assert "Cache read tokens: unavailable (" in text
    assert (
        "cache token columns not recorded by this storage version" in text
    )
    assert "architect:" in text and "builder:" in text
    assert "Invocations: 5" in text
    # The explicit unavailability summary lists every missing metric.
    assert "Unavailable metrics:" in text
    assert "cache_read_tokens" in text


def test_render_text_never_prints_zero_for_unavailable_metrics() -> None:
    text = render_text(_fully_unavailable_metrics())
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("Tool batches", "Singleton ratio", "LLM rounds")):
            assert "unavailable (" in stripped, stripped
            assert not stripped.endswith(": 0") and not stripped.endswith(": 0.0")


def test_rendering_is_total_over_fully_unavailable_metrics() -> None:
    metrics = _fully_unavailable_metrics()
    text = render_text(metrics)
    document = json.loads(render_json(metrics))
    assert "wf-empty" in text
    assert document["tool_batches"] is None
    assert document["tool_batches_reason"] == (
        "no recorded telemetry for this workflow"
    )
    assert document["per_role"] == []
    assert len(document["unavailable_metrics"]) == 11
