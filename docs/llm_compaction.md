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

The configured models were checked against official documentation on 2026-11-11.
No GPT-6.2 endpoint was configured. New model IDs require a fresh capability audit;
a shared vendor or OpenAI-compatible wire format is not proof of support.

| Configured model | Native compact route | Other configured routes |
| --- | --- | --- |
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

## History, failure, and switching contracts

Semantic checkpoints keep the existing prompt, validation, retries, and memory
candidate policy. Native compaction uses `/responses/compact` through Pal's
endpoint authentication, SDK transport, cancellation, timeout, and usage owners.
There is no direct credential path and no fallback to another endpoint or strategy
after a native attempt fails. Automatic server-side compaction and configuration
update items are outside this implementation.

Native capture consists of L plus a continuous, accepted, protocol-complete R
prefix. Unanswered inputs, incomplete assistant output, and unpaired tool calls
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
bound is retained for read-only switch advice; generation counts the effective
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
