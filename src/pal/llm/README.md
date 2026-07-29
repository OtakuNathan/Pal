# llm

Owns:
- canonical llm request and outcome shapes
- endpoint resolution
- provider runtime boundary
- provider-declared thinking choices and per-endpoint selection
- provider-neutral, resident-process usage accounting

Does not own:
- durable state other than endpoint registry
- durable token, cache, or cost history
- local side effects
- tasking state
- scheduler state

Exposes:
- canonical llm contracts
- `EndpointResolver`
- `LLMRuntime`
- `/status` for active-model, request, token, cache-hit, reasoning-token, and
  provider-reported cost statistics
