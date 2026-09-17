# Explicit cache handoff

OpenAI explicit endpoints (Responses and Chat), including OpenRouter's OpenAI
explicit dialect, now share `openai_explicit_economic_v1`. Existing explicit
profile names remain configuration aliases; implicit, hybrid and Anthropic
strategies are unchanged. No price multiplier or profitability threshold changed.
The new profile can also be selected explicitly in `prompt_cache.cache_profile`.

A successful HTTP response or cache write does not advance the economic baseline.
Internal state version 2 keeps S (stable system/developer endpoint), U (fixed user
endpoint), F (accepted round frontier) and at most one pending C. Every legal S
and U is sent, independently of minimum estimated size, profitability, missing
usage, candidate failure or cooldown. Identical positions are deduplicated;
the actual request carries at most four markers. Only F/C use the economic gate.
“Accepted” below means **estimated attribution**, not a provider receipt.

For an ordinary turn, U ends at the last actual user input, excluding runtime
context appended using the user role. On the first turn observing a new compact
generation, U ends at the compact block itself, including when it is the first
block of a larger user message. Later rounds retain this exact boundary; the next
turn uses its new user input. Without active user input, the compact block is the
fallback. L1 projection metadata and codec paths identify the block; no text search
or neighboring-block fallback is used.

## Evidence algorithm

1. Always send S/U. Marker presence and observed read coverage are separate facts.
   The baseline is the furthest read-covered S/U or accepted F, using its local
   estimate coordinate; without evidence it is zero. In the audited closed
   explicit set, a positive read covers S. U requires H strictly above reliable
   frozen upper bounds of **every earlier marked boundary**. A read from F/C also
   covers U; this does not prove that U itself has a resident cache entry.
   Unknown earlier bounds prevent U confirmation, never U transmission.
   Successful final inclusive input/read counts can establish upper bounds for
   audited markers; evidence is learned only after judging the current response.
   C may be proposed while U remains unconfirmed, once all competitor bounds are
   known. Its position must be beyond U (or S if U is unavailable).
2. At actual submission, freeze every competing marker's independent upper bound
   and its source request/version. M is their maximum. Any unknown competitor
   makes M unknown; zero is permitted only for an empty competing set.
3. The first eligible response calibrates C: `E = actual_input - estimated_suffix`,
   `epsilon = max(256, ceil(0.25 * estimated_suffix))`. Freeze the resulting
   interval, intersected with `[0, actual_input]`, and the calibration send sequence.
   Opaque replay or multimodal suffixes cannot supply this estimate.
4. Only a later **sent** request may confirm C. It must have the same candidate
   and current owner, a preserved prefix and an audited complete marker set,
   successful final inclusive input/read counters without anomalies, `H > M`,
   and H within the calibration interval. Current H cannot tighten its own M.
   Missing write counters do not block this check; `delta H > 0` is not required.
5. ACK moves the estimated baseline to C's **estimated coordinate**, never to H.
   `ack_source=estimated` does not enable exact-evidence warm deadlines or hot-cache
   compaction. Diagnostics supplied by the provider remain auxiliary evidence.

M excludes competing explanations; the heuristic interval checks plausibility.
This relies on explicit lookup being restricted to the audited markers and on
comparable, continuous-prefix counting. The gateway honoring those semantics is
an assumption, not something a local hash or TLC can establish. A marker is not a
lease and an observed read does not guarantee future residency.

If the calibrated interval lies entirely below M, terminate the trial as
`candidate_inseparable_from_old_bounds`, preserve the estimate and wait for a
farther boundary or independent new evidence. This is not a provider failure.
`read_observed_unattributed` and `candidate_reuse_not_observed` remain distinct.

## Lifecycles and accounting

The bounded session index excludes turn ID; the upstream cache key already did.
Endpoint/model/shape/provider URL, profile generation and policy binding isolate
state. Final returned provider/model/tier changes invalidate active evidence.

