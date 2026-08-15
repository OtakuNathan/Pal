"""Verifier corpus for efficiency_report rendering edge behavior.

Derived from the module contract: rendering is total, never fabricates zeroes,
JSON is stable and valid for equal inputs, and unavailable metrics carry
reasons. These cases exercise boundary inputs the developer corpus omits:
empty split/latency dicts, insertion-order independence, available zeroes,
and hostile reason strings.
"""

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


def _boundary_metrics(reason: str = "legacy storage lacks column") -> WorkflowEfficiencyMetrics:
    return WorkflowEfficiencyMetrics(
        workflow_id="wf-edge-unicode-日本語",
        tool_batches=_available(0),  # zero was actually recorded
        singleton_ratio=_available(0.0),
        longest_singleton_streak=_available(0),
        llm_rounds=_available(0),
        token_splits={},
        latency_totals={},
        per_role=(
            RoleEfficiencyTotals(
                role="zeta",
                invocations=1,
                llm_rounds=0,
                input_tokens=_unavailable(reason),
                output_tokens=_unavailable(reason),
                cache_read_tokens=_unavailable(reason),
                cache_write_tokens=_unavailable(reason),
                llm_latency_ms=_unavailable(reason),
                tool_latency_ms=_unavailable(reason),
                wall_latency_ms=_unavailable(reason),
            ),
        ),
        unavailable_metrics=("input_tokens",),
    )


def test_rendering_is_total_over_empty_splits_and_latency_dicts() -> None:
    text = render_text(_boundary_metrics())
    document = json.loads(render_json(_boundary_metrics()))
    assert "wf-edge-unicode-日本語" in text
    assert document["token_splits"] == {}
    assert document["latency_totals"] == {}
    assert document["per_role"][0]["role"] == "zeta"


def test_available_zero_renders_as_zero_and_unavailable_never_does() -> None:
    metrics = _boundary_metrics()
    text = render_text(metrics)
    assert "Tool batches: 0" in text
    # Unavailable role metrics must carry the notice and reason, never 0.
    assert "Input tokens: unavailable (" in text
    assert "Input tokens: 0" not in text


def test_json_output_is_valid_and_reason_strings_are_escaped() -> None:
    hostile = 'quotes " and newline\n and backslash \\ end'
    document = json.loads(render_json(_boundary_metrics(hostile)))
    assert (
        document["per_role"][0]["input_tokens_reason"] == hostile
    )
    # round-trips through a strict parser without error
    json.loads(render_json(_boundary_metrics(hostile)), parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))


def _order_metrics(
    token_splits: dict,
    latency_totals: dict,
) -> WorkflowEfficiencyMetrics:
    return WorkflowEfficiencyMetrics(
        workflow_id="wf-order",
        tool_batches=_available(7),
        singleton_ratio=_available(1.0),
        longest_singleton_streak=_available(7),
        llm_rounds=_available(9),
        token_splits=token_splits,
        latency_totals=latency_totals,
        per_role=(),
        unavailable_metrics=("cache_read_tokens",),
    )


def test_output_is_independent_of_dict_insertion_order() -> None:
    forward = _order_metrics(
        token_splits={
            "input_tokens": _available(1),
            "output_tokens": _available(2),
            "cache_read_tokens": _unavailable("r"),
            "cache_write_tokens": _unavailable("r"),
        },
        latency_totals={
            "llm_latency_ms": _available(3),
            "tool_latency_ms": _available(4),
            "wall_latency_ms": _available(5),
        },
    )
    reverse = _order_metrics(
        token_splits=dict(reversed(list(forward.token_splits.items()))),
        latency_totals=dict(reversed(list(forward.latency_totals.items()))),
    )
    assert render_json(forward) == render_json(reverse)
    assert render_text(forward) == render_text(reverse)


def test_text_report_orders_token_and_latency_sections_deterministically() -> None:
    metrics = WorkflowEfficiencyMetrics(
        workflow_id="wf-sec",
        tool_batches=_available(3),
        singleton_ratio=_unavailable("no tool-call counts"),
        longest_singleton_streak=_unavailable("no tool-call counts"),
        llm_rounds=_available(4),
        token_splits={
            "cache_write_tokens": _unavailable("r"),
            "input_tokens": _available(10),
            "output_tokens": _available(20),
            "cache_read_tokens": _unavailable("r"),
        },
        latency_totals={
            "wall_latency_ms": _available(5),
            "llm_latency_ms": _available(6),
            "tool_latency_ms": _available(7),
        },
        per_role=(),
        unavailable_metrics=("singleton_ratio",),
    )
    text = render_text(metrics)
    assert text.index("Input tokens:") < text.index("Output tokens:")
    assert text.index("Output tokens:") < text.index("Cache read tokens:")
    assert (
        text.index("LLM latency (ms):")
        < text.index("Tool latency (ms):")
        < text.index("Wall latency (ms):")
    )
