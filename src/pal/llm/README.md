# llm

Owns:
- canonical llm request and outcome shapes
- endpoint resolution
- provider runtime boundary
- provider-declared thinking choices and per-endpoint selection
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
- canonical llm contracts
- `EndpointResolver`
- `LLMRuntime`
- `/status` for active-model, request, token, cache-hit, reasoning-token, and
  provider-reported cost statistics
- compaction uses the standard `agenerate` contract with tools and generic
  max-output continuation disabled by Core
