# Explicit cache handoff

OpenAI explicit endpoints (Responses and Chat), including OpenRouter's OpenAI
explicit dialect, now share `openai_explicit_economic_v1`. Existing explicit
profile names remain configuration aliases; implicit, hybrid and Anthropic
strategies are unchanged. No price multiplier or profitability threshold changed.
The new profile can also be selected explicitly in `prompt_cache.cache_profile`.

A successful HTTP response or cache write no longer advances a rolling baseline.
The controller keeps S (stable instructions), U (accepted user anchor), F (accepted
round frontier) and at most one pending C. Identical positions are deduplicated;
the actual request carries at most four markers. C does not replace the old
protected positions until a subsequent request supplies qualifying read evidence.
“Accepted” below always means **estimated attribution**, not a provider receipt.

## Evidence algorithm

1. Bootstrap with S alone where available. Prior successful final input counts
   bound every marker actually audited in that request. A positive read with S as
   the sole marker can tighten its bound and establish the initial estimated
   baseline. Establishing that baseline starts a fresh economic estimate epoch;
   it does not alter the usage ledger. Without competing bounds, defer new trials.
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

Turn closure revokes the old owner's response authority. An anchor candidate can
be adopted by the next turn only if its exact encoded prefix remains present.
It retains candidate identity, calibration, attempt count and original deadline.
A pending round frontier is not adopted. Settling L1 may retire replay or change
roles; the resulting prefix change invalidates affected evidence. Neither message
ID nor a session index can override that check. Accepted protection survives only
while its exact prefix survives.

The unchanged economic heuristic is:

```
D = candidate_estimate - baseline_estimate
net = (R + D) * (1 - read_multiplier) - D * max(write_multiplier - 1, 0)
```

R is estimated repeated processing, not an exact bill or guaranteed future saving.
A proposal must be at least as far as the greatest already accumulated target.
Existing R enters `through_C`; subsequent actual attempts at p settle once:

```
through_C += max(0, min(p, c) - b)
after_C   += max(0, p - c)
```

ACK discards through_C and preserves after_C. Abandonment preserves their sum.
For b=40K, c=60K and p=70K/80K, ACK retains 30K; abandonment retains 70K.
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
circular evidence and dropping anchors at every turn boundary. Python regressions
cover independent mock-provider reads across ten single-round turns, both OpenAI
wire shapes and both bindings, plus payload, lifecycle and economic edge cases.
`PromptCacheSettlement.tla` separately checks the two accumulators against a
per-request ghost ledger, including arbitrary receipt order, duplicate receipts,
ACK, abandonment and epoch invalidation. It assumes the handoff guard already
accepted the ACK. Its three negative checks discard all R on ACK, ignore late
receipts using a sequence high-water mark, and charge duplicates twice.
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

Validation on Linux/Python 3.13 (2026-09-17): core-a 1005 passed/7 skipped,
core-b 830 passed, bunshin-a 466 passed, bunshin-b 478 passed. Subsequent focused
cache checks passed 75 tests; the 8/50-round integration scenarios also passed.
The handoff model explored 53,424 distinct states; settlement explored 36.
All eleven deliberate faults produced the expected invariant counterexamples.
Hypothesis was installed only in a temporary test virtual environment. macOS and
Python 3.12 were not exercised locally; no paid provider canary was run.
