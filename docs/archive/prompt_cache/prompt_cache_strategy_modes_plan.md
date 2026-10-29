# Pal Cache Tiering and Tail Breakpoint Simplification Plan

> Historical record, archived on 2026-10-05 from `docs/prompt_cache_strategy_modes_plan.md`.
> Statements about implementation, deployment and test results describe the
> original work, not the current checkout or running host. See the
> [current documentation index](../../README.md).

Status: Implemented; offline regression and formal verification passed; the running instance is not yet activated.

Review baseline: `99506b6`. Implementation should follow the actual working tree; check for later changes first.

This plan replaces the previous in-session proposal to "keep the OpenAI economic controller." The already-decided hybrid scope is retained, along with the decision that Anthropic will not migrate to the new handoff algorithm for now. Saving this document does not modify running configuration, activate, commit, or release anything.

## 1. Why Simplify

Assume a readable boundary F already exists, and a new stable interval D is added; normal input price is 1, write is w, read is r. The current request is followed by K full-reuse requests, the cache remains readable throughout, and writes are billed linearly at the uncached new interval.

The immediate write cost is `(w + Kr)D`; delaying the write by j requests costs `[j + w + (K-j)r]D`, where `0 <= j <= K`. Delaying overpays `j(1-r)D`. Therefore, for an interval that will definitely be cached eventually, waiting itself does not save the write premium.

The benefit of writing immediately versus never writing is `[K(1-r) - (w-1)]D`. With `w=1.25, r=0.1`, a single subsequent successful reuse is enough to cover the write premium. Pricing basis: [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching).

When the future is unknown, `E[K] > (w-1)/(1-r)` is the expectation comparison between the two choices of writing immediately and never writing; it does not prove that all adaptive strategies of "wait for new information before deciding" are worse. Only when K takes 0 or 1 is 27.8% directly the probability threshold for continuing once.

Accordingly, the OpenAI new strategy removes R, estimated_net, the benefit trigger threshold, the three-candidate budget, cooldown, and estimated ACK. The first version adopts an explicit eager strategy: whenever a legitimate new stable tail exists, a breakpoint opportunity is offered, without estimating continuation probability. It is a simple policy choice and does not claim global optimality under real providers.

## 2. Three Tiers

The endpoint selects `implicit`, `explicit`, or `hybrid` in the existing `capabilities_blob.prompt_cache.mode`.

| Tier | Behavior |
| --- | --- |
| implicit | No local breakpoints inserted, no tail planning runs; the necessary stable session identifier is carried over, and the upstream decides cache behavior |
| hybrid | Fixed S/T explicit breakpoints, keeping upstream automatic caching; P/C is not managed locally |
| explicit | Uses S/T/P/C on supported OpenAI paths; upstream automatic breakpoints are turned off, mechanically offering the most recent tail reuse opportunity |

S is the end of the stable system/developer prefix; T corresponds to the U in the current implementation: for a normal turn it is the end of the current user input, and after compact the existing compact block boundary rules apply. T is a structural position and does not represent confirmed cache success.

OpenAI implicit mode allows carrying additional explicit breakpoints; automatic breakpoints occupy one of the four write slots. hybrid's S/T falls within this capacity. [Protocol notes](https://developers.openai.com/api/docs/guides/prompt-caching)

Anthropic's first version unifies the selection interface while keeping the existing explicit implementation; it does not migrate to the new rolling strategy for now, nor open up the new hybrid. Its historical lookup and usage semantics need separate handling. [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

## 3. Minimal Local State for explicit

P denotes the tail position actually committed previously, and C denotes the latest legitimate stable tail this time. Neither is a server-side success confirmation.

1. Following the codec's exact content path and prefix identity, select S and T, plus the latest legitimate stable C after T. Tool history must be selected at complete protocol batch boundaries, never moved to other roles or faked as user messages.
2. Locally, keep only the two most recent distinct committed tail descriptions within the same content generation. P is the most recent position among them that precedes C and whose prefix is still completely identical.
3. Send the deduplicated S/T/P/C, at most four. When C coincides with T, it is not sent additionally; legitimate S/T breakpoints are not suppressed by benefit or hit-confirmation thresholds.
4. Register this C only when an actual provider attempt is submitted; repeated builds do not rotate, and retrying the same C does not evict the previous distinct position. Commit identity and generation checks guarantee idempotency.
5. Responses only update the usage and diagnostics of the original request. Success, failure, missing usage, late, and duplicate responses never confirm or advance any so-called effective frontier.
6. Tail records are cleared at the end of a turn; a new turn relocates T. compact, model or endpoint changes, and prefix rewrites invalidate the corresponding positions. Positions must not be retained on message_id alone.

The local state still needed is positions, prefix identity, generation, and commit identity. What is removed is the handoff controller that speculatively confirmed remote cache success, not request lifecycle and billing idempotency.

