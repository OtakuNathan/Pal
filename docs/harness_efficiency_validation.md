# Harness efficiency validation

The changes are in the main working directories, uncommitted. No runtime was
activated, no model provider was called, and Native remains version 0.4.0.

## Streaming comparison

The same 100 completed tool rounds and 1,000 assistant stream fragments were run
against the pre-change source and the current L1 owner. Both snapshots end with
identical assistant text. The measurement counts messages visited by protocol
validation in the fragment loop (round setup and final snapshot are excluded).

| Measurement | Before | After |
| --- | ---: | ---: |
| Protocol checks | 1,000 | 1,000 |
| Message visits | 202,000 | 1,000 |
| Observed loop seconds | 1.193 | 0.082 |

Wall-clock times are illustrative, taken on Linux aarch64 alongside regression
work; the traversal counts are the regression criterion. Request rendering and
explicit snapshot reads still traverse their selected history. No provider cache
hit-rate or model-cost claim follows from this result.

`test_l1_context_index.py` also exercises the real core streaming handler: 50
additional fragments continue reaching channel delivery while whole-turn snapshot
construction is forbidden. A terminal provider error discards only that response;
previously returned snapshots remain unchanged.

## Shell ordering and recovery

Native tests block the release operation after a command completes, commit its
result into L1, and verify that the next model request begins before release is
unblocked. Other cases cover five-attempt exhaustion, reconnection without budget
reset, session-zero identities, permanent errors, failed output abandonment,
capacity failure without command replay, and owner shutdown.

The existing SessionLifecycle and HostObservation TLA+ configurations passed
(60,824 and 157,948 distinct states). OutputCleanup passed 193 distinct states,
including lost release replies and independent model dispatch. These are bounded
model checks, not an unbounded proof.

## Regression record

- L1/projection/protocol final focused suite: 40 passed.
- Additional IR/stream/tool activity suite: 68 passed, 43 subtests passed.
- Native host full pass: 137 passed, 19 subtests passed; subsequent capacity,
  shutdown and local-cleanup error checks are recorded in the final run logs.
- Native standalone worker tests: 67 passed, 2 skipped, 26 subtests passed.
- Final Native cleanup/remote audit: 27 passed, 2 subtests passed. This includes
  the added capacity and local-cache-removal failures and shutdown checks.

| Pal batch | Initial outcome | Follow-up |
| --- | --- | --- |
| core-a | 762 passed, 7 skipped, 2 failed | Shell pipeline timing case passed individually; the pre-existing memory review assertion remains failing. |
| core-b | 1,025 passed | Completed in 20m49s. |
| bunshin-a | 465 passed, 1 failed | Updated the stale assertion for the already-revised reading-strategy prompt; that case passed. |
| bunshin-b | 477 passed, 1 failed | Sandbox startup timeout passed an individual rerun. |

The full suite is therefore not reported as all green: the baseline memory review
assertion remains unresolved outside this change. No test was skipped to hide it.

The pre-existing memory review UI changes are preserved byte-for-byte. The
runtime-compaction integration test still expects the former review overview
wording and buttons. Its failure was reproduced with the pre-harness source plus
those same pre-existing UI changes. This pass does not alter that feature or its
old assertion. A sandbox startup timeout and shell pipeline timing failure in the
parallel run both passed individual reruns.

Validation is local Linux only; macOS and Windows were not run. Logs and the
comparison script are retained under `~/.local/state/pal/harness-efficiency/`.
