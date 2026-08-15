"""Focused developer tests for ``compute_workflow_metrics``.

The store module is a parallel declaration whose bodies are deferred, so these
tests construct ``WorkflowTelemetryRecords`` directly with plain dict rows
shaped like the ``bunshin_v2_role_invocations``, ``bunshin_v2_role_turns``,
and ``bunshin_v2_worker_events`` columns.
"""

from __future__ import annotations

import unittest
from typing import Any

from pal.bunshin.v2.efficiency_metrics import (
    MetricValue,
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
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_latency_ms": 0,
        "total_tool_latency_ms": 0,
        "total_wall_latency_ms": 0,
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
    *,
    phase: str = "llm_round_completed",
    event_kind: str = "progress",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "invocation_id": invocation_id,
        "event_kind": event_kind,
        "phase": phase,
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


def assert_metric_invariants(testcase: unittest.TestCase, metric: MetricValue) -> None:
    testcase.assertEqual(
        metric.value is None,
        not metric.available,
        f"value is None exactly when unavailable: {metric}",
    )
    testcase.assertEqual(
        bool(metric.reason),
        not metric.available,
        f"reason is non-empty exactly when unavailable: {metric}",
    )


class FullTelemetryTests(unittest.TestCase):
    def test_measured_metrics_and_per_role_totals(self) -> None:
        invocations = [
            invocation("inv-a", "candidate_builder", llm_rounds=2),
            invocation("inv-b", "architect", llm_rounds=1),
        ]
        turns = [
            turn("inv-a", 0, input_tokens=300, output_tokens=30, latency_ms=1500),
            turn("inv-a", 1, tool_latency_ms=0, wall_latency_ms=1500),
            turn("inv-b", 0, input_tokens=50, tool_latency_ms=900, wall_latency_ms=1000),
        ]
        events = [
            round_event("inv-a", 1, 0, 1),
            round_event("inv-a", 2, 1, 4),
            round_event("inv-a", 3, 2, 1),
            round_event("inv-a", 4, 3, 1),
            round_event("inv-b", 5, 0, 3),
            round_event("inv-b", 6, 1, 1),
        ]
        metrics = compute_workflow_metrics(records(invocations, turns, events))

        assert_metric_invariants(self, metrics.tool_batches)
        assert_metric_invariants(self, metrics.singleton_ratio)
        assert_metric_invariants(self, metrics.longest_singleton_streak)
        assert_metric_invariants(self, metrics.llm_rounds)
        self.assertEqual(metrics.tool_batches.value, 6)
        self.assertEqual(metrics.singleton_ratio.value, 4 / 6)
        self.assertGreaterEqual(metrics.singleton_ratio.value, 0.0)
        self.assertLessEqual(metrics.singleton_ratio.value, 1.0)
        self.assertEqual(metrics.longest_singleton_streak.value, 2)
        self.assertEqual(metrics.llm_rounds.value, 3)
        self.assertEqual(metrics.workflow_id, "wf-1")

        self.assertEqual(
            metrics.token_splits["input_tokens"],
            MetricValue(value=450, available=True, reason=""),
        )
        self.assertEqual(metrics.token_splits["output_tokens"].value, 50)
        self.assertEqual(metrics.latency_totals["llm_latency_ms"].value, 3500)
        self.assertEqual(metrics.latency_totals["tool_latency_ms"].value, 1100)
        self.assertEqual(metrics.latency_totals["wall_latency_ms"].value, 3700)

        self.assertEqual([totals.role for totals in metrics.per_role], ["architect", "candidate_builder"])
        builder = metrics.per_role[1]
        self.assertEqual(builder.invocations, 1)
        self.assertEqual(builder.llm_rounds, 2)
        self.assertEqual(builder.input_tokens.value, 400)
        self.assertEqual(builder.wall_latency_ms.value, 2700)
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "llm_latency_ms",
            "tool_latency_ms",
            "wall_latency_ms",
        ):
            assert_metric_invariants(self, getattr(builder, field))

        # Cache columns never exist in this schema; they are always honest
        # unavailability, never zeroes.
        self.assertIn("token_splits.cache_read_tokens", metrics.unavailable_metrics)
        self.assertIn("token_splits.cache_write_tokens", metrics.unavailable_metrics)
        self.assertNotIn("token_splits.input_tokens", metrics.unavailable_metrics)
        self.assertNotIn("llm_rounds", metrics.unavailable_metrics)

    def test_non_round_events_are_ignored_and_duplicate_rounds_take_latest(self) -> None:
        events = [
            round_event("inv-a", 1, 0, 2),
            round_event("inv-a", 2, 0, 5),  # corrected count for round 0
            round_event("inv-a", 3, 1, 0, phase="worker_started"),
            round_event("inv-a", 4, 1, 9, event_kind="other"),
        ]
        invocations = [
            invocation("inv-a", "candidate_builder", llm_rounds=2)
        ]
        turns = [turn("inv-a", 0), turn("inv-a", 1)]
        metrics = compute_workflow_metrics(records(invocations, turns, events))
        self.assertEqual(metrics.tool_batches.value, 1)
        self.assertEqual(metrics.singleton_ratio.value, 0.0)
        self.assertEqual(metrics.longest_singleton_streak.value, 0)

    def test_zero_tool_call_round_breaks_singleton_streak(self) -> None:
        events = [
            round_event("inv-a", 1, 0, 1),
            round_event("inv-a", 2, 1, 0),
            round_event("inv-a", 3, 2, 1),
            round_event("inv-a", 4, 3, 1),
        ]
        invocations = [invocation("inv-a", "verifier", llm_rounds=4)]
        turns = [turn("inv-a", i) for i in range(4)]
        metrics = compute_workflow_metrics(records(invocations, turns, events))
        self.assertEqual(metrics.tool_batches.value, 3)
        self.assertEqual(metrics.longest_singleton_streak.value, 2)
        self.assertEqual(metrics.singleton_ratio.value, 3 / 3)

    def test_deterministic_for_equal_records(self) -> None:
        invocations = [
            invocation("inv-a", "b_role", llm_rounds=1),
            invocation("inv-b", "a_role", llm_rounds=1),
        ]
        turns = [turn("inv-a", 0), turn("inv-b", 0)]
        events = [round_event("inv-a", 1, 0, 1), round_event("inv-b", 2, 0, 2)]
        first = compute_workflow_metrics(records(invocations, turns, events))
        second = compute_workflow_metrics(records(invocations, turns, events))
        self.assertEqual(first, second)


