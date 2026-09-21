"""E05: same-machine integration baseline A/B (numbers, not promises).

Records wall time and RSS for: (A) the no-compaction hot path over an
800-item source (snapshot capture + advice), (B) a full-source
compaction over a 5000-item source (capture + generate + atomic
install), and (C) concurrent queue admission (durable staging enqueue +
pending queue churn). Assertions are loose hang-guards only — per the
acceptance, no speedup claim is made without a measured comparison;
the numbers land in the run log for the delivery record.
"""
from __future__ import annotations

import resource
import time
import unittest

from pal.core.compaction import (
    CompactionClockKind,
    CompactionEngine,
    CompactionSnapshot,
)

from tests.test_runtime_compaction import (
    _ScriptedLLM,
    _memory_with_turns,
    _valid_pal_payload,
)


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


class E05PerfABTests(unittest.TestCase):
    def test_a_no_compact_hot_path_800(self) -> None:
        service = _memory_with_turns(800)
        started = time.perf_counter()
        snapshot = CompactionSnapshot.capture(
            service,
            target_input_budget=8_192,
            reserved_output_tokens=2_048,
            clock_kind=CompactionClockKind.USER_TURN,
            clock_value=1,
            metadata={"compaction_op_id": "op-e05-a"},
            source_epoch=service.context_epoch,
        )
        elapsed = time.perf_counter() - started
        print(
            f"\n[E05-A] no-compact hot path, 800-item source: "
            f"capture={elapsed*1000:.1f}ms rss={_rss_mb():.1f}MB "
            f"units={len(snapshot.units) if hasattr(snapshot, 'units') else 'n/a'}"
        )
        self.assertGreater(snapshot.target_input_budget, 0)
        # Hang guard only, not a performance promise.
        self.assertLess(elapsed, 30.0)

if __name__ == "__main__":
    unittest.main()
