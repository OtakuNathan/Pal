# Prompt cache modes

Pal separates provider cache policy from canonical L1 history and usage receipts.

| Mode | Local behavior |
| --- | --- |
| `implicit` | No explicit markers. Provider automatic caching decides reuse; routing keys remain stable. |
| `hybrid` | The stable preamble and current turn's input anchor are marked, with automatic caching enabled. |
| `explicit` | OpenAI session chase-tail: two fixed anchors and the most recent eligible closed-prefix boundaries. |

## Session chase-tail

For verified direct OpenAI and OpenRouter Responses routes, the lookup window is
52 markers: S (last stable system/developer block), T (first conversation boundary
or exact compact continuity block), and the latest 50 other legal boundaries.
Unverified compatible routes and Chat Completions use a conservative four slots.
A missing/identical anchor is omitted/deduplicated. After native compaction, the
opaque seed is never marked; the first legal block after it becomes T. Provider
lookups are not the same thing as cache writes: OpenAI saves at most the last
four marked checkpoints, and may evict or miss any checkpoint. Pal does not
predict or claim cache residency from planned markers.

Every request derives positions from its actual wire projection, including
complete tool batches. There is no remembered P/C, mutable tail accumulator,
profitability gate, estimated ACK, or turn-end reset of markers. Preparation,
retries, transport failure, restart, and late receipts cannot move the selection.
Only actual content changes (including compaction, model/tool changes and new
input) change the eligible prefix. Marker metadata is removed before content
fingerprinting. OpenAI and OpenRouter share the selector; OpenRouter also sends
`session_id` equal to the stable `prompt_cache_key`. Side questions share that
routing key but cannot replace resident warm/reminder evidence.

Hybrid sends `prompt_cache_options: {"mode": "implicit", "ttl": "30m"}` directly;
OpenRouter omits the options object while retaining its S/T markers. Explicit
sends `{"mode": "explicit", "ttl": "30m"}` on both routes. Anthropic continues
using its independent explicit cache-control policy. ChatGPT OAuth can use the
same selector when configured but remains implicit by default, preserving its
Responses WebSocket continuation path.

The UTC context date is the last stable developer block, pinned in existing L1
prompt context metadata for the current history incarnation and left generation.
Successful compact installs allow a fresh date on the next request; a failed
or cancelled compact leaves the old pin intact. An idle new turn may refresh a
cross-day pin if the matching endpoint's existing local lifetime estimate has
elapsed. Missing evidence and DeepSeek's early reminder alone do not authorize
that refresh. Otherwise a changed UTC date is appended once as an L1 fact.
There is no midnight timer or date-driven compact.

All Responses-compatible routes use top-level `instructions` for exceptional
request-scoped reminders. On verified GPT-6.2 Astra/Sol routes this field is
rendered after `input` and billed as uncached input; it counts in budgets but
not reusable prefix fingerprints. Other routes' instructions remain part of
the conservative prefix fingerprint; acceptance of the standard field does
not prove suffix caching. Standing system/developer instructions stay in
`input`. Other wire protocols use their corresponding standard request-local
system field, except verified Claude routes which use native scoped records.
Existing active legacy turns
are not rewritten when the new delivery policy becomes available. Normal rounds
need no temporary instructions. Checklist progress is delivered by actual tool
history, a short new-turn reminder, or an unfinished-only recovery record when
the relevant history has been lost.

Native compaction sends the actual compact schema (model/input and selected cache
controls), not ordinary generation tools or temporary suffix instructions. Its
read/write usage is separate from generation; cache reuse across operations is
not guaranteed. The new generation replans its anchors from the new window.
See [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
and [OpenRouter prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching).

## Configuration and activation

```sh
pal llm add ENDPOINT --replace --cache-mode explicit --runtime-root /path/to/runtime
pal llm list --all --json --runtime-root /path/to/runtime
```

`--cache-mode` also accepts `implicit` and `hybrid`. It updates the endpoint's
`capabilities_blob.prompt_cache.mode` without changing other fields. Unsupported
combinations fail before committing. The recognized models include GPT-5.6,
GPT-6 Astra/Sol/Luna, GPT-6.1 Sol/Terra, and GPT-6.2 Astra/Sol. Legacy OpenAI
profiles remain aliases, not alternate algorithms. Selection priority is
`enabled: false`, endpoint mode, legacy profile/dialect, model-hook profile,
then implicit. Existing in-flight turns retain their endpoint policy snapshot.
A configuration refresh alone cannot load changed Python implementation; a
host restart is necessary to activate new resident code.

## Evidence and checks

Transport admission remains once-only per attempted request and is recorded
independently of planning. The final merged payload is audited against selected
marker paths and exact content identity. `prompt_cache_usage` logs numeric usage
(with missing values unknown); the optional `prompt_cache_tail` diagnostic
includes marker paths and hashes. Neither log implies a provider guarantee.

`tests/test_cache_tail.py` exercises large lookup windows, fixed session anchors,
retries, failures, and independently simulated provider reuse. `SessionChaseTail`
models the stateless bounded selection; `CooperativeToolBatch` models the normal
interrupt latch. Each has a negative mutant in the TLC inventory. The former
admission-owned PromptCacheTail and PromptCacheAdmission models are retained as
historical models rather than active obligations.

### Isolated Luna provider probe (2026-11-26)

Direct OpenAI and OpenRouter Responses both accepted 52 explicit markers.
Each cold request reported 1,867 cache-write tokens out of 1,870 input tokens;
the append-only successor reported 1,867 read tokens out of 1,888 (98.89%) on
both providers. The tiny synthetic history was independent of resident L1.
A direct native compact probe also accepted the markers and reported 1,856
write tokens. In a separate generation-to-compact probe using the same routing
key and source history, compact reported zero reads: reuse between the two
operations is not established by this test. These observations do not establish
long-run production Sol hit rates.

Historical validation of the retired four-marker strategy (2026-09-17):
Linux/Python 3.13 completed core-a 875, core-b 961, bunshin-a 466 and
bunshin-b 478 passing tests (2,780 total, 7 skipped). Its TLC model checked
84,825 distinct states. These are prior-strategy results, not verification of
session chase-tail; macOS/Python 3.12 were not run in that exercise.

### Session chase-tail verification (Linux / Python 3.13, 2026-11-26)

The four repository batches ran serially, with 7,636 initial passing tests and
78 skips. Four initial failures (one unclassified safe-mode selection regression
and three outdated request/continuation fixtures) were corrected during the
checks; all four were retested successfully on the final source. Both Bunshin
batches completed without failures. These were iterative validation runs, not a
single clean full-suite run of the final checkout. Final focused regressions,
67-file contract type checking, 21 architecture boundary checks, the formal
inventory, and both new TLC models with their expected-failure mutants passed.

Claude turn-scoped reminders are orthogonal to the Anthropic cache-control
strategy: keep the immutable record, let the provider clear its semantic effect,
and mark only ordinary cacheable content. Gemini's root interaction-scoped
instructions are request-local; an isolated Hydrogen probe showed that changing
the root can lose cache reuse. Since healthy requests no longer carry temporary
instructions, the default uses that standard field only for exceptional controls.
See [prompt context lifecycle](prompt_context_lifecycle.md).