Turn closure revokes old responses' handoff authority, discards F/pending C and
ends the estimate epoch. The next turn sends its new U immediately. There is no
anchor candidate or cross-turn candidate adoption. S/read evidence survives only
while its exact encoded prefix and binding survive. Changed U starts unconfirmed;
unchanged U may retain coverage. Compact/replay retirement invalidates affected
prefix evidence; message IDs alone cannot preserve it. Provider/model/tier changes
clear read evidence and F/C while retaining legal fixed marker placement.

The unchanged economic heuristic is:

```
D = candidate_estimate - baseline_estimate
net = (R + D) * (1 - read_multiplier) - D * max(write_multiplier - 1, 0)
```

R is estimated repeated processing, not an exact bill or guaranteed future saving.
A proposal must be at least as far as the greatest already submitted target;
this ensures all existing R lies at or before C.
Existing R enters `through_C`; subsequent actual attempts at p settle once:

```
through_C += max(0, min(p, c) - b)
after_C   += max(0, p - c)
```

ACK discards through_C and preserves after_C. Abandonment preserves their sum.
For b=40K, c=60K and p=70K/80K, ACK retains 30K; abandonment retains 70K.
Fixed-anchor confirmation during a trial must also settle only covered costs.
Alongside the two C sums, maintain `after_S` and `after_U`. Each comparable actual
request contributes `max(0, p - max(b, x))` to the accumulator for fixed point x.
When x becomes the new baseline, remaining R is `after_x`; if C is pending,
`through_C = after_x - after_C`. Keep `after_C` unchanged. Counters for fixed points
at or behind the new base become remaining R. C ACK sets them to `after_C`.
For b=40K, U=50K, C=60K and p=70K/80K, U confirmation retains 50K (20K through C,
30K after C); subsequent C confirmation settles only through C.

Comparable late requests use the current split, even if their owner cannot ACK.
A changed accumulated prefix resets only the economic estimate epoch. Actual
billing remains in the existing identity-based usage ledger.

There are at most three actual carrying-C submissions, including retries, and a
30-minute deadline starting at the first submission. The third response can ACK;
a fourth request continues with protected markers only. Failure cooldown counts
three distinct normally completed rounds, excluding the triggering round and
length-recovery attempts. Builds, duplicate frames and retries cannot accelerate
it. No extra provider request is made to probe the cache. Idle state expires on
subsequent access or bounded-index eviction; no timer wakes the model.

## Wire contract and diagnostics

Normalization operates only on protocol envelopes/content blocks/tool definition
envelopes. It removes known legacy native and gateway-convertible cache controls,
then inserts exact selected targets, with no fallback to a neighboring block.
Tool parameters, tool-call arguments and user text are never recursively cleaned.
The final merged payload is audited for the full marker set, placement, cache keys
and content fingerprints. Failure disables attribution for that submission.

Content identity excludes known cache controls but includes tool schemas, call IDs,
roles and replay. Wire-audit identity separately includes marker paths/options and
routing keys. Removing an old F marker therefore does not invalidate the new F's
content identity. Hash-only attempt logs include M, bound sources and versions,
calibration/attempt state and the local decision; no control messages enter L1.
With the existing `prompt_log_enabled` switch, submitted and settled attempt
records are logged as `prompt_cache_handoff` JSON. Fixed marker paths, read flags,
base source, candidate and audit evidence distinguish “sent” from “read-covered”.
Absent usage fields are null in these log records, not fabricated zero writes.

To inspect a live run, group these records by attempt ID and compare submitted
and settled records. The following distinctions are intentional:

| Observation | Meaning |
| --- | --- |
| S/U in `planned_markers` and `applied_marker_paths` | Local payload carried the fixed markers; inspect the audit result as well. |
| `base_source=S`, `anchor_read=false` | U was sent, but there is insufficient evidence to use its coordinate as the economic base. |
| `base_source=U`, no F | A read covered U; there has been no accepted frontier handoff. |
| Pending C, unchanged fingerprint across attempts | The same candidate is being tested, not silently moved on each request. |
| `estimated_ack`, `base_source=F` | A later request passed attribution using its frozen bounds and interval. |
| Reads repeatedly equal S, trial exhausted | No acceptable C-reuse evidence arrived within the budget. This alone does not locate the fault in Pal, the gateway or the provider. |

