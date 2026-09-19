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

import asyncio
import resource
import tempfile
import time
import unittest
from pathlib import Path

from pal.core.compaction import (
    CompactionClockKind,
    CompactionEngine,
    CompactionSnapshot,
)
from pal.core.pal_compaction import PalCompactionPolicy
from pal.llm import generation_result_from_values

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

    def test_b_full_source_compact_5000(self) -> None:
        async def scenario():
            service = _memory_with_turns(5000)
            rss_before = _rss_mb()
            started = time.perf_counter()
            snapshot = CompactionSnapshot.capture(
                service,
                target_input_budget=8_192,
                reserved_output_tokens=2_048,
                clock_kind=CompactionClockKind.USER_TURN,
                clock_value=1,
                metadata={"compaction_op_id": "op-e05-b"},
                source_epoch=service.context_epoch,
            )
            capture_s = time.perf_counter() - started
            engine = CompactionEngine(PalCompactionPolicy())
            gen_started = time.perf_counter()
            result = await engine.run(
                snapshot,
                llm_runtime=_ScriptedLLM([
                    generation_result_from_values(
                        text=_valid_pal_payload("e05 bench seed")
                    )
                ]),
                memory_service=service,
            )
            gen_s = time.perf_counter() - gen_started
            total = time.perf_counter() - started
            rss_after = _rss_mb()
            print(
                f"\n[E05-B] full-source compact, 5000-item source: "
                f"capture={capture_s*1000:.1f}ms generate+install={gen_s*1000:.1f}ms "
                f"total={total*1000:.1f}ms rss_delta={rss_after-rss_before:+.1f}MB"
            )
            self.assertTrue(result.success, result.failures)
            self.assertEqual(service.context_epoch, 1)
            self.assertEqual(
                service.compaction_receipts["op-e05-b"].status, "committed",
            )
            self.assertLess(total, 120.0)

        asyncio.run(scenario())

    def test_c_concurrent_queue_admission_500(self) -> None:
        from pal.core.ingress_staging import IngressStagingStore, StagedIngressRecord

        from tests.test_compaction_gate import _envelope

        with tempfile.TemporaryDirectory() as tmp:
            store = IngressStagingStore(Path(tmp) / "bench.json")
            started = time.perf_counter()
            # The staging store is bounded (64 entries, P1 design), so the
            # realistic admission rhythm is batched: enqueue below the
            # bound, acknowledge, drain, repeat — 500 events in total.
            counter = 0
            for _batch in range(10):
                for _ in range(50):
                    store.enqueue(
                        StagedIngressRecord.from_channel_envelope(
                            _envelope(f"m-{counter}", f"queued {counter}"), scope="s",
                        )
                    )
                    counter += 1
                for record in store.pending_records():
                    store.record_receipt(record.event_id, turn_id="t")
                    store.remove(record.event_id)
            elapsed = time.perf_counter() - started
            print(
                f"\n[E05-C] queue admission, 500 events "
                f"(durable enqueue+receipt+remove): {elapsed*1000:.1f}ms "
                f"rss={_rss_mb():.1f}MB"
            )
            self.assertEqual(store.pending_records(), ())
            self.assertLess(elapsed, 30.0)


if __name__ == "__main__":
    unittest.main()
