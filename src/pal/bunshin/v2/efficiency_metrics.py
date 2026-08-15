"""Efficiency metric aggregation over raw Bunshin telemetry records.

Pure computation only: no storage access, no I/O. Every metric that cannot
be derived from the supplied records is reported as explicitly unavailable
with a reason; this module never substitutes zero or fabricated values for
missing legacy telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pal.bunshin.v2.efficiency_store import WorkflowTelemetryRecords

_NO_ROUND_EVENTS_REASON = (
    "no recorded LLM round events (bunshin_v2_worker_events has no "
    "llm_round_completed rows for this workflow)"
)
_NO_ROLE_TURNS_REASON = (
    "no recorded role turns (bunshin_v2_role_turns is empty for this workflow)"
)
_NO_ROLE_INVOCATIONS_REASON = "no recorded role invocations for this workflow"
_NO_TOOL_BATCHES_REASON = "no tool batches recorded (every recorded round issued zero tool calls)"
_NO_ROLE_TURNS_FOR_ROLE_REASON_TEMPLATE = (
    "no recorded role turns for role {role!r} (bunshin_v2_role_turns)"
)
_CACHE_TOKEN_REASON = (
    "cache token telemetry is not recorded by this storage "
    "(bunshin_v2_role_turns has no cache token columns)"
)
_UNKNOWN_INVOCATION_REASON = "role turn references an unknown invocation: {invocation_id!r}"


@dataclass(frozen=True)
class MetricValue:
    """One possibly-unavailable scalar metric.

    Attributes:
        value: The measured value when ``available`` is true; ``None``
            otherwise.
        available: False exactly when the underlying telemetry was not
            recorded by legacy storage.
        reason: Human-readable explanation of why the value is unavailable;
            empty when ``available`` is true.
    """

    value: float | int | None
    available: bool
    reason: str = ""


@dataclass(frozen=True)
class RoleEfficiencyTotals:
    """Aggregated per-role totals for one workflow."""

    role: str
    invocations: int
    llm_rounds: int
    input_tokens: MetricValue
    output_tokens: MetricValue
    cache_read_tokens: MetricValue
    cache_write_tokens: MetricValue
    llm_latency_ms: MetricValue
    tool_latency_ms: MetricValue
    wall_latency_ms: MetricValue


@dataclass(frozen=True)
class WorkflowEfficiencyMetrics:
    """Complete efficiency telemetry for exactly one workflow.

    Attributes:
        workflow_id: Workflow identifier the metrics were computed for.
        tool_batches: Total number of assistant tool batches (LLM rounds
            that issued at least one tool call), when worker-round
            telemetry exists.
        singleton_ratio: Tool batches containing exactly one tool call
            divided by all tool batches, in ``[0.0, 1.0]``.
        longest_singleton_streak: Longest run of consecutive LLM rounds
            whose batch contained exactly one tool call.
        llm_rounds: Total completed LLM rounds recorded for the workflow.
        token_splits: Provider-reported input/output/cache token totals.
        latency_totals: LLM, tool, and wall latency totals in milliseconds.
        per_role: Per-role totals ordered deterministically by role name.
        unavailable_metrics: Names of metrics that could not be measured,
            each with the reason carried in its ``MetricValue``.
    """

    workflow_id: str
    tool_batches: MetricValue
    singleton_ratio: MetricValue
    longest_singleton_streak: MetricValue
    llm_rounds: MetricValue
    token_splits: dict[str, MetricValue]
    latency_totals: dict[str, MetricValue]
    per_role: tuple[RoleEfficiencyTotals, ...]
    unavailable_metrics: tuple[str, ...]


def _measured(value: float | int) -> MetricValue:
    return MetricValue(value=value, available=True, reason="")


def _unavailable(reason: str) -> MetricValue:
    return MetricValue(value=None, available=False, reason=reason)


def _required_int(row: Mapping[str, Any], column: str, context: str) -> int:
    """Return ``row[column]`` as an int, rejecting malformed values.

    SQLite stores the telemetry columns as INTEGER; anything else (or a
    missing column) is a structurally malformed row.
    """
    try:
        raw = row[column]
    except (KeyError, IndexError):
        raise ValueError(f"{context}: missing column {column!r}") from None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{context}: column {column!r} is not an integer: {raw!r}")
    return raw


def _required_str(row: Mapping[str, Any], column: str, context: str) -> str:
    try:
        raw = row[column]
    except (KeyError, IndexError):
        raise ValueError(f"{context}: missing column {column!r}") from None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{context}: column {column!r} is not a non-empty string: {raw!r}")
    return raw


def _role_turn_sums(turns: tuple[Mapping[str, Any], ...]) -> dict[str, int] | None:
    """Sum the recorded per-turn token and latency columns.

    Returns ``None`` when no turns exist, so callers can distinguish an
    honestly absent metric from a derived zero.
    """
    if not turns:
        return None
    sums = {
        "input_tokens": 0,
        "output_tokens": 0,
        "llm_latency_ms": 0,
        "tool_latency_ms": 0,
        "wall_latency_ms": 0,
    }
    for index, turn in enumerate(turns):
        context = f"role turn {index}"
        for source, target in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("latency_ms", "llm_latency_ms"),
            ("tool_latency_ms", "tool_latency_ms"),
            ("wall_latency_ms", "wall_latency_ms"),
        ):
            sums[target] += _required_int(turn, source, context)
    return sums


def _round_tool_call_counts(
    worker_events: tuple[Mapping[str, Any], ...],
) -> dict[str, list[tuple[int, int]]] | None:
    """Map each invocation to its ordered per-round tool-call counts.

    Only ``llm_round_completed`` progress events carry round telemetry; the
    latest event (highest ``event_id``) wins when a round emitted more than
    one. Returns ``None`` when no round events exist at all.
    """
    latest: dict[tuple[str, int], tuple[int, int]] = {}
    for event in worker_events:
        invocation_id = _required_str(event, "invocation_id", "worker event")
        if str(event.get("event_kind") or "") != "progress":
            continue
        if str(event.get("phase") or "") != "llm_round_completed":
            continue
        event_id = _required_int(event, "event_id", f"worker event {invocation_id}")
        round_index = _required_int(event, "round_index", f"worker event {invocation_id}")
        tool_call_count = _required_int(
            event, "tool_call_count", f"worker event {invocation_id}"
        )
        if tool_call_count < 0:
            raise ValueError(
                f"worker event {invocation_id}: negative tool_call_count {tool_call_count}"
            )
        key = (invocation_id, round_index)
        previous = latest.get(key)
        if previous is None or event_id >= previous[0]:
            latest[key] = (event_id, tool_call_count)
    if not latest:
        return None
    rounds: dict[str, list[tuple[int, int]]] = {}
    for (invocation_id, round_index), (_event_id, tool_call_count) in sorted(
        latest.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        rounds.setdefault(invocation_id, []).append(
            (round_index, tool_call_count)
        )
    return rounds


def compute_workflow_metrics(records: WorkflowTelemetryRecords) -> WorkflowEfficiencyMetrics:
    """Compute all efficiency metrics for one workflow.

    Args:
        records: Raw read-only telemetry rows for one workflow.

    Returns:
        ``WorkflowEfficiencyMetrics`` where every underived metric is an
        unavailable ``MetricValue`` with a concrete reason.

    Errors:
        Never raises for missing telemetry; unavailability is data, not an
        error. Raises ``ValueError`` only for structurally malformed rows.
    """
    invocation_roles: dict[str, str] = {}
    invocation_rounds: dict[str, int] = {}
    for invocation in records.role_invocations:
        invocation_id = _required_str(invocation, "invocation_id", "role invocation")
        role = _required_str(invocation, "role", f"role invocation {invocation_id}")
        round_count = _required_int(
            invocation,
            "last_completed_turn",
            f"role invocation {invocation_id}",
        )
        if round_count < 0:
            raise ValueError(
                f"role invocation {invocation_id}: negative last_completed_turn "
                f"{round_count}"
            )
        invocation_roles[invocation_id] = role
        invocation_rounds[invocation_id] = round_count

    turns_by_role: dict[str, list[Mapping[str, Any]]] = {}
    for turn in records.role_turns:
        invocation_id = _required_str(turn, "invocation_id", "role turn")
        turn_role = invocation_roles.get(invocation_id)
        if turn_role is None:
            raise ValueError(
                _UNKNOWN_INVOCATION_REASON.format(invocation_id=invocation_id)
            )
        turns_by_role.setdefault(turn_role, []).append(turn)
    all_turns = list(records.role_turns)

    rounds_by_invocation = _round_tool_call_counts(records.worker_events)
    if rounds_by_invocation is None:
        tool_batches = _unavailable(_NO_ROUND_EVENTS_REASON)
        singleton_ratio = _unavailable(_NO_ROUND_EVENTS_REASON)
        longest_singleton_streak = _unavailable(_NO_ROUND_EVENTS_REASON)
    else:
        per_invocation_counts = [counts for counts in rounds_by_invocation.values()]
        batch_count = sum(
            1
            for counts in per_invocation_counts
            for _round_index, count in counts
            if count >= 1
        )
        singleton_count = sum(
            1
            for counts in per_invocation_counts
            for _round_index, count in counts
            if count == 1
        )
        longest_streak = 0
        for counts in per_invocation_counts:
            streak = 0
            previous_round: int | None = None
            for round_index, count in counts:
                if (
                    previous_round is not None
                    and round_index != previous_round + 1
                ):
                    streak = 0
                if count == 1:
                    streak += 1
                    longest_streak = max(longest_streak, streak)
                else:
                    streak = 0
                previous_round = round_index
        tool_batches = _measured(batch_count)
        longest_singleton_streak = _measured(longest_streak)
        if batch_count == 0:
            singleton_ratio = _unavailable(_NO_TOOL_BATCHES_REASON)
        else:
            singleton_ratio = _measured(singleton_count / batch_count)

    llm_rounds = (
        _measured(sum(invocation_rounds.values()))
        if invocation_rounds
        else _unavailable(_NO_ROLE_INVOCATIONS_REASON)
    )

    workflow_sums = _role_turn_sums(tuple(all_turns))
    if workflow_sums is None:
        token_splits = {
            "input_tokens": _unavailable(_NO_ROLE_TURNS_REASON),
            "output_tokens": _unavailable(_NO_ROLE_TURNS_REASON),
            "cache_read_tokens": _unavailable(_CACHE_TOKEN_REASON),
            "cache_write_tokens": _unavailable(_CACHE_TOKEN_REASON),
        }
        latency_totals = {
            name: _unavailable(_NO_ROLE_TURNS_REASON)
            for name in ("llm_latency_ms", "tool_latency_ms", "wall_latency_ms")
        }
    else:
        token_splits = {
            "input_tokens": _measured(workflow_sums["input_tokens"]),
            "output_tokens": _measured(workflow_sums["output_tokens"]),
            "cache_read_tokens": _unavailable(_CACHE_TOKEN_REASON),
            "cache_write_tokens": _unavailable(_CACHE_TOKEN_REASON),
        }
        latency_totals = {
            name: _measured(workflow_sums[name])
            for name in ("llm_latency_ms", "tool_latency_ms", "wall_latency_ms")
        }

    per_role: list[RoleEfficiencyTotals] = []
    for role in sorted(set(invocation_roles.values())):
        role_turns = turns_by_role.get(role, [])
        role_sums = _role_turn_sums(tuple(role_turns))
        if role_sums is None:
            no_turns = _unavailable(
                _NO_ROLE_TURNS_FOR_ROLE_REASON_TEMPLATE.format(role=role)
            )
            role_input = role_output = role_cache_read = role_cache_write = no_turns
            role_llm_latency = role_tool_latency = role_wall_latency = no_turns
        else:
            role_input = _measured(role_sums["input_tokens"])
            role_output = _measured(role_sums["output_tokens"])
            role_cache_read = _unavailable(_CACHE_TOKEN_REASON)
            role_cache_write = _unavailable(_CACHE_TOKEN_REASON)
            role_llm_latency = _measured(role_sums["llm_latency_ms"])
            role_tool_latency = _measured(role_sums["tool_latency_ms"])
            role_wall_latency = _measured(role_sums["wall_latency_ms"])
        per_role.append(
            RoleEfficiencyTotals(
                role=role,
                invocations=sum(1 for r in invocation_roles.values() if r == role),
                llm_rounds=sum(
                    invocation_rounds[invocation_id]
                    for invocation_id, invocation_role in invocation_roles.items()
                    if invocation_role == role
                ),
                input_tokens=role_input,
                output_tokens=role_output,
                cache_read_tokens=role_cache_read,
                cache_write_tokens=role_cache_write,
                llm_latency_ms=role_llm_latency,
                tool_latency_ms=role_tool_latency,
                wall_latency_ms=role_wall_latency,
            )
        )

    unavailable: list[str] = []
    top_level = (
        ("tool_batches", tool_batches),
        ("singleton_ratio", singleton_ratio),
        ("longest_singleton_streak", longest_singleton_streak),
        ("llm_rounds", llm_rounds),
    )
    for name, metric in top_level:
        if not metric.available:
            unavailable.append(name)
    for name, metric in token_splits.items():
        if not metric.available:
            unavailable.append(f"token_splits.{name}")
    for name, metric in latency_totals.items():
        if not metric.available:
            unavailable.append(f"latency_totals.{name}")
    for totals in per_role:
        for field_name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "llm_latency_ms",
            "tool_latency_ms",
            "wall_latency_ms",
        ):
            if not getattr(totals, field_name).available:
                unavailable.append(f"per_role.{totals.role}.{field_name}")

    return WorkflowEfficiencyMetrics(
        workflow_id=records.workflow_id,
        tool_batches=tool_batches,
        singleton_ratio=singleton_ratio,
        longest_singleton_streak=longest_singleton_streak,
        llm_rounds=llm_rounds,
        token_splits=token_splits,
        latency_totals=latency_totals,
        per_role=tuple(per_role),
        unavailable_metrics=tuple(unavailable),
    )
