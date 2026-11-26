# Model adapters and compaction

Endpoint assembly binds an immutable model adapter containing the exact-model
request hook, provider/endpoint response normalizers, and compaction strategy.
Model and response module revisions participate in projection identity. Existing
`MODEL_HOOK`, `MODEL_ID`, and `normalize_blocks` modules remain supported.
`<runtime_root>/llm/models/*.py` can instead export `MODEL_ADAPTER`, an instance of
`pal.llm.model_adapter.ModelAdapter`, or declare `COMPACTION_STRATEGY` alongside
the legacy exports. Model hooks still cannot change generation policy or routing.

An endpoint can override the model default with
`capabilities_blob.compaction_strategy`, exposed by `pal llm add
--compaction-strategy`. Values are `auto`, `semantic_checkpoint`, and
`openai_responses`. An unsupported explicit native combination fails endpoint
assembly. `auto` uses the researched allowlist below; an unrecognized model or
route keeps the existing semantic checkpoint behavior.

## Model and route audit

New model IDs require a capability audit;
a shared vendor or OpenAI-compatible wire format is not proof of support.

| Configured model | Native compact route | Other configured routes |
| --- | --- | --- |
| `gpt-6.2-astra`, `gpt-6.2-sol` | Official OpenAI HTTP Responses, API-key authentication | OpenRouter uses semantic checkpoints |
| `gpt-6-astra` | Official OpenAI HTTP Responses, API-key authentication | ChatGPT OAuth/WebSocket and OpenRouter use semantic checkpoints |
| `gpt-6-luna` | Official OpenAI HTTP Responses, API-key authentication | ChatGPT OAuth/WebSocket and OpenRouter use semantic checkpoints |
| `gpt-6.1-sol` | Official OpenAI HTTP Responses, API-key authentication | ChatGPT OAuth/WebSocket and OpenRouter use semantic checkpoints |
| `gpt-6.1-terra` | Official OpenAI HTTP Responses, API-key authentication | OpenRouter uses semantic checkpoints |
| `gpt-6-sol` | Supported on the verified official API route | Its configured OpenRouter route uses semantic checkpoints |
| `glm-5.3`, `glm-5.3-flash` | No native compact API established | Preserve configured thinking behavior; semantic checkpoints |
| `deepseek-v4.1-pro`, `deepseek-flash` | No compatible native compact API established | Anthropic-compatible route; semantic checkpoints |
| `gemini-3.8-flash`, `gemini-4-argon` | No native compact API established for the configured route | OpenAI Chat Completions-compatible route; semantic checkpoints |