### Explicitly Accepted Failure Behavior

Mechanical rotation cannot guarantee preserving the farthest available cache. For example:

- Request 1 successfully writes C1.
- Request 2 carries P=C1, C=C2, reads C1, but C2 was not written successfully.
- Request 3 carries P=C2, C=C3; C1 has already been dropped; it may fall back to T even though the server still holds C1.

OpenAI explicit-only only looks at the explicit boundaries provided by the current request, so this counterexample holds. [Lookup rules](https://developers.openai.com/api/docs/guides/prompt-caching)

The new strategy has no "false confirmation" state, but it can still drop a valuable old position. After fallback, rewriting C may also overwrite an interval written previously, at which point the idealized proof's assumption of "continuous incremental reuse after one write" does not hold.

Here we accept the best-effort behavior of the two most recent tail opportunities plus S/T fallback, without re-adding ACK to compensate for unreliable servers. When encountering such a provider, the user can choose hybrid or implicit; tiers are not switched automatically based on hit rate. The community report of tool-result breakpoint failures is recorded as a known issue, and we cannot promise hybrid has fixed it: [report](https://community.openai.com/t/gpt-5-6-responses-api-breakpoint-on-function-call-output-is-accepted-but-never-writes-cache/1386415).

## 4. Module and Configuration Changes

- The capability layer lists available tiers, legitimate paths, capacity, and TTL based on endpoint/provider, model, and api_shape; capability declaration indicates protocol support and does not guarantee a hit on every runtime call. Unknown combinations only allow implicit; explicit writes are not enabled via broad model-name guessing.
- The strategy layer only selects structural anchors and eager tail; the adapter handles the actual fields for OpenAI Responses, Chat Completions, etc., exact injection, full marker auditing, and usage normalization. OpenRouter conversion fields are also included in auditing. [OpenRouter documentation](https://openrouter.ai/docs/guides/best-practices/prompt-caching)
- The endpoint's unified prepare_attempt entry, encoded spans, and turn policy snapshot are retained. Precheck and sending use the same resolved policy, including endpoint/base URL identity, to avoid divergent capability decisions in two places.
- The new `mode` takes precedence over old configuration; `enabled: false` retains the highest off priority. The old OpenAI explicit profile maps to the new eager explicit, old implicit maps to implicit, and old hybrid maps to S/T hybrid. Documentation clearly states this two-way behavior migration and does not claim the old explicit/hybrid behavior is preserved as-is. Anthropic's old configuration keeps its current semantics.
- Without an endpoint or model hook selection, the default is implicit. Explicitly selecting an unsupported tier reports a configuration error before the request. Reading compatibility with old configuration names is retained, but the second OpenAI R/ACK implementation is not kept permanently.
- Mode changes take effect on the next turn. Local state is isolated by session, endpoint, model, shape, and configuration generation; existing upstream keys remain compatible, and the new mode's keys do not vary by round or configuration generation.
- Each mode is built from encoded results that never injected cache control, clearing legacy control fields belonging to the adapter; content identity and marker auditing remain separate. Logs record the actual tier, breakpoints, and real usage; missing data stays unknown and never enters the LLM context.

## 5. Offline Acceptance and Implementation Order

Tests cover normal turns, single-round continuous conversation, multi-turn tool loops, compact, repeated builds, retries, out-of-order usage, mode switching, and prefix invalidation. Check the final payload's exact paths, marker sets, roles and content not rewritten by strategy, and no additional model requests.

An independent simulated provider verifies two kinds of outcomes: under ideal incremental caching, the previous tail can be read and the new interval written; when an injected C2 write failure occurs, the above fallback is reproduced while the client does not falsely claim confirmation. Simulated server state must not enter client selection conditions.

TLA+ is reduced to Build, Submit, Observe, BeginTurn, Invalidate: Build does not change tail records; Submit rotates idempotently by identity; Observe does not change markers; generation changes clean up invalid positions. It verifies breakpoint capacity, exact prefix validity, cross-generation isolation, and no additional requests, and no longer verifies "ACK attribution" or "F always valid." The old model is marked retired as a historical record.

The follow-up work will be implemented in three steps, each accompanied by relevant regressions:

1. Extract the capability and tier parsing, and wire up config compatibility and read-only diagnostics.
2. Integrate S/T hybrid and eager explicit, remove the OpenAI economics accumulation and estimation handoff code, keep generic billing idempotency; update the offline models at the same time.
3. Complete resident/Bunshin integration verification and migration documentation.

This plan has been implemented per subsequent instructions, with `pal llm add --replace --cache-mode` added as the configuration entry point. Running the configuration, activation, and rollout has not yet been done; the paid comparison will be arranged separately. For usage instructions, see [Cache Tiers](../../prompt_cache_modes.md).
