# llm

Owns:
- canonical llm request and outcome shapes
- endpoint resolution
- provider runtime boundary

Does not own:
- durable state other than endpoint registry
- local side effects
- tasking state
- scheduler state

Exposes:
- canonical llm contracts
- `EndpointResolver`
- `LLMRuntime`
