# P0 BASELINE — refactor/full-context-compaction-v2 (merge tree)

> Historical record, archived on 2026-10-05 from `BASELINE.md`.
> Statements about implementation, deployment and test results describe the
> original work, not the current checkout or running host. See the
> [current documentation index](../../README.md).

Generated: 2026-09-19 (Pal implementer). Per the P0 requirements of the `pal_compaction_handoff_v2` HANDOFF.
This file records only verified facts; the "ready/needs change/not connected" verdicts follow the HANDOFF §12 semantics.

## 1. Environment

| Item | Value |
|---|---|
| worktree | `/home/nathan/Documents/coding/Pal-full-compaction-v2` |
| Branch | `refactor/full-context-compaction-v2` |
| Base | `0e33220e75c9bd011ac06005517161bded66c4e8` (origin/refactor/llm-projection-session-v2, verified identical) |
| main merge | `4daab5f086d5b08753cb254fa7ed2f222874516e` (origin/main, verified identical) |
| merge commit | `614529e347de03719ee00d6ef3b55fa743555def` |
| merge tree | `4a47e242f8b96c8db2c796ffcf9d08d5f3b83012` |
| merge conflicts | none (clean ort merge; main side 5 files: telegram endpoint/provider/pyproject, turn_executor 2-line deletion, bootstrap tests +186 lines) |
| Python | 3.13.5 (system interpreter) |
| pytest | 9.0.3 |
| `pal.__file__` | `/home/nathan/Documents/coding/Pal-full-compaction-v2/src/pal/__init__.py` |
| Test command | `cd <worktree> && PYTHONPATH=$PWD/src python3 -m pytest tests/test_X.py -q` (all tests inside the worktree must use PYTHONPATH=$PWD/src) |
| Number of test files | 153 |

## 2. P0 five-question conclusions (HANDOFF "execute P0 first" item 4)

### 2.1 actual durable inbox / ACK — missing, must be added

- Ingress queue = `core/runtime.py::pending_channel_turns` (in-memory deque; `_schedule_admitted_channel_turn_async` appends when busy at L823-844, L842).
- Dequeue = `_start_next_queued_turn_async` L793-806 (holds `channel_turn_transition_lock`, checks `resident_quiescing` + active).
- Interjection injection = `core/interjection.py::inject_pending_interjection_async` L14-138: initial snapshot under lock (L28-31), final append+ack re-acquires the lock and re-checks queue-head identity (L82-108), append is idempotent by message_id, and on exception it falls back to `contains_l1_message`/active turn to judge "already committed" (L174-205).
- **There is no durable staging whatsoever**: a process crash loses pending items; there is no separate event receipt table — dedup relies on the original text existing in L1, and once compaction compresses the original text away, cross-restart dedup breaks (the gap hit directly by Q08/R03).
- Conclusion: a minimal staging/receipt must be added on top of the existing persistence facilities (no new message bus allowed). Possible hooks: `RuntimeSnapshotCoordinator` (see 2.2) or the memory module's existing persistence layer; the plan is decided in P1.

### 2.2 runtime snapshot root — skeleton exists, install-level atomicity needs wiring and verification

- `core/runtime_state.py`: `RuntimeStatePort` (snapshot/prepare/install/reset, four methods, L13-27); `RuntimeSnapshotIdentity` contains `incarnation`/`sequence`/`producer_fencing_token` (L32-64, the anti-ABA identity already exists); `RuntimeSnapshotCoordinator.snapshot/restore/reset` (L66-118), restore validates each module's schema+identity fail-closed (L85-116, `validate_runtime_snapshot` L152).
- memory module port: `memory/runtime_state.py::MemoryRuntimeStatePort` (L28-139) — snapshots/restores l1_turns (including per-turn revision/state) + l2 entries + top_of_mind + heat.
- Callers: `runtime_app.py` L161/L221 (application-level save/restore), `core/runtime.py` L1665 (soft reset), `bunshin/runner.py` L2883 (worker level).
- Conclusion: the durable root skeleton exists; the single-transaction multi-field write needed for "seed + new empty segment + epoch + receipt committed atomically in one shot" is feasible at the coordinator layer, but the current compact install path (see 2.3/§3) does not go through it at all — P2 must wire install to that root and verify atomicity.

### 2.3 epoch owner — memory-level context epoch does not exist, a single field must be added

- The projection side already has complete fencing: `history_epoch` in `llm/projection_checkpoint.py` (frontier vs. L1 cursor consistency check L224-237) + `owner_fence` (current value separated from historical receipt source fences, L90-93/429-490, hardened in B4/C2).
- But the projection epoch semantics is "projection cursor generation", not the memory owner's semantic context generation. `memory/service.py::compact` (L512-561) and `compact_transactionally` (L563-584) have no epoch concept; `L1TurnStore.replace_all` (`memory/turn_ir.py` L459-473) has no container-level CAS (only per-turn `replace` enforcing revision increments L441-447 and `restore_turn` expected checks L449-457).
- Conclusion: per PLAN §2, add **one** integer context epoch to the memory checkpoint root (+1 on successful install, identity change on reset), reusing RuntimeSnapshotIdentity.incarnation to guard against epoch=0 ABA; do not build a separate one in executor/summary/provider.

