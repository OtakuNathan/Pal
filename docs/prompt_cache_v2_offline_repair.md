# Astra prompt-cache v2: offline repair

This branch repairs cache evidence, request diagnostics, and attempt accounting on
PR1–PR3 (`09ec6d4`), with the socket interruption/backpressure fix cherry-picked
from `ae0f872`. It does not change the default Astra profile, deploy runtime code,
or invoke a paid provider. The wire behavior of the profiles follows the supplied
v2 plan; offline tests cannot establish provider acceptance or cache hit rates.

## Evidence and accounting

A submitted breakpoint is a local placement decision, not proof of provider cache
coverage. Track state now says `submitted`; compatibility `confirmed` fields stay
false/zero. Aggregate read/write usage never confirms a particular anchor or
frontier, and estimated prefix sizes never reduce billed costs as confirmed coverage.

All current adapters lack evidence attributing cached tokens to an exact local
boundary. Cache warm-deadline reminders and hot-cache compaction are consequently
ineligible (`boundary_evidence_unavailable`). Ordinary context-pressure compaction
is retained. Restoring those optimizations requires an adapter that can establish
boundary identity, coverage, and freshness; aggregate usage is insufficient.

Usage stores numeric raw counters and field presence separately. Explicit zero is
known; an omitted field is unknown. OpenAI shapes use inclusive input accounting;
Anthropic uses input excluding cache reads/writes. Final cumulative usage can
correct earlier counters downwards; replayed intermediate frames cannot replace a
terminal settlement. Independent attempts are summed, never merged as snapshots.
Negative or inconsistent values retain anomaly evidence rather than inventing a
successful cache outcome.

Each started provider attempt settles once, including timeout, decoding failure,
provider error, cancellation, retry, and output recovery. Partial observed usage
and generation/provider/model identity survive failure. Logical success accounting
does not charge the same attempt twice. Bunshin reports the same settlement through
its idempotent receipt path. The displayed cost is a known subtotal while any
attempt's cost is unknown; cache partition completeness is reported separately.
Preparation errors do not invent provider attempts. The ledger is process-local,
not a durable reconciliation system for provider invoices.

## Profile selection

The immutable registry contains these three version-2 profiles:

- `openrouter_astra_legacy_explicit` (existing default behavior).
- `openrouter_astra_provider_implicit`.
- `openrouter_astra_hybrid_anchor`.

They apply only to OpenRouter, exact model `openai/gpt-6-astra`, and
`openai_response`. Unknown or mismatched explicit selections fail before transport.
`prompt_cache.enabled = false` takes precedence. Endpoint profile/dialect selection
overrides the model hook's optional `CACHE_PROFILE_REF`, then the existing default
applies. Hooks reference profiles; they do not own cache runtime state.

Selection is captured in the turn settings snapshot, including Bunshin turns.
Refreshes take effect on the next turn. Diagnostics identify profile version,
origin, and generation. Local state additionally separates turns, endpoints,
providers, shapes, and profile generations. Upstream session/cache keys remain
stable across rounds in a turn; local isolation does not create a new key per
request.

## Diagnostics

The bounded attempt ring (128 entries by default, 256 tracked scopes) records
turn/task/round/attempt identities, timing, profile provenance, actual provider
identity, numeric usage evidence, estimated reusable tokens, and applied marker
paths. It retains hashes and byte lengths of stable input, history, dynamic input,
and tools, plus the first changed visible wire item/component. Prompt text, tool
results, and encrypted replay bodies are not retained in diagnostic descriptions.
Estimates use visible serialization and are not provider token counts.

Frontier-stall warnings require three consecutive comparable observations: prefix
growth of at least 1,024 estimated tokens per step, read variation at most 1,024,
and explicitly reported zero writes. Missing fields, anomalies, unknown/changed
provider, prefix/tools/parameter changes, dynamic input changes, failures, or gaps
over 300 seconds reset the evidence streak. A warning triggers no cache-policy or
compaction changes. Late settlements remain billable but cannot overwrite newer
scope observations.