class MissingTelemetryTests(unittest.TestCase):
    def test_legacy_records_without_turns_or_events_report_unavailable(self) -> None:
        invocations = [invocation("inv-a", "candidate_builder")]
        metrics = compute_workflow_metrics(records(invocations, (), ()))

        for metric in (
            metrics.tool_batches,
            metrics.singleton_ratio,
            metrics.longest_singleton_streak,
            metrics.token_splits["input_tokens"],
            metrics.latency_totals["llm_latency_ms"],
        ):
            assert_metric_invariants(self, metric)
            self.assertFalse(metric.available)
            self.assertTrue(metric.reason)

        self.assertIsNone(metrics.tool_batches.value)
        self.assertEqual(metrics.llm_rounds.value, 0)
        self.assertTrue(metrics.llm_rounds.available)
        for name in ("tool_batches", "singleton_ratio", "longest_singleton_streak"):
            self.assertIn(name, metrics.unavailable_metrics)
        self.assertIn("token_splits.input_tokens", metrics.unavailable_metrics)
        self.assertIn("latency_totals.tool_latency_ms", metrics.unavailable_metrics)

        self.assertEqual(len(metrics.per_role), 1)
        totals = metrics.per_role[0]
        self.assertEqual(totals.invocations, 1)
        self.assertEqual(totals.llm_rounds, 0)
        self.assertFalse(totals.input_tokens.available)
        self.assertIn("per_role.candidate_builder.input_tokens", metrics.unavailable_metrics)

    def test_rounds_without_tool_batches_leave_ratio_undefined(self) -> None:
        invocations = [invocation("inv-a", "worker", llm_rounds=1)]
        turns = [turn("inv-a", 0)]
        events = [round_event("inv-a", 1, 0, 0)]
        metrics = compute_workflow_metrics(records(invocations, turns, events))
        self.assertEqual(metrics.tool_batches.value, 0)
        self.assertEqual(metrics.longest_singleton_streak.value, 0)
        self.assertFalse(metrics.singleton_ratio.available)
        self.assertTrue(metrics.singleton_ratio.reason)
        self.assertIn("singleton_ratio", metrics.unavailable_metrics)

    def test_completely_empty_records(self) -> None:
        metrics = compute_workflow_metrics(records())
        self.assertEqual(metrics.per_role, ())
        self.assertFalse(metrics.llm_rounds.available)
        self.assertFalse(metrics.tool_batches.available)
        self.assertIn("token_splits.cache_read_tokens", metrics.unavailable_metrics)

    def test_round_count_uses_cumulative_invocation_ledger(self) -> None:
        invocations = [
            invocation("inv-a", "architect", llm_rounds=32),
            invocation("inv-b", "coder", llm_rounds=16),
        ]
        turns = [
            turn("inv-a", 16),
            turn("inv-a", 32),
            turn("inv-b", 16),
        ]

        metrics = compute_workflow_metrics(records(invocations, turns, ()))

        self.assertEqual(metrics.llm_rounds.value, 48)
        self.assertEqual(
            {item.role: item.llm_rounds for item in metrics.per_role},
            {"architect": 32, "coder": 16},
        )

    def test_missing_round_index_breaks_singleton_streak(self) -> None:
        invocations = [invocation("inv-a", "worker", llm_rounds=3)]
        events = [
            round_event("inv-a", 1, 0, 1),
            round_event("inv-a", 2, 2, 1),
        ]

        metrics = compute_workflow_metrics(records(invocations, (), events))

        self.assertEqual(metrics.tool_batches.value, 2)
        self.assertEqual(metrics.longest_singleton_streak.value, 1)