### 2.4 final provider payload capture — resident idle segment exists, active cut segment missing

- `turn_executor.py::_resident_compaction_replay_request` (L2041-2113): reads `llm_runtime.prompt_cache_confirmed_anchor_request`, validates continuity_id, then projects the suffix messages after the anchor from the **settled** turns of `build_pack(include_l1_recent_context=False)` — the active turn is entirely absent from the suffix (the W02/F10 gap is real).
- Trigger condition `logical_scope_id == "pal:resident" and continuation is None` (L1915) — i.e., only the resident idle manual path has warm replay.
- hot-gated: `cache_warm_deadline.claim_compaction(cache_epoch)` (runtime.py L1698) + engine `replay_guard` (L2014-2026, TTL/epoch recheck) — the hot-only skeleton that does not steal cold is already in place.
- Prefix/Coverage dual proof (PrefixProof/CoverageProof) does not exist; there is no capture point for actual final payload-level comparison (tools/thinking/hooks invariance). P3 must add an after-hook final payload capture.
- `_scope_safe_snapshot` (compaction.py L188-200) already: strips pal_authored developer context and discards warm replay entirely if stale instructions are inside the replay.

### 2.5 Bunshin scope gate — single-task worker self-consistent, cross-scope coordinator needs scoping

- `bunshin/runner.py`: workers use an independent TurnExecutor + `BunshinCompactionPolicy` (L1517), `compaction_clock_provider=llm_round_count` (L1518); compaction-purpose requests skip llm_round counting/heartbeat (`is_compaction` L259-279, satisfying "compact requests are not counted as ordinary tool rounds").
- Scope identity: `control_scope_key=f"bunshin:{run_id}"` (L872/876); compact_memory_async's scope resolution already distinguishes `pal:resident`/`bunshin:<id>` (executor L1880-1893).
- Missing: coordinator/ticket does not yet exist (added this round); design it with isolation by scope key (I17/Q15); keep the status quo that the Manager proxy has no plaintext private checkpoint channel.

## 3. Line-by-line verdicts on the HANDOFF §12 wiring map

| Line | Actual location (line numbers in this tree) | Verdict |
|---|---|---|
| `agent_turn_program` | turns.py L231-309: preflight advice COMPACT_REQUIRED (L245) and finish_reason COMPACT_REQUIRED (L281) dual triggers; `compact_generation_count>=3` cap (L246/282); MemoryCompactEffect L93 | ready; needs change: wire the safe-round trigger point to the `_after_tool_batch_async` boundary, and handle the issue that the count is not cleared on epoch change per I11 |
| `_handle_llm_preflight` | turn_executor.py L160-202: `_is_hard_budget_overflow` → `context_budget_exhausted` failure signal (L180-201, the wording is the old "active cannot be compacted" semantics) | needs change: split "incompressible base over-window" vs "active compressible over-window", reserving handoff headroom (B01/B02) |
| `_handle_memory_compact`/`compact_memory_async` | executor L204-268 / L1837-2039 (claim/snapshot/generate/install/release are currently all inlined in engine.run, no ticket/gate) | needs change: extract a shared entry + ticket (the core of this package); L1 requires `_ensure_l1_turn_async` to be settled before continuing (L206-218, part of round-safety) |
| `_resident_compaction_replay_request` | executor L2041-2113 | needs change: cover the active cut's anchor+suffix, explicit prefix/coverage proof (W01-W03) |
| `core/compaction.py` | capture L84-128 (active filtering L102-113); engine.run L214+ (max_attempts=3/timeout180s/max_output 64k); `_shrink` L635-664 (pop(0) drops head units — the current "silent source-shrinking" behavior); `_commit` L667-720 (acompact_transactionally+after_commit=retire diff L1984-2012) | needs change: capture includes active, source stamp, warm config inheritance; **forbid** _shrink for full-compaction install (S03: source_too_large) |
| `memory/service.py` / contracts | compact L512-561 (`replace_all([summary_turn, *active_turns])` L543 — active original text preserved); compact_transactionally L563-584 (in-memory rollback, not durable CAS) | needs change: source-aware install, new empty segment + revision + epoch, receipt, durable wiring (§2.2/2.3) |
| `memory/turn_ir.py` / continuity | replace L441-447 / restore_turn L449-457 / replace_all L459-473; `_close_incomplete` L257+ (closing normalize shared with interrupt) | ready: the closing/rollback/revision semantics are reusable; needs change: container-level generation CAS, fresh-segment metadata whitelist copy (F24) |
| `core/runtime.py` / ingress | queue L823-844, dequeue L793-806, manual compact L1687-1773 (double-checks active L1690/1714 but **claim and check are in different critical sections** — the A08/F01 race is real), reset quiesce L1645-1685 (`resident_quiescing` bool, unconditionally False in finally — Q13/F08 hits this existing anti-pattern) | needs change: manual atomic claim, start gate, drain after release, stop/reset priority (P1 main body) |
| `core/interjection.py` | snapshot L28-31 + final critical section L82-108 (structurally exactly the shape the "two-gate checks" want, only missing the ticket check) | Needs change: add the same ticket gate in both places (Q03/Q04); do not rebuild |
| `bunshin/runner.py` | is_compaction L259-279, policy L1517, coordinator L2883 | Needs change: same coordinator, role scope gate, checkpoint restore tied to epoch (P4) |
| runtime_state / checkpoint ports | see §2.2 | Needs wiring: install atomic root (P2), pending inbox restore (P4) |
| cache tracker / projection owner | `prompt_cache_confirmed_anchor_request`/`cache_warm_deadline` (claim_compaction/clear_for_compaction/ignore) | Ready-made (idle hot flow); needs change: handoff as an independent consumer, invalidate the old lineage after install (W08), anchor override of active (P3) |

