# LLM Projection Refactor v2 — Delivery Report

> Historical record, archived on 2026-10-05 from `docs/llm_projection_refactor/DELIVERY.md`.
> Statements about implementation, deployment and test results describe the
> original work, not the current checkout or running host. See the
> [current documentation index](../../README.md).

> 2026-09-23 update: the implementation and verification of the standalone projection checkpoint described below is historical record.
> That interface, which was never wired into the runtime, has now been removed; recovery currently treats the canonical L1 as the single source of truth,
> and rebuilds the in-process projection. See the follow-up records in the [two-session delivery log](../session_compaction/DELIVERY.md).

**Branch: `refactor/llm-projection-session-v2` (worktree `~/Documents/coding/Pal-projection-v2`).**
**Baseline: `7e0b1f74`. Safety branch: `safety/llm-projection-before-v2-7e0b1f7` (local).**
**Date: 2026-09-18. Executor: Pal. main / online Pal / production config untouched throughout.**

> **Delivery status correction (after external review on 2026-09-18):** the delivery status of this branch is
> **"new-component bypass prototype + native capture wiring", not "new implementation fully complete, only P7 deployment remaining".**
> The request hot path (`endpoint.py::_iterate()`) still uses per-message ReplayEnvelope
> encoding; EndpointProjectionSession / joint checkpoint / Bunshin owner have not entered the
> actual request and recovery chains. What was completed on the branch for P4 (resident integration), P5 (Bunshin/Manager
> proxy integration), and P6 (legacy hot path cleanup) of the original PLAN is **component-level and isolation testing**,
> not integration delivery; the original phase naming in this report shares names with but differs in meaning from the PLAN, which invited misreading, hence this correction.
> The integration portion remains to be done; until it is complete this must not be accepted as a switchable implementation. See "External review correction record" for details.

## Branch Commits

| commit | content |
|---|---|
| 706b1df | P0 baseline: environment, three pre-check confirmations, per-file suite baselines, continuation contract matrix |
| d55799d | P1 formalization: first real SANY/TLC runs; fixed three defects in the solution package model |
| 90b48ad | P1 typing: projection_contracts (construction-time invariants) |
| 978f95d | P2: structured continuation contract + pre-hook native capture |
| c0cbeca | P3: session-level projection owner (receipt-driven chunk freezing) |
| fa6d123 | P4: checkpoint atomic encode/decode + invoker capture wiring |
| 3ef4c9f | P5: multi-role isolation and worker-restart continuation (receipt ledger across restarts) |
| (P6) | Wrap-up: file_read dead code cleanup, prefix amortized cache, microbenchmarks, this report |

## Implemented (code on the branch)

1. **Type layer** `llm/projection_contracts.py`: full-chain construction-time validation of identity/binding/cursor/receipt/round/
   PreparedRequest; §4.2 forbids non-representable combinations.
2. **Continuation contract** `llm/continuation_policy.py`: structured validation of three shapes (signature/
   encrypted_content missing → Degraded; unknown block/entry → explicit issue; bad payload →
   Unsupported). Does not guess required fields from vendor names.
3. **Native capture** `llm/native_capture.py`: a pass-through capture layer between codec and response hook;
   the loss point where the DeepSeek hook strips the envelope has been pinned by tests and bypassed.
4. **Session owner** `llm/projection_session.py`: single writer, receipt-driven frontier,
   chunk freezing (including anthropic trailing-user trimming — source-verified merge behavior means a
   user ending is not a prefix-stable boundary), idempotent/conflicting/late receipts, switching destroys the old lineage,
   and missing required-native yields an explicit ContinuationUnavailable.
5. **checkpoint** `llm/projection_checkpoint.py`:scope/binding/generation/
   frontier/native/receipt ledger in the same snapshot; rejects stitching across generations; legacy snapshots go through the new generation.
6. **Invoker wiring** `endpoint.py`: decode goes through NativeCapture (semantic stream unchanged),
   `LLMAttemptResult.native_payload_json` carries the capture output (new optional field).

