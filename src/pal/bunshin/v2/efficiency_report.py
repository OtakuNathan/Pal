"""Rendering of workflow efficiency telemetry for operators.

Pure rendering only: converts computed metrics into the operator-facing
text report and the machine-readable JSON document. Unavailable metrics
must appear as explicit ``null`` values with reasons in JSON and as honest
"unavailable" lines in text — never as zeroes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from pal.bunshin.v2.efficiency_metrics import (
    MetricValue,
    RoleEfficiencyTotals,
    WorkflowEfficiencyMetrics,
)

_TOKEN_LABELS = (
    ("input_tokens", "Input tokens"),
    ("output_tokens", "Output tokens"),
    ("cache_read_tokens", "Cache read tokens"),
    ("cache_write_tokens", "Cache write tokens"),
)

_LATENCY_LABELS = (
    ("llm_latency_ms", "LLM latency (ms)"),
    ("tool_latency_ms", "Tool latency (ms)"),
    ("wall_latency_ms", "Wall latency (ms)"),
)

_ROLE_METRIC_FIELDS = (
    ("input_tokens", "Input tokens"),
    ("output_tokens", "Output tokens"),
    ("cache_read_tokens", "Cache read tokens"),
    ("cache_write_tokens", "Cache write tokens"),
    ("llm_latency_ms", "LLM latency (ms)"),
    ("tool_latency_ms", "Tool latency (ms)"),
    ("wall_latency_ms", "Wall latency (ms)"),
)


def _metric_text(value: MetricValue) -> str:
    """Format one metric honestly, never substituting zero."""
    if value.available:
        return str(value.value)
    return f"unavailable ({value.reason})"


def _metric_entries(target: dict, name: str, value: MetricValue) -> None:
    """Add one metric to a JSON document object in place.

    Available metrics become plain numbers; unavailable metrics become
    ``null`` plus a sibling ``<name>_reason`` field carrying the reason.
    """
    if value.available:
        target[name] = value.value
    else:
        target[name] = None
        target[f"{name}_reason"] = value.reason


def _render_text_section(
    lines: list[str],
    title: str,
    entries: Iterable[tuple[str, str, MetricValue]],
) -> None:
    lines.append("")
    lines.append(title)
    for _key, label, value in entries:
        lines.append(f"  {label}: {_metric_text(value)}")


def _render_role(lines: list[str], role: RoleEfficiencyTotals) -> None:
    lines.append(f"  {role.role}:")
    lines.append(f"    Invocations: {role.invocations}")
    lines.append(f"    LLM rounds: {role.llm_rounds}")
    for field, label in _ROLE_METRIC_FIELDS:
        lines.append(f"    {label}: {_metric_text(getattr(role, field))}")


def render_text(metrics: WorkflowEfficiencyMetrics) -> str:
    """Render the human-readable operator report.

    Args:
        metrics: Computed metrics for one workflow.

    Returns:
        A complete multi-line report string. Every unavailable metric is
        rendered with an explicit unavailability notice and its reason.

    Errors:
        Never raises; rendering is total over valid metrics.
    """
    lines = [f"Workflow efficiency report: {metrics.workflow_id}"]
    lines.append("")
    lines.append("Overview:")
    lines.append(f"  Tool batches: {_metric_text(metrics.tool_batches)}")
    lines.append(f"  Singleton ratio: {_metric_text(metrics.singleton_ratio)}")
    lines.append(
        "  Longest singleton streak: "
        f"{_metric_text(metrics.longest_singleton_streak)}"
    )
    lines.append(f"  LLM rounds: {_metric_text(metrics.llm_rounds)}")

    _render_text_section(
        lines,
        "Token splits:",
        (
            (key, label, metrics.token_splits[key])
            for key, label in _TOKEN_LABELS
            if key in metrics.token_splits
        ),
    )
    _render_text_section(
        lines,
        "Latency totals (ms):",
        (
            (key, label, metrics.latency_totals[key])
            for key, label in _LATENCY_LABELS
            if key in metrics.latency_totals
        ),
    )

    if metrics.per_role:
        lines.append("")
        lines.append("Per-role totals:")
        for role in metrics.per_role:
            _render_role(lines, role)

    if metrics.unavailable_metrics:
        lines.append("")
        lines.append("Unavailable metrics:")
        for name in metrics.unavailable_metrics:
            lines.append(f"  {name}")

    lines.append("")
    return "\n".join(lines)


def render_json(metrics: WorkflowEfficiencyMetrics) -> str:
    """Render the machine-readable JSON document.

    Args:
        metrics: Computed metrics for one workflow.

    Returns:
        A JSON object document as a string. Unavailable metrics are encoded
        as ``null`` with a sibling reason field; available metrics are
        encoded as plain numbers. Output is stable and deterministic for
        equal inputs.

    Errors:
        Never raises; rendering is total over valid metrics.
    """
    document: dict = {"workflow_id": metrics.workflow_id}
    _metric_entries(document, "tool_batches", metrics.tool_batches)
    _metric_entries(document, "singleton_ratio", metrics.singleton_ratio)
    _metric_entries(
        document, "longest_singleton_streak", metrics.longest_singleton_streak
    )
    _metric_entries(document, "llm_rounds", metrics.llm_rounds)

    token_splits: dict = {}
    for key, value in metrics.token_splits.items():
        _metric_entries(token_splits, key, value)
    document["token_splits"] = token_splits

    latency_totals: dict = {}
    for key, value in metrics.latency_totals.items():
        _metric_entries(latency_totals, key, value)
    document["latency_totals"] = latency_totals

    per_role: list[dict] = []
    for role in metrics.per_role:
        entry: dict = {
            "role": role.role,
            "invocations": role.invocations,
            "llm_rounds": role.llm_rounds,
        }
        for field, _label in _ROLE_METRIC_FIELDS:
            _metric_entries(entry, field, getattr(role, field))
        per_role.append(entry)
    document["per_role"] = per_role

    document["unavailable_metrics"] = list(metrics.unavailable_metrics)

    return json.dumps(document, sort_keys=True, indent=2)
