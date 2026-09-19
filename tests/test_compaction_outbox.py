"""I08: post-commit candidate outbox — a stage/approval-notify failure after
a committed compact never rolls the seed back; the batch retries via the
outbox and stays a review draft (no automatic L3 write)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pal.core.runtime import PalCore
from pal.memory import MemoryService
from pal.shared import RuntimeStatus

from tests.test_full_compaction_source import _service_with_active


class _CandidateEngine:
    """Barrier-less stub engine whose compact result carries candidates."""

    def __init__(self) -> None:
        self.calls = 0
        from pal.core.compaction import CompactionClockKind

        self.policy = SimpleNamespace(
            policy_id="outbox-test",
            clock_kind=CompactionClockKind.USER_TURN,
            accepts_memory_candidates=True,
        )

    async def run(self, snapshot, *, llm_runtime=None, memory_service=None,
                  after_commit=None, replay_guard=None, commit_guard=None):
        from pal.core.compaction import CompactionRunResult

        self.calls += 1
        memory_result = SimpleNamespace(
            summary="outbox summary",
            projected_entries=[],
            metadata={
                "projected_entry_count": 0,
                "compact_summary_count": 1,
                "retired_count": 0,
                "memory_candidates": [
                    {
                        "kind": "fact",
                        "title": "Outboxed candidate",
                        "summary": "A candidate whose stage failed once.",
                        "source_excerpt": "outbox",
                    }
                ],
            },
        )
        return CompactionRunResult(
            status="compacted", attempts=1, memory_result=memory_result,
        )


def _binding():
    return SimpleNamespace(
        endpoint=SimpleNamespace(endpoint_id="stdio", channel_kind="stdio"),
        response_handle=SimpleNamespace(reply_target={"session_id": "i08"}),
        control_scope_key="scope-i08",
        correlation_id="corr-i08",
    )


def _effect():
    from pal.core.turns import MemoryCompactEffect

    return MemoryCompactEffect(
        assembly_context=None, target_input_budget=8_192, reserved_output_tokens=2_048,
    )


def test_i08_stage_failure_outboxes_and_retries_without_rollback():
    async def scenario():
        core = PalCore()
        service, turn_id, _ = _service_with_active()
        engine = _CandidateEngine()
        core.context.port_registry["memory:memory"] = service
        core.context.port_registry["llm:llm"] = object()
        core.turn_executor._compaction_engine = engine
        continuation = SimpleNamespace(
            turn_id=turn_id, waiting_effect_id=None,
            interrupted=False, interrupt_reason="",
            delivery_binding=_binding(),
            pending_compact_memory_candidate_batches=[],
        )

        reviews = service.reviews
        real_stage = reviews.stage_payload
        stage_calls: list[dict] = []

        def failing_stage(payload, route, *, legacy=False):
            stage_calls.append(dict(payload))
            raise RuntimeError("approval store is down")

        reviews.stage_payload = failing_stage
        result = await core.turn_executor.execute_turn_effect_async(
            continuation, _effect(),
        )
        # The compact itself committed: the seed stands.
        assert result.status == RuntimeStatus.OK
        assert engine.calls == 1
        assert service.compaction_receipts or service.context_epoch >= 0
        # The failed batch sits in the outbox with a diagnostic; nothing
        # rolled back and no L3 auto-write happened (stage never succeeded).
        outbox = core.state.compaction_candidate_outbox
        assert len(outbox) == 1
        assert outbox[0]["batch"]["candidate_batch_id"]
        assert any(
            d.get("kind") == "compaction_candidate_outbox"
            for d in core.state.diagnostics
        )

        # The store recovers; the next drain retries the batch as a draft
        # and empties the outbox.
        def recording_stage(payload, route, *, legacy=False):
            stage_calls.append(dict(payload))
            return real_stage(payload, route, legacy=legacy)

        reviews.stage_payload = recording_stage
        drained = core.turn_executor._drain_compaction_candidate_outbox(service)
        assert drained == 1
        assert core.state.compaction_candidate_outbox == []
        # three stage touches: the failed original, the effect's own
        # post-commit drain retry (store still down), and this manual
        # drain's success — one batch, never duplicated.
        assert len(stage_calls) == 3
    asyncio.run(scenario())


def test_i08_outbox_survives_until_drain_point(tmp_path):
    async def scenario():
        core = PalCore()
        service, turn_id, _ = _service_with_active()
        core.context.port_registry["memory:memory"] = service
        core.context.port_registry["llm:llm"] = object()
        core.turn_executor._compaction_engine = _CandidateEngine()
        continuation = SimpleNamespace(
            turn_id=turn_id, waiting_effect_id=None,
            interrupted=False, interrupt_reason="",
            delivery_binding=_binding(),
            pending_compact_memory_candidate_batches=[],
        )
        reviews = service.reviews

        def failing_stage(payload, route, *, legacy=False):
            raise RuntimeError("still down")

        reviews.stage_payload = failing_stage
        result = await core.turn_executor.execute_turn_effect_async(
            continuation, _effect(),
        )
        assert result.status == RuntimeStatus.OK
        # A still-failing retry keeps the batch queued (never dropped,
        # never duplicated as a second batch).
        kept = core.turn_executor._drain_compaction_candidate_outbox(service)
        assert kept == 0
        assert len(core.state.compaction_candidate_outbox) == 1
    asyncio.run(scenario())
