# P1 Design Record — Gating and Backpressure (resident host)

> Historical record, archived on 2026-10-05 from `P1_DESIGN.md`.
> Statements about implementation, deployment and test results describe the
> original work, not the current checkout or running host. See the
> [current documentation index](../../README.md).

Per `pal_compaction_handoff_v2` PLAN §4/§5/§6/§13-P1. This document is an implementation decision record for review; acceptance is governed by TEST_MATRIX A/Q/X (P1-phase items) plus acceptance_status.json.

## Scope

- Full wiring for the resident host; Bunshin host wiring in P4 (TurnExecutor new parameters default to None = old behavior, without breaking its current isolation).
- P1 does not change capture/install semantics (P2); warm request construction is untouched (P3). The gate is an outer wrapper around existing compact calls.

## 1. Ticket / Gate (`core/compaction_coordinator.py`, new file)

- `CompactionTicket` (frozen dataclass): `scope`, `op_id` (uuid), `trigger` ("manual"|"manual_hot"|"auto"), `phase` (CLAIMED→GENERATING→READY→COMMITTED→RELEASED terminal state), `cancelled: bool`, `claimed_at_monotonic`, `deadline_seconds`. P2 appends `source_epoch`/`source_stamp_digest` fields (append-only evolution, R04 fail-closed).
- `CompactionGate`: holds `CoreRuntimeState.compaction_tickets: dict[scope, CompactionTicket]`.
  - `claim(scope, trigger)`: **the caller must already hold `channel_turn_transition_lock`** (documented + assertion `lock.locked()`); an existing ticket → None (I05 single admission).
  - `cancel(scope, reason)`: marks cancelled (revoking commit eligibility), does not remove the holder — a dying coroutine still holds the gate until its own release (X01/F07: after the late-arriving finally's identity check, it has no right to release a successor).
  - `release(ticket)`: removed only on op_id identity match; on mismatch → no-op returning False.
  - `is_active(scope)` / `is_cancelled(scope, op_id)`: for barrier checks.
- No second set of unrelated bools is added; `resident_quiescing`/`memory_maintenance` are untouched (Q13/F08: compact release only removes its own ticket and does not clear anyone else's quiesce).

## 2. Three Gate-Check Locations (PLAN §5.3)

1. New-message execution entry `_schedule_admitted_channel_turn_async`: busy condition adds the gate → queue + **durable staging write** (§4).
2. `_start_next_queued_turn_async`: checks the gate under the same lock, does not dequeue.
3. `inject_pending_interjection_async`: the gate is checked at **both** the initial snapshot (inside the already-locked section) and the final append+ack critical section; if either is active → no append, no pop, the message stays queued (Q03); the claim waits for an append already in the critical section to finish first (naturally serialized by the same lock, pinned by Q04 test).

## 3. Ordering Decision: preflight authoritative (replacing _after_tool_batch local estimation)

- `_after_tool_batch_async` **no longer** injects interjections (current behavior: inject immediately after a batch, which would feed new user input into the upcoming compact source).
- The injection point moves to `_handle_llm_preflight`: advice READY and queue non-empty and no gate → inject → rebuild prompt → run preflight once more (bounded at 2 per effect; no injection on the second run). advice COMPACT_REQUIRED → no injection, proceed with MemoryCompactEffect.
- After a successful compact (after `_handle_memory_compact` release), call the runtime injection callback to drain the queue → the next loop-top preflight sees the new messages (Q05: FIFO once each, no running an old task first).
- Messages arriving during handoff are all durably queued (§4), and after release are processed by continuation eligibility.

## 4. Durable staging (`core/ingress_staging.py`, new file)

- `IngressStagingStore`: a single JSON file under the runtime root (ResidentCheckpointStore pattern: tmp+fsync+os.replace+directory fsync), storing pending records + accepted receipts.
- Record: scope, event_id, correlation_id, endpoint (endpoint_id/channel_kind/binding_key/send_policy), reply_target, payload (dict as-is; IR payload uses the existing IR round-trip in `memory/runtime_state.py`), queued_at. **The envelope is fully reconstructed** (ChannelEnvelope with all-JSON primitive fields).
- Order (Q08/R03 skeleton): append L1 → **write receipt (durable)** → pop memory queue/staging; crash gaps are deduplicated by either the receipt or the L1 message_id, dedup key = source event ID, not batch hash.
- Bounds (Q10): 64 entries / 4MiB envelope metadata (configurable starting point); when full → `IngressStagingFull` → the caller **does not send a queued confirmation**, does not pop the oldest; the channel side retries or the user retries.
- Write failure (Q11): exception propagates → no confirmation, no ack.
- Honesty boundary: the staging file's persistence granularity = atomic replacement per enqueue/receipt; **a hard crash loses the window since the last write**, which belongs to the channel-retry domain (consistent with HANDOFF's "the channel may retry and has not been acked"); R-class real crash tests are pinned in P4 with real temp files.

## 5. Triggers and Admission (PLAN §4 decision table landing)

- MANUAL (`_handle_compact_memory_async` refactor): claim and all pre-checks (no active, no outstanding turn_tasks/teardown, `pending_channel_turns` empty = A10 earlier event wins, not memory_maintenance) in the **same critical section**; the status notification is sent only after claim (X08: notification failure does not cancel the gate nor authorize a second claim); no reservation.
- MANUAL_HOT (cache_epoch): same as above + the existing `claim_compaction`/`replay_guard` (hot-only, does not steal-convert cold).
- AUTO (`_handle_memory_compact`): claim after a round-safe determination at the effect entry; round-safe = `_ensure_l1_turn_async` settled + no IN_PROGRESS messages in the active L1 + tool protocol closed (`l1_tool_protocol_validation_error(allow_pending=False)` semantics; verify the function's actual signature at implementation time) + no live `_ActiveRound` in L1 (stream not closed) + `continuation.waiting_effect_id is None`. A04 (unjournaled mutation ledger): verify the existing ledger API in `execution/runtime.py` at implementation time; if present, wire it; if absent, record the gap and defer to P4 fault injection.
- The existing `compact_generation_count>=3`/turn caps are kept (turns.py is not relaxed).

## 6. Controls (PLAN §6)

- `/interrupt`/stop (`_handle_interrupt_turn_async`): with no active turn, if the gate is active → cancel the ticket and reply (X05); with an active turn, interrupt as before (task.cancel naturally interrupts the compact await; the compact-wrapped finally does the identity-checked release, and CancelledError is not swallowed into a half-state).
- Confirmed reset: inside the quiesce critical section, first `gate.cancel_all("reset")`, then proceed with the original reset (X04: old tickets invalidated, the reset's new generation is not overwritten by late summaries).
- shutdown: the existing shutdown checkpoint path interrupts active turns → naturally cancelled as above; the staging file persists with the runtime root.

## 7. Tests

- New file `tests/test_compaction_gate.py` (the P1 items of A/Q/X): reuse the interjection tests' real fixture style (real CoreRuntimeState/TurnExecutor/MemoryService + fake LLM + **barrier-style stub engine** (overriding `run()` to await an asyncio.Event)), zero real sleeps.
- Manual/auto, races, backpressure, and cancellation each use asyncio.Event barriers to control timing; each maps to a TEST_MATRIX ID.

## 8. Deviations Determined During Implementation (2026-09-19, none conflicting with the spec)

1. **Drain on failure too**: release's finally uniformly calls `_start_next_queued_turn_async()` (success and failure share the same path; Q06 requires that queued input not get stuck after a failed release). After success there is additionally an executor-internal after_compaction injection (for the active-turn scenario, entering L1 before the next preflight).
2. **Typed ingress first**: by the queue entry, the dict payload has already been compiled into LLMMessageIR; the staging record uses payload_kind="message_ir", with the llm/serde round-trip preserving type fidelity.
3. **The direct execution path also checks the gate**: `process_channel_turn_async` (synchronous API) adds the gate check to the quiescing/dreaming checks; I03 leaves no bypass.
4. **A04 status**: there is no publicly queryable effect ledger ("outcome unknown" is internal semantics of the tool result); round-safe = protocol pairing + stream closure + no in-flight effect + effect ordering. A04's full semantics (unjournaled mutation fault injection) belong to P4; the gap is recorded in BASELINE.md.
5. **X01 active-turn interruption**: interrupt_active_turn's task.cancel naturally interrupts the compact await, with the finally doing the identity-checked release; the ticket is simultaneously marked cancelled. Mid-flight revocation of HTTP billing in the engine is not promised (P2 adds a cancel event to engine.run).

## 9. Explicitly Not in P1

- capture includes active, new empty segment/epoch/receipt installation (P2)
- warm anchor covers active, prefix/coverage proof (P3)
- Bunshin gate wiring, real crash recovery matrix, Manager proxy (P4)
- full regression + 108-item summary (P5)