## Verified by Running (commands actually executed and their results)

- **TLC**: single 8340/3818, isolation 92017/26244 all passed (distinct state counts exactly match the in-package
  Python model); the mutant failed with `DraftAligned is violated` (by design).
  Logs in `tlc/`, jar sha256 `936a2620…`, Java 21.0.10. The three model fixes (nested quantifiers ×2,
  absent-field discrimination) have source-level comments.
- **Targeted tests** (blast radius of each change): P1 types 17, P2 capture 9, P3 session 10,
  P4 checkpoint/wiring 7, P5 isolation 5, all green; full llm family regression 190 passed + 60 subtests
  + 6 skips (real-integration skipped by design, no paid calls).
- **Microbenchmark** (local Pi, OPENAI_COMPLETION, 800 messages / 400 rounds): build 3.82s;
  steady-state incremental prepare 17.4ms (tail of 1); full encode 14,655ms; **840x**;
  one-time full rebuild after the switch 14,775ms. The residual O(n) is payload serialization (json.dumps +
  thaw_json), within the envelope declared in PLAN §5.4 ("no promise of end-to-end O(tail)").
- **Full suite (P6 gate)**: run file-by-file in isolation; results in the table below.

### Full regression (branch @ P6, file-by-file isolation)

| Item | Baseline (P0) | Branch (P6) |
|---|---|---|
| Files | 141 | 146 (+5 new test files) |
| rc=0 | 139 | 144 |
| Passing tests | 2557 | **2605** (+48) |
| Non-zero | 2 (known environment items) | Same 2 items, exactly identical |

The non-zero items match the baseline: `test_package_installation` 1 failed (the
setuptools/_vendor environment failure reproduced on main checkout) and `test_tool_schema_properties` module-level skip
(missing hypothesis_jsonschema). **Zero regressions, all new tests pass.**

## Static reasoning (type/construction guarantees backed by tests; no end-to-end run verification performed)

- resident and Bunshin/worker role sessions share the same implementation; isolation is guaranteed by per-instance state +
  scope identity (covered by S20/S13/S21/S22 session-layer tests; Manager proxy full chain not wired).
- Rejection paths for power-loss/crash half-writes (frontier ahead of L1, truncated native, wrong schema) are covered by
  construction and checkpoint tests; real process-level crash injection was not done.

## External review correction record (2026-09-18)

`~/Documents/coding/pal_branch_review_2026-09-18` (REVIEW.md + 13 product-API-level counterexample
drafts) verified all eight issues as real; all fixed on the branch:

| # | Issue | Fix |
|---|---|---|
| R2 | PreparedRequest kept only items, losing system/tools/policy | prepare retains all codec shell fields (new `request_shell` parameter; Anthropic system regression test pins it); with zero tail, the shell is reused without invoking the codec |
| R3 | chunk sealed the prepare inputs rather than the accepted outputs | observe_commit adds materialization: native payload extracted as wire items by shape, or `accepted_messages` IR encoded via codec; repair's calls/results materialized likewise |
| R4 | requires_native/attach/native_committed not closed-loop | commit verifies native material existence and call inventory consistency; missing requires_native rejected; boolean self-claims no longer take effect |
| R5 | after restore, frontier non-zero but prefix empty, old history lost | snapshot persists the chunk chain, restore rebuilds the prefix and verifies linkage to the frontier; "non-empty frontier without chunks" rejected |
| R6 | consistency gate only checked "not ahead"; fence could be rewritten to 0 | epoch must be equal (rejects splicing across compacts), digest always verified at same position; fence saved/restored verbatim; committed/chunk chain verification |
| R7 | construction guarantees did not block illegal paths | attach_native verifies open round + binding (shape/endpoint/model); chunk public snapshot deep-frozen (mutation raises TypeError); PreparedRequest.build rejects dangling tool calls; owner fence monotonic |
| R8 | empty inventory comparison short-circuited and skipped | the three shape validators compare inventory unconditionally in both directions |

