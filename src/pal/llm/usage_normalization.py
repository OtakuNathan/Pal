"""Field-aware provider accounting. No prompt/response content is retained."""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Mapping

from pal.llm.ir import LLMUsageIR

USAGE_FIELD_PATHS = {
    "input_tokens": ("input_tokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens"),
    "cached_input_tokens": (
        "cached_input_tokens", "cache_read_input_tokens",
        "prompt_tokens_details.cached_tokens", "input_tokens_details.cached_tokens",
    ),
    "cache_write_input_tokens": (
        "cache_creation_input_tokens", "cache_write_input_tokens",
        "prompt_tokens_details.cache_write_tokens", "input_tokens_details.cache_write_tokens",
    ),
    "reasoning_tokens": (
        "reasoning_tokens", "thinking_tokens", "completion_tokens_details.reasoning_tokens",
        "output_tokens_details.reasoning_tokens", "completion_tokens_details.thinking_tokens",
        "output_tokens_details.thinking_tokens",
    ),
    "cost": ("cost", "total_cost"),
}


def usage_from_mapping(
    payload: Mapping[str, Any] | None, *, input_accounting: str | None = None,
    final: bool = False,
) -> LLMUsageIR:
    source = payload or {}
    values: dict[str, Any] = {}
    present: list[str] = []
    raw: dict[str, int | float] = {}
    anomalies: list[str] = []
    for field_name, paths in USAGE_FIELD_PATHS.items():
        for path in paths:
            value: Any = source
            for part in path.split("."):
                value = value.get(part) if isinstance(value, Mapping) else None
            if value is None:
                continue
            try:
                number = float(value) if field_name == "cost" else int(value)
                if not math.isfinite(number):
                    raise ValueError("nonfinite counter")
            except (TypeError, ValueError, OverflowError):
                anomalies.append(f"invalid:{path}")
                continue
            raw[path] = number
            if number < 0:
                anomalies.append(f"negative:{path}")
            if field_name not in present:
                present.append(field_name)
                values[field_name] = max(0, number)
    # Auto detection is only the backwards-compatible utility entry point.
    # Runtime codecs always pass their protocol's input accounting explicitly.
    accounting = input_accounting or (
        "exclusive_cache" if any(k in source for k in (
            "cache_read_input_tokens", "cache_creation_input_tokens"
        )) else "inclusive_cache"
    )
    usage = LLMUsageIR(
        **values, reported=payload is not None, reported_fields=tuple(present),
        raw_counters=tuple(raw.items()), raw_input_tokens=values.get("input_tokens"),
        input_accounting=accounting, final=final,
        reasoning_tokens_reported="reasoning_tokens" in present,
        usage_anomaly=",".join(anomalies),
    )
    return _normalize(usage)


def _normalize(usage: LLMUsageIR) -> LLMUsageIR:
    raw_input = usage.raw_input_tokens
    if raw_input is None:
        raw_input = usage.input_tokens
    read, write = usage.cached_input_tokens, usage.cache_write_input_tokens
    if usage.input_accounting == "exclusive_cache":
        total, uncached = raw_input + read + write, raw_input
    else:
        total, uncached = raw_input, raw_input - read - write
    anomalies = [s for s in usage.usage_anomaly.split(",") if s and s != "input_lt_read_write"]
    if uncached < 0 and usage.has("input_tokens"):
        anomalies.append("input_lt_read_write")
    return replace(usage, input_tokens=total, uncached_input_tokens=max(0, uncached),
                   usage_anomaly=",".join(anomalies))


def merge_usage(left: LLMUsageIR, right: LLMUsageIR) -> LLMUsageIR:
    """Merge snapshots of ONE response, never independent provider attempts.

    Each adapter supplies cumulative snapshots, with the last supplied field
    authoritative. Final settlement can revise counters downwards. Replayed
    intermediate frames cannot override a terminal settlement.
    """
    if not right.reported or (left.final and not right.final):
        return left
    present = tuple(name for name in USAGE_FIELD_PATHS if left.has(name) or right.has(name))
    values = {name: getattr(right if right.has(name) else left, name) for name in USAGE_FIELD_PATHS}
    raw_input = right.raw_input_tokens if right.has("input_tokens") else left.raw_input_tokens
    if raw_input is None:
        owner = right if right.has("input_tokens") else left
        raw_input = owner.input_tokens
        if owner.input_accounting == "exclusive_cache":
            raw_input -= owner.cached_input_tokens + owner.cache_write_input_tokens
    raw = {**dict(left.raw_counters), **dict(right.raw_counters)}
    # A partial settlement must not erase an anomaly in an untouched field.
    # Explicit correction of that field may clear its earlier anomaly.
    path_fields = {path: name for name, paths in USAGE_FIELD_PATHS.items() for path in paths}
    anomalies = [item for item in left.usage_anomaly.split(",") if item
                 and item != "input_lt_read_write"
                 and not right.has(path_fields.get(item.partition(":")[2], ""))]
    anomalies.extend(item for item in right.usage_anomaly.split(",") if item)
    return _normalize(LLMUsageIR(
        **values, reported=True, reported_fields=present,
        raw_counters=tuple(raw.items()), raw_input_tokens=raw_input,
        input_accounting=(right.input_accounting if right.input_accounting != "unknown"
                          else left.input_accounting),
        final=left.final or right.final,
        reasoning_tokens_reported=left.reasoning_tokens_reported or right.reasoning_tokens_reported,
        usage_anomaly=",".join(dict.fromkeys(anomalies)),
    ))


def sum_usage(usages: list[LLMUsageIR]) -> LLMUsageIR:
    """Aggregate separate attempts; presence means ALL components are known."""
    if not usages:
        return LLMUsageIR()
    fields = tuple(name for name in USAGE_FIELD_PATHS if all(item.has(name) for item in usages))
    return LLMUsageIR(
        **{name: sum(getattr(item, name) for item in usages) for name in USAGE_FIELD_PATHS},
        uncached_input_tokens=sum(item.uncached_input_tokens for item in usages),
        reported=any(item.reported for item in usages), reported_fields=fields,
        input_accounting="aggregate", final=all(item.final for item in usages),
        reasoning_tokens_reported=all(item.reasoning_tokens_reported for item in usages),
        usage_anomaly=",".join(sorted({item.usage_anomaly for item in usages if item.usage_anomaly})),
    )
