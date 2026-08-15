"""Adversarial verifier cases for ``compute_workflow_metrics``.

Derived from the module contract: honest unavailability (never fabricated
zeroes), MetricValue value/reason invariants, deterministic per-role
ordering and aggregation across multiple invocations of one role, latest
event wins for duplicated rounds regardless of iteration order, and
ValueError strictly for structurally malformed rows.
"""

from __future__ import annotations

import unittest
from typing import Any

from pal.bunshin.v2.efficiency_metrics import (
    MetricValue,
    RoleEfficiencyTotals,
    WorkflowEfficiencyMetrics,
    compute_workflow_metrics,
)
from pal.bunshin.v2.efficiency_store import WorkflowTelemetryRecords


def invocation(
    invocation_id: str,
    role: str,
    *,
    llm_rounds: int = 0,
) -> dict[str, Any]:
    return {
        "invocation_id": invocation_id,
        "workflow_id": "wf-1",
        "role": role,
        "status": "completed",
        "last_completed_turn": llm_rounds,
    }


def turn(invocation_id: str, turn_index: int, **overrides: Any) -> dict[str, Any]:
    row = {
        "invocation_id": invocation_id,
        "turn_index": turn_index,
        "input_tokens": 100,
        "output_tokens": 10,
        "latency_ms": 1000,
        "tool_latency_ms": 200,
        "wall_latency_ms": 1200,
    }
    row.update(overrides)
    return row


def round_event(
    invocation_id: str,
    event_id: int,
    round_index: int,
    tool_call_count: int,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "invocation_id": invocation_id,
        "event_kind": "progress",
        "phase": "llm_round_completed",
        "round_index": round_index,
        "tool_call_count": tool_call_count,
    }


def records(
    invocations=(),
    turns=(),
    events=(),
) -> WorkflowTelemetryRecords:
    return WorkflowTelemetryRecords(
        workflow_id="wf-1",
        role_invocations=tuple(invocations),
        role_turns=tuple(turns),
        worker_events=tuple(events),
    )


def all_metric_values(metrics: WorkflowEfficiencyMetrics) -> dict[str, MetricValue]:
    """Flatten every metric and its unavailable-list name."""
    flat: dict[str, MetricValue] = {
        "tool_batches": metrics.tool_batches,
        "singleton_ratio": metrics.singleton_ratio,
        "longest_singleton_streak": metrics.longest_singleton_streak,
        "llm_rounds": metrics.llm_rounds,
    }
    for name, value in metrics.token_splits.items():
        flat[f"token_splits.{name}"] = value
    for name, value in metrics.latency_totals.items():
        flat[f"latency_totals.{name}"] = value
    for totals in metrics.per_role:
        for field_name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "llm_latency_ms",
            "tool_latency_ms",
            "wall_latency_ms",
        ):
            flat[f"per_role.{totals.role}.{field_name}"] = getattr(totals, field_name)
    return flat


class RoleAggregationAcrossInvocationsTests(unittest.TestCase):
    def test_same_role_multiple_invocations_aggregate(self) -> None:
        invocations = [
            invocation("inv-1", "coder", llm_rounds=1),
            invocation("inv-2", "coder", llm_rounds=1),
            invocation("inv-3", "reviewer"),
        ]
        turns = [
            turn("inv-1", 0, input_tokens=10, latency_ms=100, tool_latency_ms=5, wall_latency_ms=105),
            turn("inv-2", 0, input_tokens=20, output_tokens=2, latency_ms=200, tool_latency_ms=7, wall_latency_ms=207),
        ]
        events = [
            round_event("inv-1", 1, 0, 1),
            round_event("inv-2", 2, 0, 2),
        ]
        metrics = compute_workflow_metrics(records(invocations, turns, events))
        self.assertEqual([t.role for t in metrics.per_role], ["coder", "reviewer"])
        coder = metrics.per_role[0]
        self.assertIsInstance(coder, RoleEfficiencyTotals)
        self.assertEqual(coder.invocations, 2)
        self.assertEqual(coder.llm_rounds, 2)
        self.assertEqual(coder.input_tokens.value, 30)
        self.assertEqual(coder.output_tokens.value, 12)
        self.assertEqual(coder.llm_latency_ms.value, 300)
        self.assertEqual(coder.tool_latency_ms.value, 12)
        self.assertEqual(coder.wall_latency_ms.value, 312)
        # Reviewer has invocations but no recorded turns: honest unavailability.
        reviewer = metrics.per_role[1]
        self.assertEqual(reviewer.invocations, 1)
        self.assertEqual(reviewer.llm_rounds, 0)
        for field in (
            "input_tokens",
            "llm_latency_ms",
        ):
            value = getattr(reviewer, field)
            self.assertFalse(value.available, field)
            self.assertIsNone(value.value, field)
            self.assertTrue(value.reason, field)


class OutOfOrderDuplicateRoundTests(unittest.TestCase):
    def test_latest_event_id_wins_regardless_of_iteration_order(self) -> None:
        invocations = [invocation("inv-a", "worker", llm_rounds=2)]
        turns = [turn("inv-a", 0)]
        early = [  # chronological order: round 0 corrected from 3 -> 1
            round_event("inv-a", 10, 0, 3),
            round_event("inv-a", 25, 0, 1),
            round_event("inv-a", 30, 1, 1),
        ]
        late = list(reversed(early))  # same rows, reversed iteration order
        first = compute_workflow_metrics(records(invocations, turns, early))
        second = compute_workflow_metrics(records(invocations, turns, late))
        self.assertEqual(first, second)
        self.assertEqual(first.tool_batches.value, 2)
        self.assertEqual(first.singleton_ratio.value, 1.0)
        self.assertEqual(first.longest_singleton_streak.value, 2)


