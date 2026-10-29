# P2 Design Record — Full-source capture and atomic install

> Historical record, archived on 2026-10-05 from `P2_DESIGN.md`.
> Statements about implementation, deployment and test results describe the
> original work, not the current checkout or running host. See the
> [current documentation index](../../README.md).

Per PLAN §7/§10/§13-P2 and TEST_MATRIX S01-S10, I01-I14 (P2 portion), E01 cold variant.

## 1. Data contracts (memory/contracts.py)

- `MemoryCompactRequest` additions (default = old local semantics; paths calling the old compact directly are unchanged):
  - `op_id`: install idempotency key (receipt primary key)
  - `source_stamp`: digest over the turns sequence at capture time (non-empty means full-compaction mode)
  - `active_turn_id`: the logical turn that needs a new empty successor segment (empty for idle manual)
  - `expected_epoch`: the memory context epoch at capture time
- `CompactionReceipt`(frozen):op_id, status=committed, epoch_before/after, summary_source_id, successor_turn_id/revision, removed_turn_ids, removed_result_refs, cleanup_status(ok|pending), created_at.
- `StaleCompactionSource(ValueError)`: explicit type for CAS/epoch failures (I03/I12/F18).

## 2. Source stamp (memory/turn_ir.py, emitted by the L1 owner)

`source_stamp_for_turns(turns)` = sha256 over each turn's `turn_id:state:revision:message_count:message_id list`.
Capture and install validation **use the same function**; callers cannot fake it by filling in block_count themselves (PLAN §11).

## 3. MemoryService full-compaction install (memory/service.py)

- New state: `context_epoch: int = 0` (incremented by +1 on successful install; `soft_reset` also +1 to change identity, PLAN §2); `compaction_receipts: dict[op_id, CompactionReceipt]` (capacity 256, oldest dropped beyond that — receipts are idempotency facts, not an unbounded ledger).
- At the top of `compact(request)`: if `request.source_stamp` is non-empty → full-compaction branch:
  1. **Idempotency** (I05/I06): if receipts already contain the same op_id → return the recorded result if the stamp matches (no re-run, no second epoch bump); different stamp → raise (a different candidate reusing the same op_id is rejected).
  2. **CAS** (I03/I06/F18): if `source_stamp_for_turns(current) != request.source_stamp` → raise StaleCompactionSource (never restore a capture snapshot over newer data).
  3. **epoch**(I12):`context_epoch != expected_epoch` → raise StaleCompactionSource.
  4. Construction: summary_turn (using the current continuity channel) +, when `active_turn_id` exists, a new empty successor segment `L1TurnIR(same turn_id, ACTIVE, revision=old+1, messages=(), metadata={"compact_successor": True, "compact_op_id": op_id})` — metadata allows only these two keys via whitelist (F24). `begin_l1_turn` returns an existing ACTIVE turn as-is → I10 holds naturally (no re-feeding of the opening).
  5. Single commit section (I04/I09): `replace_all([summary_turn, *successor])` + `remove_projected_entries` + `context_epoch += 1` + receipt write, all as non-raising local assignments; all raising steps (validation/construction) happen earlier.
- `compact_transactionally(request, after_commit)` full-compaction branch: if compact succeeds and `after_commit()` then raises → **no rollback**; set `receipt.cleanup_status="pending"` and swallow the exception (F21/I07: cleanup failure must not masquerade as uncommitted); the old local path's rollback semantics are unchanged.
- `mark_compaction_cleanup_pending(op_id)`: the I07 retry hook.
- soft_reset: epoch+1; receipts are kept as historical facts (replaying an old op_id is rejected by the stamp/epoch CAS, so old seeds are not revived, X04/R07).

## 4. Capture and engine (core/compaction.py)

- `CompactionSnapshot` adds `source_stamp`, `source_epoch`, `active_turn_ids`.
- `capture(..., include_active=True, source_epoch=0)`: in full mode, take the transcript directly from the `turns` sequence (`_transcript_from_turn` projection, S01 "same revision set"), and compute the stamp with the §2 function over turns; `include_active=False` preserves the old behavior for existing direct callers.
- engine: when `snapshot.source_stamp` is non-empty (full-compaction mode), preflight COMPACT_REQUIRED / finish_reason COMPACT_REQUIRED no longer goes through `_shrink` — return `source_too_large`, leaving the original text/epoch untouched (S03/S04; shrink is unreachable in the full-compaction path, and the method is kept pending P5 cleanup). The replay branch likewise falls to this terminal state when over the window.
- `_commit` forwards the snapshot's stamp/epoch/op_id/active_turn_id when constructing the MemoryCompactRequest (active is taken from the unique entry in snapshot.active_turn_ids; multiple → S09 rejects at the service layer).

## 5. Executor wiring (core/turn_executor.py)

- `compact_memory_async`: capture with `include_active=True` and `source_epoch=memory_service.context_epoch`; active_turn_ids = ACTIVE turns in the store (in the auto path, that is the continuation's turn; empty for idle manual); generate `op_id` as a uuid, attach it to the snapshot metadata and pass it along with the request.
- The existing after_compact (retire set difference) gains committed+pending semantics via the transactionally full-compaction branch (I07).

## 6. Persistence (memory/runtime_state.py)

The snapshot payload adds `context_epoch` and `compaction_receipts`:
- Present but invalid (non-int/negative/bool, or malformed receipt structure) → restore raises (R04 fail-closed; no fallback to the old format).
- Genuinely missing (old checkpoint) → controlled migration with `context_epoch=0` + a log entry (R05).
- P2 tests use MemoryRuntimeStatePort round-trip as checkpoint storage evidence; the real process-restart recovery matrix belongs to P4.

## 7. Explicitly out of scope for P2

- warm request construction / prefix+coverage proof / anchor covering active (P3)
- Bunshin host wiring, real crash recovery, the full await/commit fault-injection matrix (P4)
- Budget split refinement (B01-B05 headroom reserved), full regression and the 108-item summary (P5)

## 8. E01 cold variant (vertical acceptance, in tests/test_full_compaction_source.py)

Following E2E_RECIPES steps 1-10 (steps 11/12, restart and replay, are P4): real engine + scripted LLM + real MemoryService + staging; SEED0/Q_ORIGINAL/A_DECISION/call A/RESULT_A sentinels; barrier at generate; meanwhile M=ACTUAL_NEW_CORRECTION queues durably, L1 is frozen, and the epoch is unchanged; after install, seed1 + a new empty segment (same turn_id, revision+1, epoch+1, receipt); M enters the new context exactly once; in the next preflight request, seed1 and M each appear once, and Q/A/result originals no longer appear raw; tool A's execution count remains 1; budget/guard continuity is preserved.