Counterexample tests merged into `tests/test_projection_review_adversarial.py` (13 items + 3 subtests,
assertion strength not weakened, only output shape adapted to the codec's actual structure).

**The performance number (840x) is downgraded to a synthetic-scenario observation**: until full-request equivalence, recovery, and native
correctness are achieved, it is not an acceptance conclusion; the cumulative prefix overhead of the old finalize_cache_spans
is an old cost measurable independently; the benchmark must be re-run after integration.

## Second-round external review corrections (2026-09-19, targeting ae04397)

All five accusations in `pal_branch_review_2026-09-18/pal_projection_review_ae04397` were verified true; fixed:

| # | Issue | Fix |
|---|---|---|
| F1 | system survived the first round but lost in incremental rounds; developer position projection misjudged under independent tail encoding; zero tail ignored the new shell; after restore, zero tail unusable | shell/tail fully separated: request_shell.messages = preamble, shell encoded verbatim on every prepare (system stable across rounds, per-request budget effective, usable with zero tail and after restore); tail encoded independently with an explicit boundary flag ShapeContext.has_conversation_prefix (Completion codec position determination switched to it); empty-array fallback ("Continue.") excluded using the codec's own spans |
| F2 | anthropic tool result disappeared after trim but the frontier had already passed it; next round's pending-call validation blocked continuation outright | semantic acceptance and wire freezing separated: the trimmed tail enters the session-held _pending_wire_tail, injected automatically on every prepare until frozen later; persisted across checkpoints; pending/tail boundary spliced per the codec's merge rule (user-user); callers no longer manually re-feed committed results |
| F3 | REQUIRED-native repair double-materialized in the same call; OPTIONAL branch set native_committed=True without attaching | single representation for the same assistant contribution: when native materialization exists, IR calls are not rebuilt (only results go through IR); both REQUIRED/OPTIONAL attach material from ClosedRound; observe_commit rejects the native+assistant-IR dual representation |
| F4 | set comparison lost multiplicity/order: orphan results, duplicate calls within a round, results preceding calls all allowed; direct construction of PreparedRequest bypassed validation | linear order-sensitive pairing state machine (call opens a group, result consumes it, uniqueness within a group, no leftovers); __post_init__ follows the same rules; constructor/deserialization cannot bypass |
| F5 | restore shallow-copied, sharing nested objects with the caller's snapshot, allowing later tampering to pollute the prefix | restore performs a one-time JSON deep copy at the boundary (ownership transfer), not copying per prepare |

New tests: review's 10 counterexamples (tests/test_projection_followup_review.py) + an offline vertical path
(tests/test_projection_vertical_path.py: shell→native answer→REQUIRED-native repair→
checkpoint→restore by a new owner→full request, with content/order/count assertions against the full-history reference).
Merged regression: llm family 760 passed (2 failures are pre-existing interference from bunshin same-process mixed runs; each passes when run alone,
unrelated to this branch).

### Known limitations (honest disclosure)

anthropic's adjacent same-role merge semantics has one unclosed equivalence difference against the frozen boundary: when an already-frozen
assistant item is immediately followed by a natively accepted assistant turn in the same round (a text answer directly followed by a tool
call), the full encoding merges them into one message while the incremental path keeps two adjacent messages. **Content blocks,
order, and counts are fully equivalent**; only the wire item partitioning differs (the Anthropic API accepts consecutive same-role
messages). A patch that materialized boundary merging was attempted and rolled back because it would rewrite the frozen region — per the review
principle of "establish the contract before patching," it must be resolved by redesigning stable frozen boundaries (including complete tool groups),
not with a local patch. This difference has been pinned by tests (vertical test comments).

## Third external review fixes (2026-09-19, targeting 4afee09)

All four accusations in `pal_projection_review_4afee09` (G1/G2/G3 at P1 level + G4 at P2 level) were verified one by one against source code and
**all confirmed**; fixed as follows:

| # | Problem | Fix |
|---|---|---|
| G1 | preamble entered both the standalone encoding and the conversation chunk: prepared_items mixed in the preamble while _frontier_item_count is a conversation coordinate, so the first commit froze S into the prefix; subsequent requests became S\|S\|Q1\|A1… and prefix identity was broken | prepared_items stores only conversation-span items (prefix+pending+tail); the preamble is freshly injected at the head of the container on every prepare at assembly time and never enters the frozen stream; the anthropic splice boundary now uses pure conversation coordinates |
| G2 | The Anthropic promotion branch only checked whether local messages were empty: when the tail was encoded standalone, a leading developer (or even SYSTEM) item was promoted into the tail's top-level system and then discarded by prepare | Promotion condition changed to boundary-aware (local empty AND has_conversation_prefix false); mid-stream system/developer items are always demoted to user blocks in chronological order; prepare merges the tail's promoted system parts in order after the shell system instead of discarding them |
| G3 | native/semantic compatibility checked only call-IDs: differing name/args also passed; native + plain-text IR assistant entered history twice | ClosedRound construction validates ordered per-call id+name+args semantic equality between the native payload and the semantic record (bool≠number, int/float numerically equivalent); observe_commit rejects all assistant-role IR when a native payload is present (both text and calls rejected) |
| G4 | _item_tool_events split into two lists, and the validator consumed all calls then all results, flattening the order within an item and ignoring the containing role | Single-pass ordered event stream (call/result interleaving preserving order) + same-pass role gating (tool_use only on assistant, tool_result only on user, completion tool_calls only on assistant); build/direct construction/deserialization share one implementation |

**Same-family problems beyond the review description (found and verified in source during the fixes):**

1. `has_conversation_prefix` counted the preamble as part of the "conversation prefix" — a wrong signal for the Completion
   developer promotion decision (F1 only fixed half of it): a tail developer following the first-round preamble would
   be wrongly demoted. Fixed to pure conversation semantics (prefix+pending, excluding the preamble).
2. Completion's adjacent system text merging did not occur at the preamble|conversation splice: the incremental path yielded
   two system items while the full encoding yielded one merged text. Added a symmetric splice merge at assembly time (O(1),
   completion only, mirroring the codec's _merge_instruction_text).
3. Deepening of the G2 fix: request-header content promoted at the tail's head renders into the top-level system, but container coordinates
   cannot freeze it into the chunk — the current round is saved but later rounds permanently lose it. Added a session-held
   `_committed_head_system` (anthropic-specific path) that re-enters every request across rounds and is persisted with the
   checkpoint.

**Stronger contract established this round (pinned by a scenario matrix):** multi-round scenarios parameterized over three wire shapes
(shell preamble + leading/mid-stream developer insertion + multiple commits + checkpoint/restore):
**after assembly, the incremental request is byte-for-byte equal to the full codec encoding** (both container and top-level system
equal) — no longer merely content/count equivalence. The old "adjacent assistant merge" limitation remains (see below);
the matrix scenarios deliberately avoid that boundary.

**Performance (explicitly requested by Nathan, A/B on the same machine, back-to-back):** steady-state prepare @800 prefix items, 1 tail item:
openai_completion 14.62→14.53ms, openai_response 20.28→20.13ms,
anthropic_messages 18.70→18.67ms (baseline 4afee09 vs post-fix). An early G4 two-pass scan
briefly regressed to 26/34/32ms; merged into a single-pass linear scan restoring parity. G3 validation parses the native payload only once,
at ClosedRound construction (once per round, not per message).

**Contract clarification (fixture adaptation, not assertion weakening):** single supply for shell and view — the same logical
message no longer enters via both the shell and the view channels (dual supply could legitimately produce two copies of the
 top-level system under the merge semantics). Two old fixtures were corrected accordingly
(test_anthropic_system_survives… changed so the view no longer carries the system already present in the shell;
test_closed_round_native_association…'s abstract payload changed to carry tool_use consistent with the semantic record
— after G3, declared IDs must be backed by payload contents).

New tests: review 8 counterexamples + a 3-shape scenario matrix (including restore) + boundary supplements (mid-stream SYSTEM,
leading developer merged into system, numeric/boolean argument equivalence, role-gating counterexamples) merged into
`tests/test_projection_third_review.py` (15 items + 14 subtests).

### Known limitations (updated 2026-09-19)

- **Adjacent assistant merge difference (pre-existing, unchanged)**: see the second-round statement above; untouched this round and
  still requiring a frozen-boundary redesign; the scenario matrix avoids this boundary.
- **Old snapshot compatibility**: snapshots predating the G2 persistence lack the committed_head_system segment; restore
  cleared (prototype stage, no production snapshots, no actual impact); schema version not bumped (structural addition,
  missing keys have deterministic semantics).
- **Single-supply contract**: incremental assembly assumes the shell and the view do not supply the same logical message twice;
  double supply is no longer silently deduplicated but produces two copies per merge semantics (see above).

### Regression for this round (per-file isolation, PYTHONPATH=$PWD/src)

- All 15 affected files green (projection×7 + llm×5 + prompt_cache×3 + setup_wizard
  + continuation_capture, 218 passed).
- llm full family, 27 files: **347 passed + 78 subtests + 6 skipped**
  (real-integration skipped by design, no paid calls), zero failures.
- The full 146-file suite was not run beyond the affected surface (this round's changes only touched the llm/projection surface).

## Fourth external review fixes (2026-09-19, targeting 422c7ba)

The three findings of `pal_projection_review_422c7ba` (H1/H2 P1-level + H3 P2-level) were verified one by one against the source:
**all confirmed**, and fixed. The review also confirmed that the round-three G1-G4 fixes themselves hold; the three items this
round are all fix lifecycle issues and pre-existing leftovers (H1/H2 are pre-existing leftovers confirmed while checking the fix lifecycle;
H3 is in the native argument compatibility check added this round):

| # | Problem | Fix |
|---|---|---|
| H1 | `observe_commit` rejection happens after pending installation: `_pending_wire_tail` has already been rewritten while frontier/ledger were not advanced, and close_round does not restore — re-feeding the tail from the old frontier doubles content (empirically demonstrated by review isolation probes) | Atomicized rewrite: all validation/trimming/chunk construction completes in local candidate variables, a single installation block commits once (rejection path causes zero session-visible changes, same-receipt retry is deterministic, prepared_items no longer pre-mutated); zero freezable commits (anthropic all-user items) changed to accept semantics commit — frontier advances with the receipt, all spans go into the open tail (F2 same rule), and an empty chunk seals the chain so it still ends at frontier (restore zero changes) |
| H2 | `ClosedRound` only has attempt/calls/results/continuation; assistant texts retained when there is no native repair have no input channel — the helper can only rebuild the tool inventory from calls, texts are never projected back (frontier taking the tail cannot make up for it) | New channel `ClosedRound.assistant_texts` (tuple of non-empty strings); when native material is present, semantic texts are rejected (a single representation per assistant contribution); `accept_repaired_round` without native materializes as a single assistant message (texts first, calls after); text-only repair (all calls trimmed) is separately representable |
| H3 | `_native_arguments_match` fills None as `{}` for comparison but replay does not fill; all three shape extractions use `.get()` and cannot distinguish missing field/null; argument field wire types not validated per shape (OpenAI string vs Anthropic object treated as arbitrary values) | contracts layer: `_MISSING` sentinel distinguishes missing field/explicit null/empty object, each with its own rejection message; raw wire types validated per shape before comparison (openai×2 must be JSON strings, anthropic must be objects, values filled when unverifiable keep the original value at replay); continuation_policy layer: three validators gain argument field structure validation (missing field/null/wrong type → Degraded → attach_native explicit ContinuationUnavailable, covering the direct attach path that bypasses ClosedRound), contract version bumped to *-2 |

**Policy choice (H1; the review offered two consistent policies, either acceptable):** chose "accept semantics commit +
hold the open tail" over "reject but state unchanged" — the receipt is evidence of a durable L1 commit;
rejection would leave projection lineage permanently behind L1, recoverable only via an expensive full rebind rebuild;
the accept path reuses the existing F2 pending mechanism, restore zero changes. The only remaining rejection
(nothing to seal: zero items beyond frontier) is now fully atomic (pinned by snapshot-level assertions).

**All three SCENARIOS.md variants pinned (parameterized over three shapes):** texts + partial calls retained
(B trimmed, A call/result pairs intact), pure text repair (all calls trimmed), zero-content repair
(exercises the H1 zero-freeze path); all include post-checkpoint/restore repeat assertions and
byte-for-byte equality between incremental and full encoding.

New tests: `tests/test_projection_fourth_review.py` (**16 items + 20 subtests**).
The review's own 7-item acceptance draft (including H1 both-branch compatible phrasing, H3 acceptance, head-system
lifecycle positive controls) ran as-is on the fixed code: **7 passed + 9 subtests**.

### Regression and performance for this round

- llm full family, 30 files (including new fourth_review), per-file isolation: **363 passed +
  100 subtests + 6 skipped, zero failures** (real-integration skipped by design, no paid calls;
  log /tmp/proj4_regress.log, ALL_DONE).
- Performance A/B (Nathan asked to watch for regressions; same machine, same script /tmp/pal_bench_prepare.py,
  prepare@800 prefix items, 1 tail item, 50 rounds): openai_completion 14.5→**14.34ms**,
  openai_response 20.1→**19.52ms**, anthropic_messages 18.7→**18.15ms**
  (baseline 422c7ba vs post-fix) — flat to slightly better, zero regression. This round's changes are all in the commit/construction
  path (once per round) and during ClosedRound construction; the prepare hot path was untouched.

## Fifth external review (2026-09-19, targeting 82a2311): pass at fix level, no new blockers

Conclusion of `pal_projection_review_82a2311`: **H1-H3 all closed; within the scope of this increment's checks,
no new blocking defects confirmed** (all 9 isolation probes passed: zero-freeze acceptance/idempotence/rejection repeatability/
candidate failure does not pollute active+pending+head/subsequent sealing after pending/head transfers only once, etc.).
Two non-blocking documentation cleanups were handled this round:

1. projection_session.py module header chunks description unified (frozen at commit = items beyond the last request's
   frontier + newly accepted outputs of this round; zero-freeze chunks record semantic spans);
2. DELIVERY pending-items wording unified (resident/Bunshin offline wiring belongs to implementation and integration;
   P7 is a separately approved launch canary; the two are kept separate).

The review also clarified the gate for the next phase (out of scope for this round): stable freeze cut/complete tool set,
The real runtime accepts the same implementation as durable checkpoint, resident/Bunshin; live canary
cannot replace offline integration.

## Sixth round external review (2026-09-19, pal_full_branch_review_f12730e, entire branch vs main): B1-B4 fixed

Scope expanded from "the last fix commit" to all net changes, compatibility of legacy call chains, and the
exit/restore semantics of the new public API. All four findings are on **new components not yet wired in**, not confirmed production regressions;
G1-G4 / H1-H3 are not reopened.

| # | Level | Issue | Fix |
|---|---|---|---|
| B1 | P1 (integration blocker) | session rebuilding ShapeContext only provides shape/endpoint_id/model_id, capabilities falls back to {}: when the endpoint declares unsupported_request_parameters=["temperature"], the legacy full encoding omits it while the new entry regenerates it—request contract inconsistency (verified by probe) | bind() adds a capabilities keyword argument (the caller-parsed verified profile, deep-frozen storage, lifetime bound to lineage); added _shape_context() as the single construction point, with shell/tail/accepted-message encodings sharing the same profile; checkpoint persists capabilities and returns it on restore (missing key → empty, faithful reconstruction of pre-B1 snapshots) |
| B2 | P2 | attach_native writes to native_by_attempt before commit; close/reject only clears _active and not unaccepted native; snapshot does not filter—cancelled payloads remain in the authoritative store and checkpoint | close_round/reject_commit discard unaccepted native of the round (drafts live and die with the round); snapshot native_records filtered by committed receipt (in-round drafts never enter checkpoint); committed native preserved as-is, late attach still rejected |
| B3 | P2 | legacy/unbound restore on a populated target only does retired=False+return False, legacy identity/chunks/native remain usable—contradicts the "fresh lineage" promise | restore starts with a pristine check (any non-empty identity/active/chunks/native/ledger/pending/head/frontier → reject with zero modification); legacy/unbound and bound paths follow the same rule, no more silent merging; fresh target compatible restore unchanged |
| B4 | P2 | begin_round advances the current fence independently of commit, but snapshot only stores the source fence of historical receipts and restore rebuilds with max(source)—a cancelled high fence regresses after restart, a stale worker rejected before restart is accepted after restart | current owner fence persisted independently (snapshot owner_fence field); restore rebuilds from that field with max(persisted, max-committed-source) to preserve monotonicity; pre-B4 snapshots missing the key fall back to max(source) (faithful reconstruction, pinned by tests) |

The review also confirmed: the legacy hot path (_iterate) is not wired to session; the usage/proxy route (read-only path)
shows no new native leaks; the shared Anthropic codec location rule change requires regression on the legacy path (included in
this round's full-family run); file_read net change is dead code removal; TLA+ Restart/Cancel boundaries closed
by product tests (B4 closed).

**Merge reminder (review recommendation adopted)**: this branch and main have diverged (main-only Telegram
pin/body/provider 0.3.3, three commits, no file overlap between the two sides, no real merge test performed);
before merging, first merge current main in a worktree and run combined regression; do not replace main with the branch's old tree.

New tests: `tests/test_projection_branch_review.py` (**13 items + 10 subtests**,
B1 includes an ordinary-endpoint temperature retention control group, capabilities deep-freeze, non-Mapping
rejection, survival after restore; B3 includes the bound path under the same rule; B4 includes pre-B4 snapshot fallback semantics).
Review's own acceptance: B2/B3/B4 six items pass as-is; B1, per its file-header note, requires fixture injection of the new
channel (assertions unchanged; after injection, all green in this repo's tests).

### This round's regression and performance

- llm full family, 31 files (including new branch_review), isolated per file: **376 passed +
  110 subtests + 6 skipped, zero failures** (real-integration skipped by design, no paid calls;
  log /tmp/proj6_regress.log, ALL_DONE).
- Performance A/B (same machine, same script /tmp/pal_bench_prepare.py, prepare@800 prefix items,
  1 tail item, 50 rounds): openai_completion 14.65ms, openai_response 20.01ms,
  anthropic_messages 18.69ms vs baseline 14.5/20.1/18.7—on par (within ±1% noise),
  zero regression. The B1 change is one context construction per prepare (pass-by-reference), no hot-path overhead.

## Seventh round external review (2026-09-19, pal_projection_review_4c20311): C1/C2 fixed

Two narrow lifecycle and restore validation issues (both P2), fixed after probe verification:

| # | Issue | Fix |
|---|---|---|
| C1 | begin_round does not reject already committed attempt IDs: duplicate delivery can re-grant a finished attempt a draft lifecycle, and close/reject then deletes the already committed native while receipt/chunk remain; re-attach after reopening overwrites the original record, and snapshot only checking ledger membership exports unaccepted new candidates as committed native | begin_round rejects already committed attempt IDs after the identity check and before fence/_active advancement (the committed ledger is the terminal record, no new state machine added); duplicate receipts go through the existing observe_commit idempotency; same-source overwrite chains are broken at the entry point |
| C2 | restore's fence branch conflates "old format missing field" (→ compatible fallback, reasonable) with "field present but invalid" (null/bool/negative/float/str/list/dict, which should also be rejected), and validation sits after the install boundary—the trailing raise leaves a half-restored target | Use field presence (`in`) to tripartite: missing → the declared compatible fallback; present and valid → max(persisted, source-max); present but invalid → ProjectionCheckpointError; validation moved before session.bind() and all state installation |

Impact scope statement (per review): C1 requires duplicate delivery/scheduler resend/upper-layer accidental attempt ID reuse to trigger,
not the normal unique ID path; frozen chunks remain, so it does not cause the next request to immediately lose history. C2 is a
fail-closed gap in the restore entry point; no evidence that actual checkpoints are corrupted.

New tests: `tests/test_projection_c_review.py` (**7 items + 12 subtests**, scenarios and
assertions identical to the review attachment, including four controls: receipt redelivery idempotency, a different-ID draft cancellation
leaving committed native untouched, old-format missing fallback, valid fence surviving). The review attachment tests
run through as-is: **7 passed + 12 subtests**.

### This round's regression and performance

- llm full family, 32 files (including new c_review), isolated per file: **383 passed + 122
  subtests + 6 skipped, zero failures** (real-integration skipped by design, no paid calls;
  log /tmp/proj7_regress.log, ALL_DONE).
- Performance A/B: prepare@800 items 14.33/19.68/18.22ms vs baseline 14.5/20.1/18.7—
  Parity with zero rollback (C1 is a single dict lookup in begin_round, C2 is on the restore path only).

## Round 8 external review (2026-09-19, pal_projection_review_c73a285): passed, no new findings

Conclusion: **C1 and C2 are closed; this incremental review found no new blocking issues and added no
Request changes items**. Independent source_probe execution passed all 8 items (same/higher fence commits both rejected and
fixture state unchanged, new draft can be opened and cancelled without touching the old native, stale owner rejected, invalid
fence all rejected, missing-field compatibility fallback including empty ledger reset to zero, legal fence monotonic, admission
6 rejected, 7 accepted). Full restore installation order is a source-code review conclusion (not a simplified wrapper verification).

The three delivery boundaries reiterated by the review (consistent with existing DELIVERY items): C1/C2 fixes can be
closed via source re-review; merging current main requires an isolated worktree real merge plus combined regression; enabling the new hot path / rollout
requires completing the existing integration and acceptance gates.

## Not covered / explicitly not done

1. **Hot path not yet wired (offline integration and rollout canary are separate)**: the resident LLMRuntime still uses
   per-message ReplayEnvelope encoding (original path untouched; all new components ready on the bypass).
   The real wiring of resident/Bunshin belongs to the implementation and integration phases (the integration parts of P4/P5);
   P7 is a separately approved rollout canary (manual restart + canary); the two are not the same thing.
   This is an intentional safety boundary, not an omission.
2. **Matrix TBD not yet resolved**: GLM family (openai_completion) and DeepSeek gateway signature
   issuance behavior remain verified at synthetic-opaque level; provider-real fixtures require sanitized
   logs you authorize, or the P7 canary.
3. **compact / RewriteReceipt**: upgrading the context-view excluded channel to an explicit receipt
   is recorded in baseline.md as a design requirement and was not implemented this round (PLAN §5.3 audit conclusion: the normal path
   has no early rewrite, and the channel is bounded).
4. **Performance items**: the full 10/100/1000/5000 gradient matrix and RSS profiling were not done (a single-scale measurement was used instead);
   zero-copy / hand-rolled JSON was not introduced (PLAN §11 explicitly says the minimal viable version skips these).

## Migration and rollback

- Old snapshots without a projection segment → new-generation lineage; ReplayEnvelope messages remain read-only compatible.
- Rollback = switch back to main (`safety/llm-projection-before-v2-7e0b1f7` is the local backup pointer);
  the new schema is written only on the branch; production data on main has not been touched by any branch code.
- No path exists where old code reads the new schema (the branch is not deployed).

## Stop-work condition check (PLAN §13)

All seven checked one by one: no gambling on API acceptance after a required native is lost (Degraded → explicit failure);
no masking of protocol failures by disabling thinking or swapping models; main/production DB untouched and rollback possible; no shared
mutable projection; no reporting pass before running (TLC/tests all have logs); no relaxed assertions;
all frontier advancement goes through receipt verification.