## Offline validation

`tests/test_prompt_cache_v2_runtime.py` exercises the real Pal core, memory/compiler,
turn executor, model hook, endpoint invoker, codec, and cache coordinator. Only the
provider transport is replaced. All three profiles run 8-round and 50-round
same-turn fixtures with tool calls/results and opaque reasoning replay. Additional
runs change dynamic input, tools, compaction state, and the configured profile.
Reported fixture usage/cost is synthetic and is not a cache-performance measurement.

Targeted tests also cover corrected/missing usage, stream sequence replay, failed
and cancelled attempts, retry/recovery accounting, configuration rejection,
Bunshin receipt replay, and interrupted stall-evidence streaks. Validation runs use
`PYTHONPATH=src` to ensure this worktree is imported, and `PAL_TEST_LLM_API_KEY=` to
skip opt-in paid integration tests. Hypothesis dependencies are installed only in
`/tmp/pal-cache-v2-test-deps`; no runtime configuration is changed.

The validation host is Linux ARM with Python 3.13. CI's Python 3.12 and macOS runs
remain external validation. Live cache-hit improvement and provider billing must
be measured separately after an explicitly authorized activation and online test.

### Validation record (2026-09-16)

| Check | Result |
| --- | --- |
| `core-a` full batch | 1,099 passed; one Hypothesis input-generation speed health check failed |
| Schema properties and stall guards, separate rerun | 274 passed, including the previously failing property test; no health checks suppressed |
| `core-b` full batch | 899 passed, 6 opt-in paid integration tests skipped; one old observation-window fixture failed |
| `core-b --lf` after fixture correction | 1 passed; final policy/receipt suite also passed (35 tests) |
| `bunshin-a` full batch | 466 passed, 77 subtests passed |
| `bunshin-b` full batch | 477 passed, 122 subtests passed |
| Bunshin receipts and cache policy, final focused run | 35 passed, 2 subtests passed |
| Final usage/codec/runtime/receipt regression (excluding already-passed 50-round cases) | 77 passed, 45 subtests passed |
| Avatar backpressure/transport regression | 48 passed; Avatar source unchanged |
| `git diff --check` and Python compilation | Passed |

The first full `core-b` invocation could not collect tests because Hypothesis was
missing; the isolated test dependency installation described above resolved that
collection error. Full batches are not silently represented as clean first runs:
the Core-a timing failure and its successful separate rerun are recorded above.
Core-b had already collected the old observation-window fixture before it was
updated: duplicate settlement of one plan now correctly counts once. The corrected
fixture creates distinct request plans and passed both the policy suite and the
final failed-test rerun. No unresolved test failures remain.
The three 50-round profile cases passed in the Core-a batch before the final
partial-usage anomaly fix, which received its own focused regression test.


### Incremental planning correction

The follow-up restores incremental economics for the legacy explicit planner.
The stable prefix, unchanged user input, and previously submitted frontier supply
`planning_base_tokens`; only growth beyond that baseline contributes to the next
checkpoint decision. Large frozen history cannot make a tiny new suffix appear
profitable. `economics_assumption = incremental_reuse_estimate` labels this planning
estimate separately from observed usage and billing. Submitted coverage remains
unconfirmed, and unknown actual cost remains unknown.

The existing accumulated-reprocessing heuristic, configured read/write multipliers,
minimum prefix length, and net-benefit threshold are preserved. This heuristic is
not a guarantee of future reuse or provider savings. Higher write premiums or no
read discount can defer a new checkpoint even when the prefix is large. Actual
cache writes, reads, and total costs remain the acceptance criteria; high hit rate
alone is insufficient. This correction does not switch the selected profile.

Validation of this correction: 67 cache policy/evidence/diagnostic/runtime tests
passed, including all three 50-round profiles. A final policy and usage run passed
38 tests, including the new incremental-cost regressions. No paid calls or runtime
activation were performed.
