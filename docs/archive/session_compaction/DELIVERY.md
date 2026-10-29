# DELIVERY — pal-two-segment-v3 (refactor/two-segment-session-v3)

> Historical record, archived on 2026-10-05 from `DELIVERY.md`.
> Statements about implementation, deployment and test results describe the
> original work, not the current checkout or running host. See the
> [current documentation index](../../README.md).

> Raw `logs_*.txt` references below use their original root filenames. The files
> are now retained locally under `test-logs/legacy/`; see the
> [test log summary](test_log_summary.md) for results, checksums and Git recovery.

Status: **two_segment as the sole mode (v2 physically deleted) + closure items still pending**. Baseline `7e6773f`; v2 delivery facts are carried by git history. No new commits pushed, not merged to main, not deployed.

## 1. Formal gate(G0)

- tla2tools.jar SHA `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88` (byte-identical between the official v1.7.4 release and the local copy)
- Real SANY / TLC safety / liveness / large-parameter positive cases + 6 mutant counterexamples: **10/10 PASS** (`~/Documents/coding/pal_two_segment_v3_20260920/logs/tlc_results.json`)
- `implementation_gate.py` exit 0; TLC 2923 distinct states match the in-package Python reference model

## 2. Commit chain (on top of the baseline)

| Commit | Gate | Content |
|---|---|---|
| `04da350` | G1 | `memory/history_root.py` two-segment authority: cut/promote/replace-left/final-state arbitration/producer token; full L/C/X owner-level family, 33 tests |
| `45e1623` | G2 | projection made two-segment: `semantic_span`, `prepare_handoff`, `on_left_replaced`, `rebind`; three-shape codec oracle (P01-P08 + W side) |
| `cb39341` | G3(a) | MemoryService facade: `history_root` (lazy self-healing)/`left_transcripts`/`begin_left_compaction`/`compact_left` |
| `939db45` | G3(b) | I18: builder publicly forwards the compaction four parameters; removed runtime/bunshin private writes after build |
| `7aa4030` | G3(c)-1 | executor `compaction_mode='two_segment'` left-segment flow: promote→begin→capture_left→engine→compact_left (cold-left) |
| `eb25fdc` | G3(c)-2 | interrupt/reset dual-mode routing (two_segment goes through root arbitration); root self-healing upgraded |
| `352d831` | G3(c)-3 | Admission branching: v2 ticket/ledger/outbox/lease machinery kept only in full_source; staging auto-wiring is switched off by mode |
| `0695976` | G3(d) | H01 twelve combinations (2 hosts × 3 shapes × 2 modes) real executor chain + retire hook sharing fix (caught a real UnboundLocalError bug) |
| `0bb2b31` | G4 | owner-level B01/B03/Q03 pinning tests |
| `5e042c6` | Follow-up | **projection session wired into the real chain**: LLMRuntime manages per-scope sessions (live endpoint binding, single rebind on endpoint switch); after two_segment install, drives on_left_replaced (F13 does not roll back) |
| `e58d1cd` | N1 | review package F1/F2/F5/F6 fixes: root final-state closure (failure/cancel/timeout/deadline gates), left stamp changed to immutable content basis + settlement no longer rewrites the cut-covered messages, slimmed final-state records + reset clears archives, IN_PROGRESS not admitted into closed groups; `tests/test_v3_n1_root_lifecycle.py` 9 red→10 green, red evidence `logs_n1_prefix_red.txt` |
| `5a7258a` | N1 tightening | Self-audit against origin package PLAN §5.2: stamp covers all message identity fields (parts/semantic_kind/prompt_region/replay/metadata) and binds to the owner-issued cut version; added a metadata tampering negative case, N1 suite 11/11; 73 files 1225+285 subtests, zero failures |
| `7d6eb0d` | N1.1 | review package pal_v3_n1_review_29a879f four items: R1 promote sealing (complete neutral reasoning retirement before freeze, settle/interrupt no longer conflict), R2 single final-state exit (post-commit packaging faults classified by owner final state: success+memory_result+rebase; late close after reset idempotently calls close_run), R3 pre-release final gate (READY/identity/deadline re-verification, >= unified), R4 stamp adds message.state; 12 new tests 7 red→12 green, 74 files 1237+285, zero failures |
| `a8530e7` | N2 | review F3/F4/W2: handoff changed to base+L+instruction single full encoding (aligned with the three-shape oracle, no longer drops in-container preamble); rebase replays surviving chunks' original wire (native fidelity) + pending tail fully preserved + head-system attributed by span + left_revision monotonicity check; executor passes the real frozen R (frozen_message_ids) + epoch from the root; PreparedRequest carries the complete wire contract (spans/extra_body/breakpoints); checkpoint adds semantic_span; red→5+5sub green, 75 files 1242+290, zero failures |
| `702f1bc` | N2/N11 | Seed owned by the cut goes through project_standalone as an independent L reference (never bound into R's user message); normal/handoff share the same L projection; existing summary projections are not double-annotated; full_source anchor behavior unchanged; 1 red→3 green, 13 files 186+17sub, zero failures |
| `7d75ddb` | N3 slice 1 | invoker encode point accepts the owner-prepared immutable projection (typed parameter, not smuggled via metadata); without a projection, the codec path is byte-identical; 2 new tests + 97 affected files 1443+347sub, zero failures; remaining N3: runtime owner-side preparation (W3), accept→observe_commit closed loop (W5), full vertical trace |
| `5642542` | N3 main body | **ordinary turns genuinely go through projection**: executor owner-side prepare (durable-id filtering, transient content falls back cold and is never frozen) → typed immutable (EncodedRequest+Binding) handed down → runtime W3 endpoint matching (mismatch means cold) → invoker sends → real decode/accept → observe_commit freeze (ERROR turns reject without freezing); `test_v3_n3_vertical_trace.py` two-turn trace: frozen block spans cover q+a, second-turn payload contains each piece of content exactly once; 98 files 1445+347sub, zero failures |
| `695efbc` | N4 slice | W1/N20: request-boundary promote closes old groups (long-lived turns no longer permanently no_benefit); cut rule fix: the closed prefix must not end with a dangling user message (L01's old assertion contradicted its nominal intent, now aligned with PLAN §3.3); J7 staleness guard: root.left_generation (only skipped on install) vs session.history_left_revision, if rebase has not consumed it then cold fallback instead of replaying retired history |
| `1900125` | N7 (pal_v3_af51d74_review) | All six review items F1-F6 fixed: F1 `prepare_generation_plan` derives an immutable prepared plan in one shot (resolved endpoint + compiled effective request + strong binding: real spec digest / continuation contract version / capability-profile-overriding config fingerprint; spec-1/policy-1/provider:base_url placeholders all eliminated); projection encodes effective_request and generate/astream reuse the compiled result as-is on the plan endpoint; F2 typed `ProjectionSendReceipt` (attempt id + resolved endpoint + applied + native) returned with LLMGenerationResult / `last_projection_receipt` (stream); observe_commit is only authorized by a matching applied receipt; endpoint fallback reports applied=False and does not freeze; sync stale-spec recursion no longer drops projection parameters (aligned with stream); F3 invoker hands NativeCapture candidates back through native_sink per attempt, applied receipt carried to the owner, real continuation policy: manifest match and PRESERVED → byte-true native_committed freeze, mismatch (boundary replacement/DSML promotion) → discard native, IR-only freeze, downgrade → refuse freeze and fall back cold; F4 explicit AcceptedContribution boundary `_finalize_accepted_contribution` (finalization replacement precedes projection commit, receipts surviving replacement are not dropped from the frozen contribution); F5 pending tail entries carry span attribution (`_PendingWireItem`), chunk adds aligned `item_spans` (checkpoint round-trip), on_left_replaced retires pending and retired spans within surviving chunks byte-wise by kept_set; F6 soft_reset explicitly rolls the HistoryRoot incarnation (no longer inferred self-healing from length), reset flow retires LLMRuntime-managed projection sessions, owner prepare adds a structural guard: if lineage-frozen spans are no longer durable, fall back cold; `test_v3_af51d74_review_fixes.py` 12 red→12 green, 19 adjacent suites 192+67sub, zero regressions |
| `3eb036b` | N7 fixup | native_sink is only passed to invokers whose send method signature accepts it (calling subclasses with narrowed signatures raises TypeError, counted as endpoint failure — full run exposed 3 real regressions in test_llm_runtime_ir stream, run alone 3/3 reproduced and fixed to all green); full regression (1900125+this fix): 3134 passed + 491 subtests + 7 skipped, 25 failed = 3 fixed real regressions + 19 node + 3 subtest lines exactly the same set as the previous full run, run alone 20/20 zero reproductions (logs_flake_rerun_af51d74fix.txt), attributed to same-family long-run contention |
| `15196dd` | Main item 1 | warm handoff split: after the begin_left_compaction fence, executor takes the final LEFT ids and calls `_resident_compaction_replay_request(left_message_ids=...)` (anchor prefix equality check, anchor must lie entirely within L, honest cold fallback on non-compliance); test_v3_warm_handoff_split 5 red→6 green, 138 adjacent, zero regressions |
| `3621a74` | Main item 2 | prompt_cache read-evidence confirmation (`_AnchorReadEvidence`: confirm only when cached_input_tokens ≥ prefix_tokens and on a later sequence, resident-only, TTL; rebuilt within the dffb210 boundary); projection_session prepare produces spans (tail spans remapped including anthropic merge, otherwise explicit cache planning is bypassed); tests/test_v3_real_provider_e2e.py (gated PAL_V3_E2E, glm/deepseek/openrouter-luna real runs passed) |
| `1e8b42d` | Main item 3 | **v2 physically deleted + two_segment as the sole mode + ownership reclaimed** (see §3a/§4/§6 for details) |
| `95373ef` | I11 | cache diagnostics byte-self-sufficient enumeration: span downgraded to a path map, unclaimed items inherit by region; astra chain 10→6 (remaining 6 pending, see §4.1, closed this round by M17) |
| `a1058c5` | N8 implementation | Main body of fixes for review 95373ef R1-R6/S1/S2 (see §9 for details): config digest admission / strip native at replacement boundary / L view coverage (seed_reference_ids) / head preamble + real merge coordinates / full-wire diagnostics enumeration / attempt owner-match closure / eligible anchor read side / shell encodes only the preamble |
| `a923583` | N8 tests | `tests/test_v3_review_95373ef_fixes.py` 19 cases (reviewer draft's 9 cases with fixtures fixed and assertions not weakened + M16/M18 + M04 positive/negative + M05 exception variants + M06 late cleanup); M17 migration `test_prompt_cache_v2_runtime.py` to the real v3 compaction entry + byte-truth retirement assertions; warm_handoff_split eligible reader |
| This commit | N8 evidence | DELIVERY §9 + red baseline / full regression logs (citing same-tree tests a1058c5+a923583) |
| This commit | N8 follow-up | M12 seam variant closure: test_completion_system_seam_paths_shift_with_the_head (20/20 green), M12 PARTIAL→PASS; remaining ledger ordering see §4.6 |
| `faf2f41` | H-family closure | Implementation closure of Q04/B04-B08 (see §4.4): Q04 BUSY_RETRY receipt, B05 base_over_budget terminal state + cause reporting, B04/B06/B07/B08 honest executor/engine nodes; H04/H06 OBSOLETE, H02 pending |
| This commit | H-family ledger | acceptance_status 46/72 PASS (fill_ledger run per node @ faf2f41); §4.4/§4.6/N21 synchronized |
| `3a7bbe9` | H02+H03 | Graceful-stop owner state persisted with resident checkpoint (see §4.4): HistoryRoot.owner_state/restore_owner_state + memory runtime schema 2→3; H02 app-level SIGTERM chain end-to-end, fail-closed on corruption; H03 old checkpoints readable with no residue |
| This commit | H02 ledger | acceptance_status 48/72 PASS (fill_ledger run per node @ 3a7bbe9); §4.4 synchronized; branch first push (refactor/two-segment-session-v3, not merged to main) |
| `faee0dd` | C closure | review pal_v3_review_c9cb2d2 three fixes (C1 complete cold source / C2 TEXT-ONLY handoff acceptance / C3 one-shot projection bootstrap after restore·rebind) + `tests/test_v3_c9cb2d2_review_fixes.py` 11 nodes (see §10 for details) |
| This commit | C evidence | DELIVERY §10 + full regression log `logs_full_regression_c9cb2d2_review_fixes.txt` (22 failed items identical item-by-item to the known long-run family set) |
| `af400d9` | B1 closure | review pal_v3_review_4b14ce4 B1: bootstrap coverage covers the entire materialized L view (seed_coverage_ids established by the same encode; restore/rebind no longer re-encodes already-promoted ordinary history into the tail) + `tests/test_v3_4b14ce4_review_fixes.py` B02/B04/B05/B06 four nodes + C02 hardening (see §11 for details) |
| This commit | B1 evidence | DELIVERY §11 + full regression log `logs_full_regression_4b14ce4_review_fixes.txt` (22 failed items identical item-by-item to the previous long-run set; 3098 passed + 533 subtests) |

## 3. Design Points

- **Single physical history**: L/R are logical cut views over L1TurnStore, no mirrored history (A03)
- **Compact only replaces the left**: capture_left reads only the left segment (I10); install publishes a single segment without await, R preserved verbatim (I05/F04, active round content is folded into the physical record rather than rejected)
- **Identity separation**: left_revision does not fence R producers; only session incarnation (reset) fences (I09/X05/X06)
- **Admission**: the owner is the arbiter (single live run / terminal-state mutual exclusion / L08 minimal seed prevents loops); manual /compact uses the gate primitive as an idle gate (admission differs, primitive is the same, MIGRATION)
- **projection rebase**: chunks bind to semantic spans; replacing the left segment keeps/deletes by span, and R's original wire/native bytes are preserved; the anthropic user seam falls back to pending-tail reusing the F2 mechanism

## 3a. v2 Physical Deletion List (this commit; v2 never shipped, no compatibility burden)

- **Deleted**: the entire `ingress_staging.py` file; the staged-records branch in `artifact_lease.py` (the R08 L1 reference keep-alive is kept); the turn_executor v2 admission block (ticket/no-progress ledger/candidate outbox/lease/Q12); the `compact_memory_async` whole-source branch and the `CompactionSnapshot.capture` call; the full mode-switch set (`llm_compaction_mode` config, the `_compaction_mode`/`_two_segment_compaction`/`compaction_gate`/`compaction_scope` parameters and attributes); Bunshin/worker carrier/gate; runtime_app ingress auto-wiring; the contracts' `compaction_no_progress`/`compaction_candidate_outbox` fields; the staging exception surface for runtime manual compaction; the gate file's v2 semantics tests (interrupt cancels idle manual — not canceled after CPT-01; reset directly withdraws the ticket) and the x10/b09 ledger tests; the three files test_full_compaction_source/outbox/builder_mode_contract (fixtures moved to tests/test_compaction_fixtures.py)
- **Kept (MIGRATION rationale)**: the CompactionGate/Ticket primitives ("admission differs, primitive is the same") — the idle gate for manual /compact; the interjection's two gates (auto passes R growth without a ticket, manual blocks the queue with a ticket); the R08 lease; the full manual /compact handler set (A06/A08/A10/X06/X07/X08)
- **Ownership consolidation**: MemoryService adds the intent façades `interrupt_compaction_for_turn`/`cancel_active_compaction`, and runtime.py no longer digs three layers deep into history_root internals; require_port replaces the nonexistent get_port (see §6.5)

## 4. Honest Boundaries (acceptance ledger synchronized with NOT_RUN)

0. **Main items 1/2 delivered** (15196dd warm split, 3621a74 read evidence + spans + E2E); main item 3, v2 physical deletion + mode ownership consolidation, is complete; full regression 49 failed attribution: 17 real regressions fixed (reproduced individually + fix verified), 10 prompt_cache_v2 astra/chain failures are flip-predating blockers (see 1; drops to 6 after the later diagnostics-layer fix), the remaining 22 are the same set as the pre-existing long-run contention family (sampled individually, all green)
1. **astra chain prefix_preserved 10 failures (flip-predating) — diagnostics layer fixed, remaining 6 pending with converging attribution**: epistemic ruling (Nathan 2026-09-21, anchored on PLAN §7/I11/I12/F15): markers are not evidence, the receipt (usage cached tokens) is the only confirmation of a hit, and the only local claim is outbound byte-level identity; the frozen prefix span not traveling along is a design consequence of §6.2 "do not rewrite stable prefixes," not a defect; the error was describe_request using span existence as the enumeration threshold (span-less message bytes evaporate → asymmetric item stream → false prefix_changed). Fix (diagnostics-layer commit): byte self-sufficient enumeration — all session-container items are described in full, with spans demoted to a path map (unclaimed items inherit the span-less message region, mixed defaults to history); red-first contract test tests/test_cache_diagnostics.py (cold→projected asymmetry still reports prefix_preserved + a guardrail that real byte drift still reports prefix_changed). Result 10→6: all 4 explicit/hybrid chain cases green, zero collateral in the cache family. **The remaining 6 share the same root family: the harness projection observe/promote loop is not closed** (est flat at 6458 throughout, cache_tail=False never prepared; after [compaction] round 4 old history items are kept as-is = never promoted into L, the summary only appends and reports history_appended while the test expects prefix_changed; [dynamic] additionally includes encode not declaring dynamic context items — the 276B unclaimed item enters the prefix comparison per byte truth). The fix belongs to the encode/planning layer, outside the "fix diagnostics not projection" slice; pending with Nathan. **Environment warning**: the local user-site editable install (pal_v2→~/Documents/coding/Pal main@4daab5f) hijacks imports; worktree tests must set PYTHONPATH=src (this once caused a false-green misjudgment, since corrected). **[N8/M17 closed (a923583)]** The remaining 6 are attributed and closed: the root cause was the test-side legacy `memory.compact` shortcut bypassing the real v3 promote/observe loop (flat est, cache_tail never prepared, old history items kept as-is were all artifacts of that shortcut); after migrating to the real v3 entry point (left run + cut + install + rebase, with transport responding a schema-valid summary) the whole file is green, and byte-truth retirement assertions were added (the compacted-away left segment's reasoning is not replayed, the surviving segment is byte-for-byte identical, and summary requests are accounted separately). See §9/M17
2. **[Closed (later commit today)] Engine whole-source internal residue**: physically deleted — the `CompactionSnapshot.capture` (whole-source producer) and the engine's MemoryCompactRequest fallback install chain (the acompact_transactionally/compact_transactionally/acompact/compact getattr ladder) were changed to honest RuntimeError guards (a hand-rolled snapshot without two_segment_run_id has no supported install path); `MemoryService.compact/_compact_full_source/compact_transactionally/acompact_transactionally/acompact/mark_compaction_cleanup_pending` all deleted; `MemoryCompactRequest` kept as a named tombstone (stale imports fail loudly), four methods removed from the port interface; `pal.memory` exports cleaned. **Kept**: the engine's `source_stamp` branch (414/487) is not full-source specific — capture_left also sets the left stamp; it is the general "stamped snapshot" rule (S03/S04 excess separation); the `l1_source_stamp` read-only façade. Test side: deleted 4 whole files (test_compact_continuity / test_p6_fault_matrix / test_p6_perf_ab / test_compaction_diagnostics) + 3 items in test_p6_matrix_gaps + 28 items in test_runtime_compaction (22 engine-family + 5 policy + 1 transaction) + the empty class CompactionPolicyTests + the dead helpers `_snapshot`/`_capture_warm` + 1 skeleton item + 2 Bunshin items + 5 warm_handoff items + 1 gated real-engine item; migrated 2 policy-tolerance tests to the real v3 path (begin_left_compaction + capture_left + compact_left install, truer than the original whole-source stand-in); n11's MemoryCompactResult now points to pal.memory. Honest boundary: some engine-general contracts (the checkpoint 20k cap, retries not inflating attempts, three-failure isolation, etc.) were previously only covered via the whole-source stand-in; after deletion there are no v3-equivalent unit tests — pending to add v3 versions later as needed
3. **[Closed (later commit today)] v2 flake-family clean-environment rerun attribution**: full worktree rerun on an idle machine (ollama empty, no concurrent load) in 1454s: **22 failed (19 FAILED + 3 SUBFAILED), exactly the same set as the established family** — the fifth consecutive identical set (3575e48/29a879f/af51d74fix/N8 mainline/this idle run); individually, 19/19+3 sub are green. Attribution ruling: timing/order sensitivity of the serial full long run (the bootstrap-telegram / bunshin_sandbox / bunshin_v2_public / browser_cli / control_plane families), not ollama memory pressure, not any v3 change, not a real regression; if a root cure is ever wanted it requires per-test isolation attribution (event-loop/port/environment-variable leak class), and it is not a v3 closure blocker. Environment methodology note: switching venv/cloning would be over-engineering — past baselines used the same environment and the same PYTHONPATH discipline; the attribution variable is machine load, not dependencies; the host shell's global PYTHONPATH carried pal_v2 src (the hijack source), and the worktree prefix discipline suppressed it
4. **[Closed (faf2f41 + 3a7bbe9)] H-family runtime item classification consolidation**: Q04/B04-B08 implemented with honest nodes (Q04=test_compaction_gate BUSY_RETRY receipt; B04-B08=tests/test_v3_b_family_runtime.py, real executor/engine/owner, network side faked); B05 code consolidation = engine adds `base_over_budget` terminal state (empty source probe still over window → zero generations, no deleting base to squeeze the window) + manual compact reports the reason explicitly. H04/H06 judged OBSOLETE (v2 never launched: no legacy schema L1, no legacy staging files to migrate, no honest inputs). **H02/H03 closed (3a7bbe9, ticket opened by Nathan)**: graceful-stop resident checkpoint now carries two-segment owner state (incarnation/cut/left_generation/abandoned_run; memory schema 2→3, fail-closed cross-version restore); the recovering process sees full old/new roots, replay envelope preserved to rebuild native associations, active compact run voided rather than rerun, corrupted owner state rejected before install; H03 = on shutdown write failure the old checkpoint remains readable with no residue. Blast radius: 170+24sub (faf2f41) / 232+24sub (3a7bbe9, 18 files including bunshin/behavior/runtime_state), all green
5. bootstrap full regression requires ≥1800s timeout (existing v2 discipline; measured 1790s this round, ≥2400s recommended going forward)
6. **N8 follow-up plan (2026-09-22, arranged with Nathan's authorization)**: ① M10 three-path final-wire side-by-side unit tests (✅ closed, tests/test_v3_m10_final_wire.py); ② physical deletion of whole-source engine remnants (✅ see 2); ③ acceptance_status backfill (✅ fill_ledger actually run per node); ④ 22 long-run family clean-environment rerun (✅ see 3); ⑤ H-family runtime items (✅ see 4, Q04/B04-B08 implemented + H04/H06 OBSOLETE; H02/H03 subsequently closed by 3a7bbe9). M12 seam variant closed (b80fd1b)

## 5. Acceptance evidence

- v3 new test families (rerun at final HEAD): history_root 33 / projection_two_segment 14+12sub / memory facade 7 / executor flow 3 / builder 2 / H01 1+12sub / budget facts 3 — all green
- N1→N4 new suites: n1_root_lifecycle 12 / n11_followup 12 / n2_projection_fixes 5+5sub / n2_continuity 3 / n3_invoker 3 / n3_vertical 4 (including full promote→compact chain and stale-left) — all green
- Inherited families spot-checked per gate: runtime_compaction / full_compaction_source / hot_cache / memory / history / projection full set / bunshin harnesses / hosts_recovery — green
- Full regression (HEAD `3eb036b`, 25:47): **3134 passed + 491 subtests + 7 skipped, 25 failed**. Of these, 3 are true regressions introduced this round (native_sink vs narrowed invoker signature, test_llm_runtime_ir stream) — 3/3 reproduce when run alone, all suites green after fix; the remaining 19 nodes + 3 subtest lines are exactly the same set as the previous full run (695efbc), **20/20 isolated reruns: zero reproductions** (logs_flake_rerun_af51d74fix.txt), same family as the three runs at 3575e48/29a879f, attributed to long-run resource contention/timing. No claim of full-suite green; clean-environment rerun remains the final attribution step
- [acceptance_status.json](acceptance_status.json): 41/72 PASS (each item carries real node/command/exit/product_sha, written after per-node actual re-run by the then-current fill_ledger_v3.py); the remaining 31 items are NOT_RUN with reason notes on key items (no fill-script impersonation); live status of the 28-item closure matrix in §8

## 6. Real bugs surfaced by the full chain (this task)

1. `compact_memory_async` two_segment branch referenced `after_compact` defined after it (UnboundLocalError) — no test previously truly drove this entry point; exposed on the first H01 matrix run; retire hook moved before the mode branch and shared (`0695976`)
2. `right_turns()` puts the boundary turn back into R twice when intra==len (found in G1 self-review)
3. After anthropic rebase, prefix trailing user seam not merged → fell back to pending tail reuse F2 (G2)
4. **N1 terminal-state exit (closed by N1.1)**: all post-commit failures (packaging exceptions, timeout, external cancellation) are classified by owner terminal state — if already COMMITTED, report success + accepted memory_result + drive rebase, never fabricate empty success or claim "history unchanged"; resetting an already-retired run idempotently completes without raising StaleRun. The previously described window of "wait_for timer firing exactly between commit and return" was not separately proven reachable (timeout callbacks do not fire inside a synchronous section of a single event loop), and it is covered by the same classification path, so it is no longer treated as a separate boundary
5. **get_port fake-green chain (first strike at flip)**: the two_segment branch written in eb25fdc/1900125 called the nonexistent `MainContext.get_port`; full_source default made it unreachable, so flipping detonated it: /interrupt crashed with AttributeError, reset-side X04/X05 silently no-op'd, F6 lineage retirement silently no-op'd (the original test called llm runtime methods directly; PalCore-side wiring was never truly exercised — fake green). Fix: façade reclaims ownership + require_port (no swallowing) + real context wiring test
6. **Two stub drift cases**: hosts' `_BarrierEngine` lacked `timeout_seconds/max_attempts` (v3 deadline unreadable → task dies silently → test hangs; and a NameError left an unfinished barrier task deadlocking the event loop); hot_cache fixture's canned anchor had only a single message (under v3 W02/W03 prefix-equality validation it could never join) — both aligned with the real engine contract
7. **Collateral exposed by the flip**: v3 left binding did not advance `context_epoch` (i11); compact_memory_async v3 branch ignored the passed-in service parameter and forced require_port (hot_cache direct-call family all broken); `_promote/_prepare` touched the port without defense (stub context crashed) — all fixed. The two architecture_skeleton assertions were rewritten per v3 design: after seal the IR is provider-neutral, reasoning byte fidelity carried by wire envelope/projection native (the v3 form of Nathan's original core concern)

## 7. Rollback

Branch is independent; main / v2 branches untouched (history consultable); not pushed. two_segment is the only mode: rollback = revert this commit (v2 behavior carried by git history, not rebuilt from this branch).

## 8. Live status of the 28-item closure matrix (2026-09-21, against pal_v3_review_2014db4/TEST_MATRIX)

| Item | Status | Evidence |
|---|---|---|
| N01 retry after failure | PASS | test_v3_n1_root_lifecycle (schema failure) |
| N02 cancel collects only its own run | PASS | same (cancel barrier) |
| N03 expired does not install | PASS | same + N1.1 construction-time expiry/equality boundaries |
| N04 same-turn R growth does not kill L | PASS | same (intra-turn) |
| N05 true L modification rejected | PASS | same (body/metadata two negative cases) |
| N06 IN_PROGRESS does not enter L | PASS | same |
| N07 old L reclaimable | PASS | same (weakref) |
| N08 handoff preserves base | PASS | test_v3_n2_projection_fixes (three-shape oracle) |
| N09 native originals replayed | PASS | same (kept native) |
| N10 executor real keeper | PASS | same (populated R + epoch) |
| N11 shared L reference | PASS | test_v3_n2_continuity_lview |
| N12 actual profile encoding | PASS | real wiring on runtime plan side: binding carries validated capabilities, projection/cold path same source (af51d74 F1) |
| N13 endpoint mismatch cold fallback | PASS | test_v3_n3_invoker_projection (mismatch drop) + af51d74 F2 (receipt applied=False) |
| N14 fallback/spec refresh switching | PASS | af51d74 F2: dual-endpoint fallback fixture (projection not frozen) + sync stale-spec refresh preserving projection parameters |
| N15 spans/extra_body preserved | PASS | n2 handoff wire-contract cross-check |
| N16 no prefill of future A | PASS | vertical trace (frozen after accept) + af51d74 F4 (only frozen fallback after boundary replacement) |
| N17 tool result commit failure does not freeze | NOT_RUN | pending tool-delivery acceptance path |
| N18 stream partial/length does not freeze | PARTIAL | ERROR-turn reject tested; stream sub-cases NOT_RUN |
| N19 handoff output goes only to validator | PASS(component) | engine suite (existing I14) |
| N20 long-lived turn compactable | PASS | test_v3_n3_vertical_trace (full promote→compact chain) |
| N21 send budget gate | PASS | G4 B01/B03/Q03 + B04/B05 (faf2f41: B04 hard overflow interception of current view, B05 base_over_budget terminal state); B06-B08 see test_v3_b_family_runtime |
| N22 no warmup/miss no resend | PASS | hot_cache suite all green (cache_epoch/replay_guard semantics restored on v3 path); warm split 15196dd |
| N23 interrupt vs commit race | PASS | N1 cancel + compact_cancel_control |
| N24 rebase failure bans old projection | PASS | stale-left guard test + af51d74 F6 (lineage span not durable cold fallback) |
| N25 worker/owner single writer | PASS | owner-side prepare + typed lowering + **receipt-authorizes-commit** (af51d74 F2: no receipt/applied=False/attempt mismatch all reject freeze) |
| N26 graceful shutdown recovery | PASS(component) | projection_checkpoint suite (including span recovery) |
| N27 host×shape×warm real E2E | PASS(gated) | 3621a74 test_v3_real_provider_e2e (glm/deepseek/openrouter-luna real runs, PAL_V3_E2E gated); luna tail dialect anchor reading evidence is a bounded follow-up |
| N28 final matrix + full run | This table + full logs | After v2 deletion, full run 3086+491+8sk/49f (1790s): 17 true regressions fixed, affected 19 files 299+29sub all green; 10 astra flips pre-existed: after diagnostics-layer fix, 6 remain (observe/promote closed-loop family, §4.1); remaining 22 long-run family sampled single runs all green |

## 9. N8 — review pal_v3_review_95373ef (baseline 95373ef, R1–R6/S1/S2) closeout record (2026-09-22)

Fixed input: `95373efff0fe23d4fc4f6292901e97bbe5250dfe` (reviewer package MANIFEST fixed); fix tree `a1058c5` (implementation) / `a923583` (tests) / this commit (evidence). Throughout, `PYTHONPATH="$PWD/src:$PWD"` and `pal.__file__` points to this worktree (user-site hijack warning §4.1). Not pushed, not merged to main, not deployed, no real provider calls.

### R items, one by one

- **R1 (same ID ≠ same profile) fixed**: `_endpoint_config_digest` recomputed from live endpoints (capabilities / output limits / spec revision all enter the digest); drift → old projection/old plan cold fallback or recompile; runtime injection selection (cache-policy generation counting) moved out of the identity view, harmless refresh no longer destroys lineage. Counterexample M01; normal control second half of M02 (config-unchanged retry reuses, no extra probing)
- **R2 (native eligibility after recovery/replacement) fixed**: true LENGTH→continuation recovery sets receipt.native=None and the frozen artifact carries the merged full text (M03); finalization replacement strips native at the F4 boundary (M04 negative); unchanged originals freeze byte-true (M04 positive). Honesty boundary: the observe layer did not add a "forged receipt text comparison" guard — per HANDOFF "no three independently speculative conditions added"; the production replacement path is already fully closed by two lines of defense (F4 stripping + call_ids reconciliation); hand-forging receipts via the API is API abuse, not a product counterexample
- **R3 (one L model view) fixed**: rebase installs `seed_reference_ids` as an L override, so the next round's prepare no longer re-injects the same standalone seeds (M07 sentinel exactly once); two consecutive compacts share the same raw→standalone mapping (M08)
- **R4 (seam coordinates faithful to actual wire) fixed**: preamble takes only the head system/developer contiguous segment (mid-stream developer stays in the conversation flow in chronological order); tail span remapping uses the real merge result (M11 unmerged → true assistant coordinates / merged → block offset correspondence); continuity_target uses the same remapper as cache/wire (M12). The Completion system seam variant was added by a later slice (test_completion_system_seam_paths_shift_with_the_head, global coordinates + marker does not land on the wrong message, passed first try)
- **R5 (diagnostics enumerate the complete wire) fixed** (negatives added on top of the 95373ef diagnostics layer): M13 nested span coverage changes do not false-alarm / M14 top-level system drift is not obscured / M15 dynamic sub-block does not swallow historical attribution; P01/P02 positive controls kept in test_cache_diagnostics
- **R6 (attempt does not go out of control) fixed**: any exit between prepare→observe (cancel / accepting an exception / owner exception) is cleaned up by the same owner's finally collecting its own draft (M05, two variants); late cleanup owner-match: does not touch successor rounds, does not roll back what has been committed (M06)
- **S1 (legal local prefix can be attempted directly) settled**: `eligible_anchor_request` only checks scope/endpoint/TTL; `read_confirmed` is a diagnostics bit, not a permission bit; confirmed readers unchanged (hot-only cost boundary as-is) (M09 + warm_handoff_split)
- **S2 (performance wrap-up) done**: shell envelope encoding only sees the preamble; M16 proves that with a 40-message shell, a single round encodes ≤2 input messages. No fixed speedup multiplier is promised

### M01–M18 actual results (nodes all in tests/test_v3_review_95373ef_fixes.py unless noted otherwise)

| Item | Status | Evidence |
|---|---|---|
| M01 | PASS | RuntimeAndHistoryContracts::test_same_endpoint_profile_drift_invalidates_old_projection (actual send comparison see M02) |
| M02 | PASS | ::test_sync_stale_refresh_recompiles_with_the_new_profile (true stale→refresh receives cap→single-send recompile→applied=False) |
| M03 | PASS | ::test_old_attempt_native_cannot_replace_a_recovered_accepted_message (true recovery path) |
| M04 | PASS | ::test_replaced_contribution_through_real_boundary_drops_native (negative, through the real F4 boundary) + ::test_unmodified_provider_original_freezes_native_byte_true (positive) |
| M05 | PASS | ::test_cancelled_ordinary_generation_closes_projection_attempt + ::test_failed_l1_acceptance_closes_its_own_draft_and_next_round_prepares |
| M06 | PASS | ::test_late_cleanup_never_touches_successor_rounds_or_committed_history |
| M07 | PASS | ::test_new_summary_occurs_once_after_real_rebase_and_continuity_view |
| M08 | PASS | ::test_consecutive_compacts_share_one_l_identity_mapping |
| M09 | PASS | ::test_unconfirmed_local_anchor_is_eligible_until_ttl_expiry + tests/test_v3_warm_handoff_split.py |
| M10 | PASS | tests/test_v3_m10_final_wire.py three paths side by side (success: marker exactly once + retired left segment does not replay; failure/cancel: history untouched; R original with native tool call/result preserved byte-for-byte; zero tool re-execution across the three paths); success-path materialization uses honest cold encoding (prepare returns None, consistent with the N24 stale-left guard); summary-exactly-once for the continuity view is complemented by M18 |
| M11 | PASS | ::test_no_merge_uses_no_merge_coordinate_transform + ::test_merged_user_boundary_uses_merged_block_offset(full prepare) |
| M12 | PASS | ::test_continuity_target_is_remapped_with_cache_and_wire_paths + ::test_completion_system_seam_paths_shift_with_the_head (Completion system seam full chain: global coordinates + marker does not land on the wrong message; added by a later slice) |
| M13 | PASS | DiagnosticContracts::test_identical_wire_with_nested_span_coverage_change_is_preserved |
| M14 | PASS | ::test_top_level_system_drift_is_not_hidden_by_missing_span |
| M15 | PASS | ::test_dynamic_subspan_does_not_hide_unspanned_history_in_same_item |
| M16 | PASS | ProjectionSpanContracts::test_shell_encode_never_scans_the_whole_history |
| M17 | PASS | tests/test_prompt_cache_v2_runtime.py all green (true v3 entry migration + byte-truth retirement assertions + summary independent accounting); §4.1 remaining 6 outstanding items closed |
| M18 | PASS(composed) | ::test_composed_trace_normal_recovery_install_next (normal→LENGTH recovery→install→next, summary exactly once); re-inserting a same-ID spec refresh within a single loop not done (independently covered by M02), recorded honestly |

### reviewer draft and TLA boundary

- `test_review_95373ef.py` (reviewer's unrun draft) actually run on the fix tree: 6/9 pass; the 3 failures were each characterized as fixture drift (guessing params to call internal helpers directly / rebasing without live lineage / hand-forging receipts); after fixing per "fix the fixture without weakening assertions", all entered a923583 (assertion surface equal to or stronger than the draft)
- This round: zero changes to spec/. The transferred changes (attempt open/close/late cleanup, native eligibility, eligible reader) are not covered by existing TLA models (spec/llm covers endpoint calls / cache anchors / entry commits / L1 rounds, not the projection attempt FSM) — per HANDOFF, product tests serve as evidence; `eligible_anchor_request` is a purely additive read-side change; PromptCache* model semantics (frontier advance requires read evidence, marker does not authorize dffb210) untouched, prior TLC results remain valid for it, and were not duplicated as new-path evidence

### This round's test results

- Red baseline: the reviewer draft's 9 items were 9/9 red on the 95373ef tree (logs_v3_review_95373ef_draft_red_baseline.txt); after fixes, the final version is 19/19 green
- Reviewer draft run as-is (fix tree): 6/9 pass, with the 3 failures providing the fixture-drift characterization evidence above
- Wide scope: projection/prompt_cache/cache/compaction/runtime/continuity/history/v3 families 515 passed + 82 subtests + 1 skip (62s)
- Full regression (a1058c5+a923583 tree, 1356s): **3134 passed + 491 subtests + 8 skipped, 22 failed (19 FAILED + 3 SUBFAILED)** — exactly the same set as the previous round's long-run family (3eb036b attribution set) (the previous round's 3 llm_runtime_ir true regressions did not reproduce); single runs **19/19 + 3 subtests all green (7.6s)**, the fourth same-family verification (3575e48/29a879f/af51d74fix/this round). Mid-run, memory pressure from ollama bge-m3 residency (1.2GB) slowed things down, recovering after model unload; total duration still within the historical range. No claim of a fully green full run; a clean-environment rerun remains the final attribution step (§4.3 outstanding items continue)

## 10. c9cb2d2 targeted re-review closeout (2026-09-22, review pal_v3_review_c9cb2d2)

Fixed input: `refactor/two-segment-session-v3@c9cb2d250271192a06198abd587eb462f9000b5f` (reviewer package MANIFEST fixed), product tail `3a7bbe9`. Fix tree `faee0dd` (implementation + tests), evidence accompanies this commit. Throughout, `PYTHONPATH="$PWD/src:$PWD"` and `pal.__file__` points to this worktree. Not pushed, not merged to main, not deployed, no real provider calls.

The reviewer's three findings (Request changes, C1 P1 / C2 P2 / C3 P2) closed out one by one:

- **C1 complete cold source (fixed, core/compaction.py)**: `build_compaction_units` no longer does head/tail character projection on each message (removed `unit_text_limit` / `_bounded_source_text`); legal content enters the cold source whole, and whether it fits is judged by the engine's real preflight send budget — over-window takes the existing `source_too_large` / `base_over_budget` terminal states and fully preserves the old L, no longer sneaking through via source trimming. Evidence: A01 (10k head + a unique mid constraint + 10k tail, window sufficient → constraint delivered intact, no projection marker), A02 short-source positive control, A03 over-window control (READY-window fixture judged by measured request character count: full source = COMPACT_REQUIRED → empty-source probe READY → `source_too_large`; zero generation calls, old L/R unchanged id by id).
- **C2 final handoff TEXT ONLY (fixed, core/compaction.py)**: the sole generation-result acceptance point rejects TOOL_CALLS finish and tool-call parts before any parsing (even if the text is legal checkpoint JSON); retries follow the existing attempts/absolute deadline; unfinished calls are not moved into repair history (no repair_output). Evidence: B01 (TOOL_CALLS + legal JSON + ToolCallIR → rejected, zero installs, turns unchanged), B02 (same parts but finish=STOP also rejected; plain-text positive control installs), B03 (first illegal → second legal: attempts=2, single install, retry request carries no `compaction_failed_output`/`compaction_repair_request` semantics).
- **C3 one projection bootstrap after recovery/binding (fixed, turn_executor + projection_session)**: `EndpointProjectionSession.has_materialized_content()` distinguishes "fresh/rebound (unmaterialized)" from "materialized stale"; on mismatch and unmaterialized, one cold rebuild is performed from the current canonical L model view + CURRENT R via `on_left_replaced` — sharing the same entry point as post-commit rebase's `_install_left_replacement` (no third recovery path added); generation consumption is recorded only after content and coverage are established together; materialized lineage remains strictly refused (counter untouched). Evidence: C01a (focused save/restore two-round re-entry, summary/history exactly once), C01b (app-level real host save/publish→restore + the app's own executor real PromptCompiler path over two rounds: `RESTORED SEED SUMMARY` and each user message each exactly once on the wire, freeze on the first round, no stale/bootstrap-failed diagnostics), C02 (switching endpoint binding: old native does not cross providers, new projection built from current L/R, successor rounds reuse, send count == round count with zero warm-up), C03 (gen0 positive does not regress; after recovery, bootstrap exactly once and not re-entered on the second round; materialized stale continues to be refused, engaging only after explicit rebase).

Evidence and commands (workspace `~/Documents/coding/Pal-two-segment-v3`):

- Reviewer draft `test_review_c9cb2d2.py` (T01–T05, not run by the reviewer): actually run on the c9cb2d2 tree, 3 failed / 2 passed (reproducing C1/C2/C3) → fix tree **5/5 passed**. Command: `PYTHONPATH="$PWD/src:$PWD" python -m pytest <review package>/test_review_c9cb2d2.py -q`, exit 0.
- New repo test node `tests/test_v3_c9cb2d2_review_fixes.py`: **11/11 passed** (A01–A03 / B01–B03 / C01a / C01b / C02 / C03×2), same command same prefix, exit 0.
- Affected-chain rerun (fix tree): v3 chain 14 files **107 passed + 12 subtests**; compaction family 7 files **60 passed**; projection family 13 files **133 passed + 74 subtests**; total **300 passed + 86 subtests**, all green.
- Full regression (`faee0dd` tree, 1371.83s): **3094 passed + 498 subtests + 7 skipped, 22 failed (19 FAILED + 3 SUBFAILED)** — item-by-item diff of the FAILED set and SUBFAILED set against the idle rerun baseline (`logs_full_regression_idle_rerun.txt`) is empty; no new failures, all are known long-run timing/order-sensitive families (bootstrap-telegram / bunshin_sandbox / bunshin_v2_public / browser_cli / control_plane / bunshin_submission_errors / bunshin_v2_contract_protocol / bunshin_v2_verification). The increment of passed 3094 vs baseline 3071 corresponds to test nodes added in the intervening period (H02/H03, the B family, and the 11 nodes in this section). No claim of fully-green full regression.
- Not run / not claimed: N27 real provider E2E (opt-in `PAL_V3_E2E` gate unset → SKIP, not pretending it was run); iqoo/slot.py patch (exact repo/branch/SHA not obtained, not reviewed by reviewer or this round); TLA no new model actions (this round was a source/acceptance/recovery reuse fix; the existing action semantics of `on_left_replaced` are unchanged), and old logs were not substituted for new-path evidence; SIGKILL / per-compact persistence / warm-up / durable inbox were not introduced (user scope preserved).

## 11. 4b14ce4 re-review close-out (2026-09-22, review pal_v3_review_4b14ce4, B1)

Fixed input: `refactor/two-segment-session-v3@4b14ce4542afd49c29871c207f03c15cbe2d7331` (reviewer package MANIFEST pinned), product tail `faee0dd`. Fix tree `af400d9` (implementation + tests), evidence accompanying this commit. Throughout, `PYTHONPATH="$PWD/src:$PWD"` and `pal.__file__` pointed to this worktree. Not pushed, not merged to main, not deployed, no real provider calls (B05 all shapes went through prepare-only, zero network).

Reviewer verdict: C1/C2 closed this round's findings; C3 judged PARTIAL with one remaining blocker B1 (P1). B1 has now been closed:

- **B1 symptom (reproduction)**: `_install_left_replacement`, on fresh/rebind bootstrap, encoded the **entire current L model view** into the prefix, but that entry point was originally designed only for "just compacted, L has only the summary seed" — coverage registered only the standalone-continuity single id. On recovery/rebind the chunks are empty, so ordinary Q/A already promoted in L are not in coverage and get re-encoded once more by the normal tail: summary once, ordinary history twice. Reviewer drafts T01/T02 ran 3 red on the 4b14ce4 tree (Q1/A1 and POST_COMPACT_USER/ANSWER each appearing twice).
- **Fix (af400d9)**: materialization and coverage established from **the same encode** —
  - `LeftReplacement.seed_reference_ids` → `seed_coverage_ids`: the ids of **every** model-view message the seed encode actually received (post-compact rebase still equals the standalone single entry; recovery/rebind bootstrap is the entire L view). Still derived coverage; no fabricated provider round chunks, no new authoritative storage.
  - `EndpointProjectionSession._l_reference_ids` → `_l_coverage_ids`: `on_left_replaced` replaces wholesale, `bind`/`retire` clear it, `covered_message_ids()` read semantics unchanged.
  - `TurnExecutor._left_view_seed` computes the covered set from the model view it hands to encode (standalone replacement coordinates preserved; canonical ids enter coverage without string dedup); the three seed-less fallback branches honestly declare the ids they actually encode.
  - Materialized stale projections continue to be strictly rejected (C3 negative cases untouched); `_left_revision` does not admit passes by counter manipulation.
- **Invariant check**: B-I1 materialization and declaration share the same source (same encode); B-I2 normal requests encode L/R once each, order = current legal model view (B04/B05/B06 counting assertions); B-I3 recovery/rebind only rebuilds derived representations (zero new provider calls, B02/B06 sends == rounds + compact); B-I4 fresh can initialize, materialized stale still rejected, no counter-based pass (C03's two tests + B06 retirement assertion).

Evidence and commands (workspace `~/Documents/coding/Pal-two-segment-v3`, fix tree `af400d9`):

- Reviewer draft `test_review_4b14ce4.py` (reviewer did not run it): 4b14ce4 tree **6 failed / 8 passed** (T01 requests 2/3 each twice, T02 both requests POST_COMPACT twice) → fix tree **8 passed + 12 subtests** (exit 0).
- Red baseline (this repo's nodes, final test set): after temporarily `git checkout HEAD~1 --` of the two source files back to the 4b14ce4 state, `tests/test_v3_4b14ce4_review_fixes.py` plus the hardened C02 together had **17 failed** (B02 4 subfail / B04 4 / B05 3 shapes / B06 4 / C02 2), alongside 15 passed + 18 subtests; after `git checkout HEAD --` restoration, **15 passed + 35 subtests all green**, working tree verified intact.
- New `tests/test_v3_4b14ce4_review_fixes.py` (matrix B02/B04/B05/B06, 4 nodes):
  - B02: real app checkpoint save/restore (after compact, an ordinary Q/A is completed and promoted again) + the app's own executor with a real PromptCompiler over two rounds; summary / POST_COMPACT Q/A / first ask after recovery each appear exactly once in both payloads; projection wired in (left_revision=1, chunks=2).
  - B04: seed + two groups of already-promoted ordinary history, the second group belonging to the same logical turn T as the current R; after recovery, one round's payload has each of six sentinels exactly once; the cut is legal (after promote L=[seed,q1,A1,q2,A2], R empty; after recovery L identical id-by-id).
  - B05: L contains a complete aggregate tool call + result (`append_l1_tool_result`), R contains new work; all **three supported shapes** each go through a real `_prepare_turn_projection` (real LLMRuntime plan/codec, real bootstrap), **prepare only, no sending**: result sentinel exactly once, call id exactly twice (declaration + result link), order call<result, each shape using its own codec (tool_calls/tool_call_id, function_call, tool_use/tool_result); tail is exactly the current single R entry. No claim about whether tools would or would not re-execute — this node performs no sending at all.
  - B06: bootstrap → ordinary run → second real compact (real engine): bootstrap round has each of six sentinels once; after the second compact, the old L coverage retires with the old L (covered set no longer contains b06-q1/q2/q3), the new summary and the new post-compact R each appear once, retired content 0 times; sends == 3 (two rounds + compact), zero warm-up.
- B01 = the hardened `test_C02` (same-round c9cb2d2 file): in the second and third payloads, "REBIND SEED SUMMARY"/"First round."/"REBOUND_1" each appear exactly once.
- B03 = existing controls not weakened: C01b (seed-only app positive case), C03's two nodes (gen0 positive case does not regress + bootstrap exactly once + materialized stale still rejected) remain all green.
- Affected-chain rerun: v3 chain 15 files **111 passed + 47 subtests**; compaction family 7 files **60 passed**; projection family 13 files **133 passed + 74 subtests**; all green.
- Full regression (`af400d9` tree, 1377.37s): **3098 passed + 533 subtests + 7 skipped, 22 failed (19 FAILED + 3 SUBFAILED)** — the FAILED set and SUBFAILED set diff item-by-item as empty against `logs_full_regression_idle_rerun.txt` (known long-run families), no new failures; the passed increment 3098-3094=4 is this round's 4 new nodes, and the subtests increment 533-498=35 is 33 sub from the 4 nodes + 2 sub from C02 hardening. No claim of fully-green full regression.
- Not run / not claimed: N27 real provider E2E (opt-in `PAL_V3_E2E` gate unset → SKIP); iqoo/slot.py patch (exact repo/branch/SHA still not obtained, not reviewed); TLA no new model actions (materialization/coverage representation invariants, no new ownership state transitions introduced; the existing action semantics of `on_left_replaced` are unchanged), and old logs not substituted for new-path evidence; SIGKILL / per-compact persistence / cache warm-up / durable inbox not introduced.

## 12. 7d182fd targeted re-review close-out (2026-09-23, review pal_v3_review_7d182fd, S1 cross-cut user seam)

Fixed input: `refactor/two-segment-session-v3@7d182fdb32a5aa080d4dc40ff639787b487f5938` (reviewer package MANIFEST pinned, product tail `af400d9`). Throughout, `PYTHONPATH="$PWD/src:$PWD"`, `pal.__file__` pointing to this worktree. Not pushed, not merged to main, not deployed, no real provider calls (fake network frames are only in the product decoder's format).

Reviewer verdict: original B1 closed; new S1·P1 opened — when the Anthropic user seam merges, old L bytes are re-attributed to R, and the next left replacement cannot retire them.

- **S1 symptom (reproduced by actual run)**:
  - Component level: reviewer draft `test_review_7d182fd.py` (reviewer only ran py_compile) run for real on the 7d182fd tree gives **2 failed / 2 passed + 3 subtests** — T01: old L text `RETIRED_LEFT_INSTRUCTION` revives inside a mixed user item borrowed from a surviving R chunk; T02: the old `tool_result` becomes an orphan and `prepare` is directly rejected by tool-protocol validation (`ProjectionContractError`, harder than the "retire to cold" the review described); T03's three-shape comparison and T04's interrupted-turn legality comparison are green.
  - Product level (new vertical this round): bootstrap (L ends with an interrupted user-only turn) → accept R's answer → compact only the left side (R never promotes; `_rebase_projection_after_left_install` via the same post-commit interface) → in the next round's anthropic wire, old L text revives once (`Old interrupted ask` in the round2 payload count=1). **The product-path tool variant cannot occur**: `_ensure_assistant_closure` (turn_ir) guarantees that an L1 turn ending in TOOL always gets an assistant bridge added, so the L view never ends with a tool_result — the tool orphan variant is covered at component level by T02, and at product level by the text variant; both share the same mechanism (an empty span being swallowed by R's span at seam merge).
- **Fix (block-level attribution + seed L-owned marking, in the same batch)**:
  1. **per-BLOCK ownership throughout**: `ProjectionChunk.item_block_spans` / `_PendingWireItem.block_spans` / `_ActiveRound.prepared_item_block_spans` / `_prefix_item_block_spans` are four axes running parallel to the existing per-item span. `prepare` assembles the conversation triple; `_merge_anthropic_user_boundary_pairs` concatenates block-level ownership by content order (merge is `[*left.content, *right.content]`, so block order equals source order); `observe_commit` materializes accepted block-level ownership into this commit's span, and tail trims carry away block-level ownership; checkpoint serialization/restore stays in sync (missing field → () = legacy whole-item rule, lenient downgrade never rejects a snapshot).
  2. **seed ownership fix**: the seed items span in `on_left_replaced` changed from unconditional `()` to `seed_coverage_ids` (B1 same-source coordinates). The empty-span semantic remains only for legacy (conservatively kept); seed bytes with coverage retire on the **next** left replacement — fixing the shared root cause of both the pending pass-through path (a bootstrap tail user no longer survives across replace) and the seam path. Empty coverage (legacy callers) keeps the old conservative behavior.
  3. **block-level retirement rebuild**: new `_retire_wire_item(item, span, blocks, kept)` — with aligned block-level ownership it judges block by block (empty span / ⊆ kept stays, rest deleted), and on partial retirement it **rebuilds the item** keeping only surviving blocks, with the new item span = deduplicated union of surviving block spans; without a block-level axis it keeps the F5 whole-item rule. Chunk filtering in `on_left_replaced` and pending filtering both go through it uniformly. It does not re-encode the frozen prefix, does not fabricate thinking/signature, and adds no new coordinator/terminal-state/ticket (all PLAN boundaries respected).
- **Invariant check**: S-I1 seed materialization has traceable ownership (coverage is the span; empty span is no longer permanently unowned); S-I2 merge does not change source ownership (block-level concatenation, each side preserved); S-I3 after replacement the retired L original disappears, R text/tool results each kept exactly once (T01/T02 and vertical round2/3 count assertions); S-I4 wire across a cut does not split a semantic tool group (after the T02 fix, tool_use retires whole-item with the pure-L item and tool_result retires block-level, no orphans in surviving requests — tool protocol validation passes); S-I5 the coverage set still corresponds to current materialization (`_l_coverage_ids` mechanism untouched, B1 test family all green with no duplicate appends).
- **Not claimed**: component tests are not equated with host E2E; no real provider was run; the 22 pre-existing full-regression failures are not required fixes for this round (only that the failure set gains no new entries); TLA added no model since no new hold/transfer state machine was introduced; a full rebased prefix roundtrip via the checkpoint's standalone codec is still not claimed (new fields are best-effort incremental, defaulting to the legacy whole-item rule when absent).

Evidence and commands (workspace `~/Documents/coding/Pal-two-segment-v3`):

- reviewer draft: 7d182fd tree **2 failed / 2 passed + 3 subtests** (T01/T02 red) → fixed tree **4 passed + 3 subtests** all green (exit 0).
- New `tests/test_v3_7d182fd_review_fixes.py` (matrix S02 vertical + S03 openai comparison, B05 extension): before the fix the anthropic vertical was red with `Old interrupted ask` resurrecting (SUBFAILED round=2) → after the fix **2 passed + 22 subtests** all green. Node content: B05's complete LEFT tool group (interrupted wrap-up, no bridge) + an interrupted user-only turn folded into the same tail user item (S01 text + S02 tool in the same scenario); after accepting the R answer only the left side is compacted (R is never promoted); asserts round2/round3: old call/result/old summary/old interrupted text 0 times each, new summary/R Q/A/next question 1 time each, no stale/prepare_failed/rebase_failed/bootstrap_failed diagnostics (no reliance on cold fallback), round3 continues warm (chunks>0, left_revision=2); the openai_completion comparison shows the same retirement/retention (no seam and no regression).
- Affected-chain rerun: reviewer draft + vertical + checkpoint family + B family + C family (5 files) **28 passed + 62 subtests**; projection/native/v3 focused family (21 files) **206 passed + 79 subtests**; all green.
- Full regression (fixed tree, 1359.85s): **3100 passed + 557 subtests passed + 7 skipped, 22 failed (19 FAILED + 3 SUBFAILED)** — a line-by-line diff of the FAILED/SUBFAILED set against baseline `logs_full_regression_4b14ce4_review_fixes.txt` is empty, no new failures; the passed increment 3100-3098=2 and subtests increment 557-533=24 both come from this round's new test nodes (S02 vertical + S03 comparison, 2 tests + 22 subtests); full-suite all-green is not claimed. Log `logs_full_regression_7d182fd_s1_seam_fix.txt`.



## 13. 58eaadf re-review fixes: guidance visibility and frozen breakpoint mapping (2026-09-23)

Baseline `58eaadf`; work in this section is not yet committed, merged, or activated. No real provider requests.

- **Stale guidance exits the projection**: the original check only verified that the frozen id was still in L1, so turn-scoped guidance already filtered by the normal view was still replayed. It now compares against visible context ids of the effective request; when covered guidance has expired, a rebuild happens once using the existing rebind boundary. The original L1 is not rewritten, and repeated prepares on the same visible view do not repeat generation swaps. Left-segment bootstrap on restore/rebind likewise filters stale and excluded context while preserving the existing continuity replacement of the compact source.
- **Frozen position indices preserved with bytes**: cache targets, wire paths, and the continuity target produced by the codec are saved in item-relative coordinates; freeze, pending, seam merging, per-block retirement, and checkpoint all carry the indices. Relocation happens at final assembly, then the prefix description is computed from the actual payload. Frozen history is not re-encoded and breakpoint positions are not guessed; native accepted output maps along the codec's existing target rules. No ACK/controller/benefit-policy changes.
- Regression `tests/test_projection_visibility_cache.py`: the two original counterexamples failed on the baseline; new coverage includes three-shape address retention, left replacement after restore, Anthropic pending merge/partial retirement, explicit three-round T/P/C, bootstrap stale guidance, hoisted system address restore, and more.
- Verification (all with `PYTHONPATH="$PWD/src:$PWD"`): projection/v3 final suite **257 passed + 145 subtests, 1 skipped**, log `/tmp/pal-v3-projection-final-tests.txt`; compaction/history owner/checkpoint related **95 passed + 24 subtests**, log `/tmp/pal-v3-visibility-final-tests.txt`. An earlier joint projection/v3/cache/prompt_cache regression **358 passed + 145 subtests, 1 skipped**, log `/tmp/pal-v3-visibility-cache-tests.txt`. These sets overlap and are not summed as totals.
- This round did not run full-repo, macOS, real provider, or new TLC; the 22 pre-existing full-suite failures are not claimed as resolved. The fix reuses the existing rebind/commit/rebase lifecycle; position indices are derived data.

## 14. Lossless L1 and post-admission round freezing (2026-09-23)

This section supersedes earlier claims that round/turn closure retires reasoning.
Changes remain on `refactor/two-segment-session-v3`; no runtime activation,
commit, push, or paid provider request was performed.

- L1 retains accepted reasoning, native replay, opaque continuation fields,
  exact argument strings and full tool results. Codec capture reaches the
  accepted L1 message even when a provider hook clears its temporary replay.
  Textual DeepSeek tool adaptation keeps both the legal executable inventory
  and original source evidence; abort/recovery repairs only revoked calls.
- After preflight/conditional compact, the owner captures a closed R prefix.
  SDK response admission (headers, before nonstream body parsing) or Bunshin's
  provider-start event delivers an idempotent submission receipt on the owner
  loop. Only the captured prefix moves to L. Send failure, reset, changed
  input and duplicate receipts cannot promote new work. Later output stays R;
  generation failure after admission does not undo the already submitted input.
- Automatic compact replaces existing L only. Manual compact may archive a
  settled closed tail, but cannot absorb active unsent work. Checkpoint schema
  4 preserves replay and the cut; schemas 1/2/3 remain readable. Legacy snapshots
  without an owner cut initialize L from closed imported turns.
- Model-selection UI/tool affordances recommend successful compact before
  switching. Incompatible opaque continuation fails explicitly instead of
  silently deleting reasoning. Cache modes and eager chase-tail are unchanged.
- A legal L/R cut may split a projection round between sent input and newly
  accepted output. Per-message item/block ownership now permits that split,
  retires only the covered wire content and its cache addresses, and keeps
  surviving native output. Ambiguous legacy ownership still fails explicitly.
- Projection remains disposable: restart/rebind/bootstrap rebuild from L1.
  The 100-tool-round regression verifies unchanged serialized message values
  through settlement, restore and a cold projection rebuild; all three codec
  shapes also retain their settlement/recovery replay tests.
- The LLM TLC script checks six models, including new `RoundSubmission`.
  All six passed locally. The new bounded model explored 1,277 generated /
  420 distinct states; three deliberately broken configurations found the
  expected counterexamples (early freeze, R loss during compact, reasoning
  deletion). These are local lifecycle proofs, not claims about provider cache
  hits or all possible SDK/provider behavior.

Validation used `PYTHONPATH="$PWD/src:$PWD"` on Linux / Python 3.13. Source
compile and whitespace checks passed. No macOS or real-provider test was run.

Final validation evidence (counts are separate runs, not an additive total):

- `core-a`: **1166 passed, 6 skipped, 121 subtests passed**;
  `/tmp/pal-lossless-core-a-verified.txt`.
- `bunshin-a`: **464 passed, 77 subtests passed**;
  `/tmp/pal-lossless-bunshin-a-verified.txt`.
- `bunshin-b`: **478 passed, 124 subtests passed**;
  `/tmp/pal-lossless-bunshin-b-verified.txt`. The earlier sandbox timeout also
  passed standalone on both the original baseline and modified tree.
- Core B was split after the slow bootstrap/repository tests: the initial run
  passed **205 tests** before manual interruption, covering the first two files
  and part of the remaining selection (`/tmp/pal-lossless-core-b.txt`).
  The remaining 78 files were rerun separately against the final implementation:
  **859 passed, 1 skipped, 240 subtests passed**
  (`/tmp/pal-lossless-core-b-verified.txt`).
- Final projection/compact regression: **151 passed, 74 subtests passed**
  (`/tmp/pal-lossless-projection-last.txt`); gate/cancel/artifact regression:
  **26 passed** (`/tmp/pal-lossless-gates-final.txt`).
- Native output surviving compact + restart: **1 passed**;
  precise vs ambiguous partial-chunk boundary checks: **2 passed**.
  The 100-round test is also included in the final Core B remainder.
- TLC evidence: `/tmp/pal-lossless-llm-tlc.txt`; deliberate mutation logs:
  `/tmp/pal-round-early.log`, `/tmp/pal-round-erase_right.log`,
  `/tmp/pal-round-strip_reasoning.log`.

## 15. Main-to-v3 final review (2026-09-23)

Reviewed the accumulated branch against local `main` (`4daab5f`), including
projection ownership, lossless L1 replay, request admission, left-only compact,
checkpoint migration, cancellation and the resident/Bunshin transport paths.
The final follow-up fixes are:

- Chat stream null placeholders no longer overwrite accumulated text,
  reasoning, call IDs, function names or argument strings.
- Chat `reasoning_details` retains every received chunk in order instead of
  overwriting earlier chunks. This follows the streaming sequence described in
  [OpenRouter's reasoning documentation](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
  The `reasoning` alias also participates in the incompatible-model guard.
- Memory checkpoint schema 1 (the current main format) can enter the existing
  legacy migration path. A coordinator-level regression checks that settled
  imported history becomes L while active input remains R.
- Scheduled channel admission again releases its ingress reservation in a
  `finally` block. Preparation failure and cancellation cannot permanently
  prevent memory maintenance.

New null-delta, reasoning-chunk and schema-1 regressions reproduced their
failures before the fixes. The ingress regression likewise reproduced the
leaked reservation on both failure and cancellation.

Offline verification on Linux / Python 3.13, with worktree `PYTHONPATH`:

- Projection/v3/history/continuation suite: **340 passed, 1 skipped,
  162 subtests passed** (`/tmp/pal-v3-final-review.txt`). This run precedes
  the follow-up fixes above; the affected paths were retested below.
- Final lossless/codec/recovery/checkpoint/recall suite: **94 passed,
  61 subtests passed** (`/tmp/pal-v3-review-fixes.txt`), including the
  100-tool-round replay through settlement, restore and cold rebuild.
- Compact/cancel/artifact/recovery/Bunshin transport suite: **66 passed,
  2 subtests passed** (`/tmp/pal-v3-review-compaction.txt`).
- Endpoint/runtime/hooks/FD ownership: **82 passed, 11 subtests passed**
  (`/tmp/pal-v3-review-transport.txt`).
- Final ingress/compact-gate/cancellation suite: **31 passed,
  2 subtests passed** (`/tmp/pal-v3-review-ingress.txt`).
- All six LLM TLC models passed (`/tmp/pal-v3-review-tlc.txt`).
- `git diff --check` passed. Earlier full CI-batch evidence is in section 14;
  the above overlapping suites are not an additive test total.

No remaining blocker was found within the reviewed paths. This establishes
local preservation and lifecycle behavior, not upstream cache-hit guarantees.
No paid provider calls, macOS checks, merge to main, push or runtime activation
were performed during this review.

## 16. External 6b0e914 review: partial-retirement ownership (2026-09-23)

D1 is confirmed on baseline `6b0e914ac11d4fbcde46a88ef620d02931c6b4ee`.
The review package's real component tests reproduced **3 failed, 1 passed,
3 subtests passed** (`/tmp/pal-review-6b0e914-before.txt`): retained text was
correct, but item-level ownership contained owner tuples instead of string IDs.
The error also reached installed chunks and the projection snapshot exporter.

The fix flattens the surviving block owners into an ordered, deduplicated
message-ID union. Block-level ownership remains nested; retirement content,
legacy unknown-owner behavior and checkpoint validation are unchanged.
`tests/test_projection_retirement_ownership.py` incorporates the four review
regressions and adds a partially retired item containing an unowned block.

Validation uses this worktree's `src/pal/__init__.py` via
`PYTHONPATH="$PWD/src:$PWD"`:

- Ownership + S1 + visibility/cache + B1 + partial-chunk/native + checkpoint
  + lossless regressions: **64 passed, 71 subtests passed, 1 deselected**
  (`/tmp/pal-review-6b0e914-after.txt`). The 100-tool-round test was deliberately
  deselected; the run preceded the additional unowned-block subtest.
- Final in-repo ownership regressions and the unchanged external test file:
  **8 passed, 7 subtests passed**
  (`/tmp/pal-review-6b0e914-ownership.txt`). These overlap the preceding suite.
- `git diff --check` passed.

Results refer to the uncommitted D1 patch on the baseline above, not a new
product SHA. No full-suite/TLC rerun, full rebased projection restore claim,
paid provider request, commit, push, merge or runtime activation in this step.

Follow-up inspection of ownership producers/consumers and repeated-rebase
boundaries: **155 passed, 134 subtests passed** across `test_projection*.py`
and the 4b14ce4/7d182fd review regressions
(`/tmp/pal-v3-ownership-followup-review.txt`). No additional ownership defect
was confirmed in those paths.

The previously excluded standalone projection-checkpoint limitation remains:
exporting a rebased session and restoring that derived snapshot can raise
`ProjectionCheckpointError: chunk chain is not contiguous`. Reproduced using
the real `_merged_session` review fixture, `on_left_replaced`,
`snapshot_projection` and `restore_projection`. Surviving chunks retain their
original cursor chain while rebase advances the frontier. Repository source
search finds these snapshot/restore APIs defined but not called by production
runtime code; resident recovery restores canonical L1 and rebuilds projection.
This is an existing component API limitation, not evidence that ordinary Pal
restart loses history. No checkpoint redesign is included in D1.

## 17. Remove unused standalone projection persistence (2026-09-23)

Following explicit user approval, removed `llm/projection_checkpoint.py` and
its exclusive cache-span dump/load helpers. Source reference audit found no
production caller of the standalone export/restore APIs. Canonical memory
checkpoint schema 4, L1 replay persistence, resident recovery and runtime
projection/rebase remain in place. The independent checkpoint limitation in
section 16 is now obsolete because that unused API no longer exists.

Removed tests specific to its serialized format/restore protocol. Mixed tests
retain their runtime continuation, native cancellation, receipt idempotency,
wire equality and cache-address assertions; they no longer claim checkpoint
restoration. The invoker native-capture integration test moved to
`tests/test_invoker_native_capture.py`. Failure atomicity uses a test-only
in-memory state comparison, not a replacement persistence mechanism. D1 tests
now verify installed chunk/prefix/pending owner shapes instead of the removed
exporter. Historical v2 delivery notes are marked as superseded on this point.

Validation on the uncommitted worktree based on `6b0e914`, with worktree
`PYTHONPATH` (overlapping selections, not additive):

- Final projection + invoker tests: **126 passed, 66 subtests passed**;
  `/tmp/pal-remove-projection-checkpoint-final.txt`.
- Lossless 100-round/L1 recovery, graceful shutdown, runtime snapshot,
  compaction recovery and adjacent v3 review regressions: **43 passed,
  63 subtests passed**; `/tmp/pal-projection-l1-recovery.txt`.
- Repository-wide collection: **3145 tests collected**, no import errors;
  `/tmp/pal-projection-removal-collection.txt`. This is collection, not a
  full-suite execution.
- No remaining deleted API references in `src` or `tests`;
  `git diff --check` passed.

No commit, push, merge, runtime activation, paid provider request or new
TLA model in this step. Existing D1 fix remains part of the working tree.