class MalformedRowTests(unittest.TestCase):
    def test_non_integer_token_column_raises(self) -> None:
        invocations = [invocation("inv-a", "candidate_builder")]
        turns = [turn("inv-a", 0, input_tokens="120")]
        with self.assertRaises(ValueError):
            compute_workflow_metrics(records(invocations, turns, ()))

    def test_bool_token_column_raises(self) -> None:
        invocations = [invocation("inv-a", "candidate_builder")]
        turns = [turn("inv-a", 0, output_tokens=True)]
        with self.assertRaises(ValueError):
            compute_workflow_metrics(records(invocations, turns, ()))

    def test_missing_latency_column_raises(self) -> None:
        invocations = [invocation("inv-a", "candidate_builder")]
        bad_turn = turn("inv-a", 0)
        del bad_turn["latency_ms"]
        with self.assertRaises(ValueError):
            compute_workflow_metrics(records(invocations, (bad_turn,), ()))

    def test_negative_tool_call_count_raises(self) -> None:
        invocations = [invocation("inv-a", "worker")]
        with self.assertRaises(ValueError):
            compute_workflow_metrics(
                records(invocations, (), (round_event("inv-a", 1, 0, -1),))
            )

    def test_turn_with_unknown_invocation_raises(self) -> None:
        invocations = [invocation("inv-a", "worker")]
        turns = [turn("inv-unknown", 0)]
        with self.assertRaises(ValueError):
            compute_workflow_metrics(records(invocations, turns, ()))

    def test_empty_role_raises(self) -> None:
        with self.assertRaises(ValueError):
            compute_workflow_metrics(records((invocation("inv-a", ""),), (), ()))


if __name__ == "__main__":
    unittest.main()