OpenAI documents the complete returned window as the next canonical input:
[compaction guide](https://developers.openai.com/api/docs/guides/compaction),
[standalone API](https://developers.openai.com/api/reference/resources/responses/methods/compact).
The [preserved reasoning guide](https://developers.openai.com/api/docs/guides/preserved-reasoning)
explains why replacing a prefix can invalidate downstream reasoning. Pal does not
send custom compact instructions, including on future models whose reasoning
preservation behavior depends on their absence.

The other providers document reasoning continuity, without establishing support
for this standalone compact contract on the configured routes:
[Z.ai thinking](https://docs.z.ai/guides/capabilities/thinking-mode),
[BigModel thinking](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode),
[DeepSeek Anthropic compatibility](https://api-docs.deepseek.com/guides/anthropic_api),
[Gemini thought signatures](https://ai.google.dev/gemini-api/docs/thought-signatures),
[OpenRouter Responses](https://openrouter.ai/docs/api/reference/responses/overview).
The stored DeepSeek `deepseek-v4.1-pro` ID was not renamed: published model naming
did not establish that this configured alias should be replaced.

## When Pal requests compaction

Pal admits input up to the smallest of the configured compact watermark
(`--context-window`), the standard-price input ceiling where known, and the
physical context window less output and safety reserves. Equality fits.
For official GPT-6.2 Sol, a 320,384 watermark and a 1,050,000 physical
window both permit the full 272,000 standard-price input tokens. The output
reserve is capped at 65,535 per call, including recovery calls. A smaller
deployment can set `--physical-context-window`. For routes without a known or
declared physical capacity, Pal conservatively uses the configured window as
the capacity as well.

The standard-price policy covers the listed official OpenAI models at 272,000,
official Gemini 3.1 Pro Preview at 200,000, and official Claude Haiku 5.5 at
100,000. On the OpenRouter API it covers the exact OpenAI IDs for GPT-6.2
Sol/Astra, GPT-6 Sol/Luna/Astra and GPT-6.1 Sol/Terra at 272,000. Compatibility endpoints
use local estimates, with extra headroom on a known price tier. An endpoint
can opt into premium input via `pal llm add --allow-long-context-pricing`.
The provider's separate input maximum still applies, including the 922,000 cap
on GPT-6.2 Sol and Astra. See the [Sol model specification](https://developers.openai.com/api/docs/models/gpt-6.2-sol),
[OpenRouter endpoint pricing overrides](https://openrouter.ai/api/v1/models/openai/gpt-6.2-sol/endpoints),
[Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing) and
[Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing).

For a matching conversation prefix and endpoint, Pal starts with the preceding
response's normalized input plus output usage and estimates the uncovered
suffix. A changed prefix or tool schema starts a fresh estimate. Close to a
limit, first-party API routes count the complete encoded input, including tools.
Official OpenAI API-key Responses endpoints use the HTTPS input-token counter
for both HTTP and WebSocket generation. The count shares cancellation with the
generation and has its own submission state. Gemini counts only requests that
can be represented by its countTokens API.
An unavailable counter uses a conservative local estimate. Pal requires an
exact count for opaque native history without a matching usage baseline.

## Gemini Interactions and Responses WebSocket continuation

Pal retains the full accepted history, including Gemini thought signatures and
function calls. With `--continuation-mode auto`, Gemini Interactions can send a
matching `previous_interaction_id` and the remaining input. Responses WebSocket
can send `previous_response_id` when the leased connection still holds that
response. A replacement endpoint generation or process restart starts with full replay.
`--continuation-mode full` also disables Gemini interaction storage.

For a semantic checkpoint, Memory freezes L before the request is assembled.
The source endpoint can reuse an accepted response whose complete input and
output match a prefix of that L. The checkpoint instruction and any remaining
L items follow the ID. A response covering part of R is ineligible. A model
switch can use this path on the source model, then start the destination model
from the portable checkpoint. Preflight still budgets the full context.

Both full and incremental requests can hit implicit prompt caches. Pal records
the actual transmitted size and the provider's reported cache reads separately.
[Gemini caching](https://ai.google.dev/gemini-api/docs/caching)
and [Responses WebSocket mode](https://developers.openai.com/api/docs/guides/websocket-mode).

## History, failure, and switching contracts

Semantic checkpoints keep the existing prompt, validation, retries, and memory
candidate policy. Native compaction uses `/responses/compact` through Pal's
endpoint authentication, SDK transport, cancellation, timeout, and usage owners.
There is no direct credential path and no fallback to another endpoint or strategy
after a native attempt fails. Automatic server-side compaction and configuration
update items are outside this implementation.

Native capture consists of L plus a continuous, accepted, protocol-complete R
prefix. If including the completed tail would exceed the standard price tier,
Pal counts and compacts the original L while keeping R for the successor.
An already oversized L may require one premium attempt within physical capacity.
Unanswered inputs, incomplete assistant output, and unpaired tool calls
stay outside it. Capturing records a prospective cut; failure and cancellation
leave the live cut and history untouched. Commit validates the entire captured
prefix and atomically replaces it, retaining the remaining R and active turn ID.
The existing manual compact and model-switch idle gates still apply.

One candidate contract carries either a semantic checkpoint or a native window.
Native windows preserve every output item, its order, unknown fields, and opaque
content in L1 replay. They are historical input, never executable tool effects.
Encrypted resident persistence restores that same window. Memory schema 5 reads
legacy schemas 1–4; the version bump prevents older code from silently discarding
a replay-only seed. Ordinary requests,
recompaction, and semantic handoff use the same canonical codec path. Covered
preamble is not prepended again. Native windows use that codec directly instead
of the incremental derived projection cache.

Native input budgets use the
[input token count API](https://developers.openai.com/api/reference/resources/responses/subresources/input_tokens/methods/count),
cached by endpoint configuration and effective input digest. Ciphertext length
is never treated as token count. Before install, the successor input includes
the pending tail, prompt shell, and tools in its token check. A counted upper
bound is retained for read-only switch advice. Exact checks count the effective
request exactly.

Native compaction does not make an extra text-generation call or produce memory
candidates. It conservatively retains source file-result snapshot references,
including references inherited from earlier native windows. A later semantic
handoff can select the references still needed; reset and normal history ownership
continue to govern cleanup.

Cross-model or replay-incompatible switches compact on the old endpoint using
the semantic policy, then apply the new selection. Compatible same-model routes
avoid that call unless the target budget requires it. Empty histories and already
portable text-only seeds need no new handoff. Native origin mismatch after
configuration changes or recovery fails before sending the opaque window to an
incompatible model. Passive settings refresh retains the admitted source model;
external selection changes must pass through the idle model-switch control path
or take effect on an external host restart. If the handoff commits but selection fails, the text seed
remains committed and the old model remains selected, as in the existing control
transaction.

## Validation and activation

`tests/test_model_compaction_strategies.py` covers strategy assembly, canonical
replay, exact counting, recovery, text handoff, prospective capture and failure.
`spec/llm/RoundSubmission.tla` includes native capture and a negative mutant that
illegally advances the cut before commit.

For a small paid check with existing Pal credentials:

```sh
python scripts/verify_native_compaction.py --runtime-root /path/to/runtime \
  --endpoint openai-gpt-6-luna
```

This uses isolated synthetic history and in-memory model selection. It does not
change the resident endpoint or conversation. The checkout changes affect resident
LLM/Core/Memory code and require an external host restart for activation; plugin
reload alone is insufficient. Never restart Pal's host from its own active turn.

Native OpenAI compaction uses the visible closed input prefix and the same
explicit chase-tail selector as ordinary generation when configured. The compact
transport includes cache controls and the routing key via its own schema; it
never adds generation tools or ephemeral suffix instructions. Cache telemetry
for this operation is separate, and cross-operation cache reuse is not assumed.
