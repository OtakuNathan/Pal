# Prompt cache modes

Pal selects cache behavior independently of the endpoint's wire encoding.
Provider responses report usage; they do not grant permission to move a tail
checkpoint. The former OpenAI economic/estimated-ACK controller has been removed.

| Mode | Local behavior |
| --- | --- |
| `implicit` | No local markers or automatic-cache opt-in. The provider decides whether and how to cache. Stable routing/accounting keys are retained where applicable. |
| `hybrid` | Explicit structural S/T markers plus OpenAI automatic caching. No local tail history. |
| `explicit` | OpenAI explicit-only requests carry S/T and at most two tail positions, previous P and current C. Every new legal stable tail is eligible immediately. |

S ends at the stable system/developer prefix. T is the existing U boundary:
the current real user input, or the compact continuity block for the first turn
using that compact generation. Runtime context presented as user content is not
an independent user anchor. Identical positions are deduplicated.

Hybrid wire encoding differs by provider: direct OpenAI requests send
`prompt_cache_options: {"mode": "implicit", "ttl": "30m"}`. OpenRouter requests
omit that object and send only the S/T block markers and routing keys; automatic
caching remains enabled. OpenRouter's [published schema](https://openrouter.ai/openapi.json)
only accepts `explicit` as the request-level mode (checked 2026-09-23).
Explicit mode still sends `{"mode": "explicit", "ttl": "30m"}` on both providers.

The OpenAI implementation keeps the last two distinct submitted tail positions.
The next request carries the most recent still-valid position before its current
C. Rebuilding a request or retrying the same C does not consume the previous
position. Only submission changes history; usage, errors and late receipts do
not. Turn completion clears it, and prefix changes invalidate affected positions.
There is no R accumulator, profitability gate, estimated ACK, attempt budget or
cooldown in this strategy.

## Configure

With the updated package installed:

```sh
pal llm add ENDPOINT --replace --cache-mode hybrid --runtime-root /path/to/runtime
pal llm list --all --json --runtime-root /path/to/runtime
```

`--cache-mode` also accepts `implicit` and `explicit`. It updates
`capabilities_blob.prompt_cache.mode` and enables local policy selection while
preserving unrelated endpoint fields. Unsupported combinations fail before the
endpoint update is committed. The JSON list shows persisted capabilities; it is
not evidence that a running process has reloaded them.

Policy selection order is: `enabled: false`, endpoint `mode`, legacy endpoint
profile/dialect, model-hook profile, then `implicit`. Explicit endpoint selection
overrides a model-hook default. Configuration refresh affects the next turn;
in-flight turns retain their policy snapshot. Changing the endpoint identity
under a snapshot is rejected.

The three modes are available for the recognized OpenAI GPT-5.6 and GPT-6 Astra
families on Responses and Chat Completions, directly or through OpenRouter.
Unknown compatible endpoints/models get only `implicit`; schema resemblance
does not establish support. Anthropic Messages retains its existing explicit
strategy behind the shared selector, with `implicit` also available. Anthropic
hybrid and migration to the new tail strategy are deferred.

Legacy OpenAI explicit profiles, including `openai_explicit_economic_v1`, now
select eager explicit behavior. `openrouter_astra_provider_implicit` selects
implicit; `openrouter_astra_hybrid_anchor` now sends both S/T. Old names remain
configuration aliases, not parallel copies of the old algorithm. Alias cache-key
generation remains compatible. New mode values do not enter the upstream key;
mode and configuration generations isolate local history instead.

## Guarantees and limits

The client checks legal exact marker positions, content fingerprints and the
final marker set. It does not claim a marker was stored or a prefix remains
resident. There are at most four explicit markers; hybrid uses two plus the
provider's automatic slot. Protocol references: [OpenAI](https://developers.openai.com/api/docs/guides/prompt-caching)
and [OpenRouter](https://openrouter.ai/docs/guides/best-practices/prompt-caching).

If C1 writes successfully but C2 fails, the next request containing C2/C3 may
lose the opportunity to read C1 and fall back to T. This is accepted best-effort
behavior. Neither hybrid nor eager explicit is claimed to fix the reported
upstream tool-output caching issue or guarantee lower spend.

The ideal linear-cost argument and its assumptions are recorded in the
[design plan](prompt_cache_strategy_modes_plan.md). The runtime does not estimate
continuation probability or switch modes based on cache-hit percentages.

Diagnostics remain in local logs. With prompt logging enabled, `prompt_cache_tail`
records exact marker paths, content/audit hashes, mode, submission history and
reported usage. Missing counters remain unknown. Shared usage-ledger deduplication
and billing remain active; removing ACK does not remove request accounting.

## Validation and activation

`tests/test_cache_tail.py` covers all mode/provider/shape combinations, repeated
builds and retries, late responses, invalidation, and an independent simulated
provider including the failed-C2 counterexample. Existing wire tests retain exact
compact-block positioning, fingerprint and role/protocol checks.

`spec/llm/PromptCacheTail.tla` checks bounded ordered history, marker capacity,
fixed anchors and submission-only updates across content/mode generations.
The old Handoff, FixedAnchors and Settlement models are historical; the normal
LLM model-checking script now runs PromptCacheTail instead.

This change replaces core LLM implementation modules. An already running host
needs to load the updated package through a host restart; configuration refresh
alone does not replace loaded Python code. After code activation, later endpoint
mode changes can use the existing `/refresh_llm_endpoint` path and take effect on
the next turn. No paid requests are needed for the offline checks.

### Verified implementation (2026-09-17)

On Linux / Python 3.13, all four repository batches completed successfully:

| Batch | Passed | Skipped |
| --- | ---: | ---: |
| core-a | 875 | 0 |
| core-b | 961 | 7 |
| bunshin-a | 466 | 0 |
| bunshin-b | 478 | 0 |

Total: 2,780 passed, 7 skipped (subtests reported separately). The initial parallel
run was interrupted to reduce memory pressure; these counts are from the final
completed batches. A legacy safe-mode test was updated to explicitly request
explicit caching instead of relying on the former default.

TLC checked 84,825 distinct states for PromptCacheTail with no errors. The tests
include the documented loss of C1 reuse when C2 fails; this remains an accepted
strategy tradeoff. No paid cache canary or live host activation was performed.
macOS / Python 3.12 were not executed in this environment.