The policy intentionally advances U when a new turn starts. A single-round turn
has no later same-turn request to test its new U or C; mandatory U transmission
alone is not a guarantee of historical-prefix reads across such turns.


## Offline verification and activation

`spec/llm/PromptCacheHandoff.tla` is a finite handoff abstraction, not a refinement
proof of the Python implementation. Its initial state is after proposal; competing
S/U/F protection is abstracted by an old boundary and an independent conservative
M. Full four-position payload construction is covered by Python integration tests.
Provider cache membership and served boundary
are independent environment variables; local ACK uses only reported H, frozen M,
interval and owner/sequence. Ghost truth appears only in the soundness invariant.
The model checks protection, marker/attempt limits, owner isolation, accounting,
third-response opportunity and fair bounded termination. It does not assert that
an arbitrary candidate must eventually become identifiable or that F stays cached.

Run:

```
bash scripts/check_llm_tla.sh /path/to/tla2tools.jar
python scripts/check_cache_handoff_counterexamples.py /path/to/tla2tools.jar
python -m pytest -q tests/test_cache_handoff.py tests/test_prompt_cache_policy.py
```

The mutation runner requires counterexamples for dropping F, optimistic promotion,
stale ownership, ignoring M, clearing C on the third submit, hidden markers,
circular evidence, omitting fixed anchors and using an unproven U baseline. Python regressions
cover independent mock-provider reads across ten single-round turns, both OpenAI
wire shapes and both bindings, plus payload, lifecycle and economic edge cases.
`PromptCacheSettlement.tla` separately checks the two accumulators against a
per-request ghost ledger, including arbitrary receipt order, duplicate receipts,
ACK, U confirmation during a pending trial, abandonment and epoch invalidation. It assumes the handoff guard already
accepted the ACK. Its negative checks discard all R on ACK or U confirmation, ignore late
receipts using a sequence high-water mark, and charge duplicates twice.
`PromptCacheFixedAnchors.tla` independently checks mandatory S/U transmission
and read-covered baselines across a turn change.
Existing 8/50-round compiler/runtime tests and the four CI batches cover integration.

This is an offline code change. Activation of the resident runtime requires a
separate host restart; no runtime restart, endpoint reconfiguration, paid cache
probe or release is part of this change. Live upstream reuse and savings remain
to be measured from actual attempt logs after activation.

Protocol references: [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching),
[OpenAI diagnostics](https://developers.openai.com/api/docs/guides/prompt-caching/diagnostics),
and [OpenRouter prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching).
These describe explicit lookup and aggregate diagnostics; none supplies the
per-marker reuse acknowledgement required to label this evidence exact.

State-version-2 validation on Linux/Python 3.13 (2026-09-17): the focused cache,
continuity and 8/50-round runtime suite passed 125 tests. A final handoff-only run
passed 50 tests. TLC explored 10,980 handoff states, 736 fixed-anchor states
(including provider eviction) and 60 settlement states; all 13 deliberate faults
produced their expected counterexamples. Full batch results are recorded below.
No paid provider canary, macOS or Python 3.12 run was performed. Local checks cannot
establish gateway behavior or future cache residency.

| Full batch | Result |
| --- | --- |
| core-a | 1,029 passed, 7 skipped |
| core-b | 829 passed; one old explicit-wire snapshot expected S alone. Updated to require S/U; all 7 tests in that file passed afterward. |
| bunshin-a | 466 passed |
| bunshin-b | 477 passed; one sandbox subprocess exceeded its 20-second deadline during the parallel batch run. Its isolated rerun passed. |

No production code was changed for the sandbox timeout. The full batches were
not repeated after those focused checks; the final handoff regression also covers
the last logging and turn-closure adjustments. Test dependencies were reused from
a temporary virtual environment; runtime configuration was preserved.