## 4. Current Behavior Baseline (Semantic Snapshot Before Changes)

- capture excludes the active turn (compaction.py L102-113); install preserves the active turn's original text (service.py L543) — i.e., "full generational replacement" is unimplemented at both ends.
- manual compact only when idle (runtime.py L1690/1714 rejects when busy); hot reminders are hot-only (L1696-1713 + replay_guard).
- auto compact triggers within a turn via both preflight/finish_reason, capped at 3 per turn.
- compact request purpose marking makes Bunshin not count rounds; resident has no such distinction (clock kind=USER_TURN).
- Retiring tool results: after install succeeds, `retire_tool_results` by call_id set difference (executor L1984-2012, via the after_commit transaction boundary).
- After success, `clear_execution_cursors(continuation)` (L2038).

## 5. Known Failures / Environment Items (Declared Before Running the Merged-Tree Composite Baseline)

Carrying over the two environment items already characterized by the 0e33220 full regression:
1. `tests/test_package_installation*.py`: environmental setuptools fail (known baseline).
2. `tests/test_tool_schema_properties.py`: missing `hypothesis_jsonschema`, EXIT 5 (known baseline).
3. `tests/test_bootstrap_and_repositories.py` inherently takes >10 minutes (full runtime provision per test); per-file timeout must be ≥1800s.

## 6. Composite Baseline Regression Results

Command: per-file isolated `PYTHONPATH=$PWD/src python3 -m pytest tests/test_<name>.py -q`, timeout 600s per file (1800s for bootstrap), log `/tmp/fullcomp_v2_baseline.log`.

Status: **DONE (2026-09-19; the suite finished before the 61c87db fix, and the affected scope was re-run all green after the fix)**.

- All 157 files finished running: **2743 passed + 461 subtests passed, 3 failed**.
- 4 non-PASS files, all attributable:
  1. `test_architecture_skeleton` (1 failed) → real bug ①: the `has_open_round` check in `_round_safe_for_compaction` was always true on the real auto path (L1 streaming bookkeeping still open after terminal response) → auto claim always rejected. **Fixed in 61c87db** (removed that admission check, kept the store method); re-run green.
  2. `test_runtime_stability` (1 failed) → real bug ②: P1 injection drained the same-scope burst queue at the turn's first preflight (inconsistent with the "after a tool batch" semantics of the `inject_pending_interjection_async` docstring); the synthetic prompt triggered compact_required, and the stub endpoint could not produce compact JSON → a single recovery reply. **Fixed in 61c87db** (injection admission now adds `llm_round_index >= 1`; the `_after_compaction` drain after compact unchanged), re-run green.
  3. `test_package_installation` (1 failed) → §5 environment item ①, known baseline.
  4. `test_tool_schema_properties` (EXIT 5) → §5 environment item ②, known baseline (missing hypothesis_jsonschema; collection failure not counted as failed).
- Fix verification (affected scope re-run after 61c87db): stability + interjection_injection + compaction_gate + architecture_skeleton = 167 passed; full_compaction_source + warm_handoff + control_plane + cache_wire + memory_l1_ir_service = 125 passed.
- `test_bootstrap_and_repositories` 141 passed / ~13min, completed normally under a 1800s timeout.
- Both real bugs are wiring defects that unit/barrier tests calling internal methods directly could not find — only the full process_channel_turn chain could force them out (same family: the waiting_effect_id self-rejection already fixed in P4).
- Remaining known gaps (corresponding to acceptance NOT_RUN items): none — P6 (2026-09-20, 11 commits from c47d79c..14a3918) implemented or pin-tested all 25 NOT_RUN items; acceptance 108/108 PASS; P6 wrap-up full re-run (15 files including stability/architecture_skeleton) 286 passed. E05 benchmark numbers: 800-item capture 57.8ms; 5000-item full sweep 612.9ms total / RSS +7.2MB; 500 durable admissions 8330ms (fsync-dominated). Durable outbox (crash window fully closed) and E05 re-test on the target machine remain pending decisions.
