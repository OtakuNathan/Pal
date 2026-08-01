# llm

Owns:
- immutable provider-neutral LLM request, message, response, usage, and update IR
- endpoint resolution
- three wire-shape codecs and OpenAI/Anthropic SDK transport boundaries
- endpoint-declared thinking enums and per-endpoint selection
- exact-model request hooks loaded from `<runtime_root>/llm/models`
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
- provider is credential/display/telemetry identity, never a behavior switch
- the validated endpoint row is the only thinking-level truth source
- exact-model hooks can modify only messages and tool definitions
- SDK clients are reused per endpoint and retired safely when the active endpoint changes
- credentials are endpoint-local; a missing/rejected key falls back the whole endpoint
- incomplete or length-truncated tool drafts are never executable
- streaming and single-shot responses pass through the same codec-owned JSON-frame iterator
- decoder state and provider stream events never escape the codec
- closed L1 turns contain neither reasoning parts nor replay envelopes