class RoundTelemetryWithoutTurnsTests(unittest.TestCase):
    def test_tool_metrics_measured_while_token_metrics_unavailable(self) -> None:
        invocations = [invocation("inv-a", "worker", llm_rounds=2)]
        events = [
            round_event("inv-a", 1, 0, 1),
            round_event("inv-a", 2, 1, 0),
        ]
        metrics = compute_workflow_metrics(records(invocations, (), events))
        self.assertEqual(metrics.tool_batches.value, 1)
        self.assertEqual(metrics.longest_singleton_streak.value, 1)
        # The single recorded tool batch contains exactly one tool call, so
        # the ratio is derivable (1.0), not unavailable.
        self.assertTrue(metrics.singleton_ratio.available)
        self.assertEqual(metrics.singleton_ratio.value, 1.0)
        self.assertTrue(metrics.llm_rounds.available)
        self.assertEqual(metrics.llm_rounds.value, 2)
        for name in ("input_tokens", "output_tokens"):
            self.assertFalse(metrics.token_splits[name].available)
            self.assertIsNone(metrics.token_splits[name].value)
            self.assertTrue(metrics.token_splits[name].reason)
        for name in ("llm_latency_ms", "tool_latency_ms", "wall_latency_ms"):
            self.assertFalse(metrics.latency_totals[name].available)
        # No fabricated zeroes anywhere: unavailable metrics are None-valued.
        for name, metric in all_metric_values(metrics).items():
            if not metric.available:
                self.assertIsNone(metric.value, name)
                self.assertTrue(metric.reason, name)


class UnavailableListCorrespondenceTests(unittest.TestCase):
    def test_unavailable_metrics_lists_exactly_the_unavailable_values(self) -> None:
        invocations = [
            invocation("inv-1", "coder", llm_rounds=1),
            invocation("inv-2", "reviewer"),
        ]
        turns = [turn("inv-1", 0)]
        events = [round_event("inv-1", 1, 0, 1)]
        metrics = compute_workflow_metrics(records(invocations, turns, events))
        flat = all_metric_values(metrics)
        unavailable = {name for name, m in flat.items() if not m.available}
        self.assertEqual(set(metrics.unavailable_metrics), unavailable)
        self.assertEqual(len(metrics.unavailable_metrics), len(unavailable))
        # MetricValue structural invariants hold for every flattened metric.
        for name, metric in flat.items():
            self.assertEqual(
                metric.value is None,
                not metric.available,
                f"value/reason invariant: {name}",
            )
            self.assertEqual(
                bool(metric.reason),
                not metric.available,
                f"reason invariant: {name}",
            )


class SingletonRatioBoundsTests(unittest.TestCase):
    def test_ratio_stays_within_unit_interval_for_mixed_counts(self) -> None:
        invocations = [invocation("inv-a", "worker", llm_rounds=5)]
        turns = [turn("inv-a", i) for i in range(5)]
        events = [
            round_event("inv-a", 1, 0, 1),
            round_event("inv-a", 2, 1, 9),
            round_event("inv-a", 3, 2, 1),
            round_event("inv-a", 4, 3, 2),
            round_event("inv-a", 5, 4, 1),
        ]
        metrics = compute_workflow_metrics(records(invocations, turns, events))
        self.assertTrue(metrics.singleton_ratio.available)
        self.assertGreaterEqual(metrics.singleton_ratio.value, 0.0)
        self.assertLessEqual(metrics.singleton_ratio.value, 1.0)
        # All five rounds issued at least one call, so the denominator is 5.
        self.assertEqual(metrics.singleton_ratio.value, 3 / 5)
        self.assertEqual(metrics.longest_singleton_streak.value, 1)


class MalformedRowAdversarialTests(unittest.TestCase):
    def test_float_token_column_raises(self) -> None:
        invocations = [invocation("inv-a", "worker", llm_rounds=1)]
        turns = [turn("inv-a", 0, tool_latency_ms=1.5)]
        with self.assertRaises(ValueError):
            compute_workflow_metrics(records(invocations, turns, ()))

    def test_non_string_invocation_id_in_turn_raises(self) -> None:
        invocations = [invocation("inv-a", "worker")]
        turns = [turn(12345, 0)]  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            compute_workflow_metrics(records(invocations, turns, ()))

    def test_missing_invocation_id_in_event_raises(self) -> None:
        invocations = [invocation("inv-a", "worker")]
        event = round_event("inv-a", 1, 0, 1)
        del event["invocation_id"]
        with self.assertRaises(ValueError):
            compute_workflow_metrics(records(invocations, (), (event,)))


class PurityTests(unittest.TestCase):
    def test_input_records_are_not_mutated(self) -> None:
        invocations = [invocation("inv-a", "worker")]
        turns = [turn("inv-a", 0)]
        events = [round_event("inv-a", 1, 0, 1)]
        before = records(invocations, turns, events)
        snapshot = (
            before.workflow_id,
            tuple(dict(row) for row in before.role_invocations),
            tuple(dict(row) for row in before.role_turns),
            tuple(dict(row) for row in before.worker_events),
        )
        compute_workflow_metrics(before)
        after = (
            before.workflow_id,
            tuple(dict(row) for row in before.role_invocations),
            tuple(dict(row) for row in before.role_turns),
            tuple(dict(row) for row in before.worker_events),
        )
        self.assertEqual(snapshot, after)


if __name__ == "__main__":
    unittest.main()
