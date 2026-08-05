# llm

Owns:
- immutable provider-neutral LLM request, message, response, usage, and update IR
- endpoint resolution
- three wire-shape codecs and OpenAI/Anthropic SDK transport boundaries
- endpoint-declared thinking enums and per-endpoint selection
- exact-model request hooks loaded from `<runtime_root>/llm/models`
- built-in provider response hooks that recover leaked provider text protocols
  before Core, L1, or Channel can observe them
- provider-neutral, resident-process usage accounting
- endpoint retry, fallback, timeout, preflight, and usage accounting reused by
  ordinary and compaction requests

Does not own:
- durable state other than endpoint registry
- durable token, cache, or cost history
- local side effects
- tasking state
- scheduler state
- Pal/Minion compaction prompts, schemas, validators, or renderers

Exposes:
- `LLMRequestIR`, `LLMMessageIR`, `LLMResponseIR`, and shape codecs
- `EndpointResolver`
- `LLMRuntime`
- `pal llm list`, `pal llm add`, and `pal llm delete` for endpoint administration without direct
  database edits
- `/status` for active-model, request, token, cache-hit, reasoning-token, and
  provider-reported cost statistics
- compaction uses the standard `agenerate` contract with tools and generic
  max-output continuation disabled by Core

Invariants:
- provider is credential/display/telemetry identity and may select only a
  response-syntax normalizer; it never selects request semantics or a codec
- the validated endpoint row is the only thinking-level truth source
- exact-model hooks can modify only messages and tool definitions
- provider response hooks decorate the codec-owned response-update iterator
  and can only normalize updates into the same immutable IR; malformed
  provider protocols are endpoint failures
- the DeepSeek decorator streams ordinary text but retains possible DSML tag
  prefixes across chunks, so textual DSML can never escape as a channel delta
- only successful terminal DeepSeek responses can promote complete DSML to a
  tool call; native structured calls retain precedence and provider call IDs
- SDK clients are reused per endpoint and retired safely when the active endpoint changes
- credentials are endpoint-local; a missing/rejected key falls back the whole endpoint
- provider-confirmed item boundaries commit immutable response items into IR;
  open tool drafts are never executable, while a committed tool item survives
  a later length terminal and is executed once through Core's normal tool effect
  path after the response iterator closes
- streaming and single-shot responses pass through the same codec-owned JSON-frame iterator
- decoder state and provider stream events never escape the codec
- closed L1 turns contain neither reasoning parts nor replay envelopes
