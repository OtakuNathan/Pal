# llm

Owns:
- `/think`, `/model`, `/refresh_llm_endpoint`, their button callbacks and presentation
- model-selection validation and endpoint-refresh effects; Core supplies context-change coordination
- shared semantic/native compaction execution, checkpoint prompts, schemas and validators
- one asynchronous semantic response stream for complete and streamed responses
- immutable provider-neutral LLM request, message, response, usage, and update IR
- ChatGPT plan OAuth registration, renewable credentials, and explicit usage-pause recovery
- explicit endpoint selection; no automatic fallback or implicit first endpoint
- independent wire-shape codecs, endpoint authentication, and configured
  HTTP SDK or persistent Responses WebSocket transports
- immutable endpoint compositions built from validated LLM configuration
- endpoint-declared thinking enums and per-endpoint selection
- exact-model request hooks loaded from `<runtime_root>/llm/models`
- provider response defaults and endpoint response differences loaded from
  `<runtime_root>/llm/providers` and `<runtime_root>/llm/endpoints`
- DeepSeek-only recovery of leaked DSML body envelopes before Core, L1, or
  Channel can observe executable tool calls
- provider-neutral, resident-process usage accounting
- endpoint retry, timeout, preflight, and usage accounting reused by
  ordinary and compaction requests

Projection sessions are process-local derived state. Recovery restores L1,
including native replay envelopes, then rebuilds the projection.

Does not own:
- durable conversation state (endpoint configuration and protected OAuth sessions are LLM-owned)
- durable token, cache, or cost history
- local side effects
- tasking state
- scheduler state

Exposes:
- `LLMRequestIR`, `LLMMessageIR`, `LLMResponseIR`, and shape codecs
- `EndpointResolver`
- `LLMRuntime`
- `pal llm list`, `pal llm add`, and `pal llm delete` for endpoint administration without direct
  database edits
- `/status` for active-model, request, token, cache-hit, reasoning-token, and
  provider-reported cost statistics
- semantic compaction consumes the same generation stream; bound native strategies use the same endpoint
  auth/transport lifecycle for standalone compact and exact input-token counting
- model adapters bind request hooks, response normalizers and compaction policy;
  see [model compaction](../../../docs/llm_compaction.md)

Invariants:
- a persisted endpoint is uniquely identified by its lowercase access-provider slug and exact upstream model ID; `endpoint_id` is derived as `{provider}-{model_id}`. Display labels and wire shapes are not identity.
- provider is credential/display/telemetry identity and may select only a
  response-syntax normalizer; it never selects request semantics or a codec
- the validated endpoint row is the only thinking-level truth source
- Resident thinking requests are frozen at turn admission. The first turn in
  a prefix pins its root effort in L1; documented/verified effort-update
  routes append a native update before the next user message. Other routes
  retain their root until a committed compact (not promotion or expiration).
  Tool rounds, retries, and keepalive never choose a new effort. Native
  standalone compact omits update items from its submitted copy and restores
  the captured effective effort alongside the returned window.
- non-off effort values are transmitted unchanged in each shape's wire field;
  there is no effort mapping table or codec fallback to another level
- configuration errors list shape-accepted levels; invalid defaults and runtime
  selections list the endpoint's declared choices
- `lowest_supported` resolves after endpoint selection, clears inherited manual
  thinking budgets, and leaves the ordinary conversation's selection unchanged
- endpoint-declared unsupported request parameters are omitted by codecs while
  the provider-neutral generation policy remains unchanged
- request contexts bind exact-model sampling defaults for native and `openai/`
  model IDs, independently of provider or transport. Explicit `sampling_policy`
  overrides the default; projected requests share the final parameter guard
- exact-model hooks can modify only messages and tool definitions
- codecs classify reasoning, body text and native tool items before any
  provider-specific normalization; reasoning and body examples are not tool calls
- codecs split native envelopes into non-executable response blocks, retaining
  tool input as raw JSON/string values until protocol adaptation is complete
- endpoint assembly binds provider defaults followed by exact-endpoint hooks;
  the common response decoder runs after both layers and alone creates ToolCallIR
- Runtime consumes the semantic `LLMEndpointInvokerPort`; lifecycle, projection
  receipts and attempt accounting use `ManagedEndpointInvokerPort`, independent
  of the concrete invoker. Custom semantic invokers return already-adapted IR;
  Runtime never converts their output back to blocks or re-runs protocol hooks
- endpoint cache capabilities bind before policy execution. Explicit
  `prompt_cache.protocol` overrides compatibility presets; unknown endpoints
  use `general`. Cache planning consumes bound capabilities rather than
  inferring them from provider identity, URL or model names
- built-in DSML recovery is exclusive to the DeepSeek provider hook; it never
  runs on other provider identities or inside reasoning
- the DeepSeek decorator streams ordinary text and examples but retains possible
  top-level DSML tool envelope prefixes across chunks, outside Markdown examples
- only successful terminal DeepSeek responses can promote complete DSML to a
  tool call; native structured calls retain precedence and provider call IDs
- SDK clients are reused per endpoint and retired safely when the active endpoint changes
- `wire_shape`, `auth_kind`/access profile and `transport` are independent node
  configuration. Load/refresh compiles an `LLMEndpoint` with a fixed codec,
  authentication provider, transport and both response-hook layers. Requests execute that composition;
  access profile never chooses HTTP versus WebSocket. Replaced compositions
  reject new admission and close after their in-flight requests finish.
- `http_transport` owns SDK clients and request/response lifetimes;
  `websocket_transport` owns persistent Responses sockets. Sequential socket
  requests can reference accepted output on the same socket using `previous_response_id`.
  Native Gemini Interactions uses `previous_interaction_id` over HTTP. Both paths
  retain full local replay and verify the exact input prefix for each logical scope
  and endpoint generation. Set `continuation_mode=full` to force full replay and
  disable Gemini interaction storage. Cache diagnostics record the provider's cached
  input tokens separately from the transmitted byte count and first-frame latency.
  Semantic compaction may continue a matching frozen left prefix on its source model.
  Following a model switch, the destination starts with the committed checkpoint.
  Concurrent WebSocket requests hold
  exclusive sockets. Cancellation, uncertain writes, failed responses, token
  rotation and endpoint changes retire affected sockets through fd leases.
  Shared request, cancellation and transport-error types live in
  `transport_contracts`; `transport` preserves their existing import entry points.
- WebSocket never automatically replays an ambiguous write or interrupted
  response; a fresh connection is prepared for a subsequent request instead
- API credentials are endpoint-local; requests retry only the selected endpoint
- ChatGPT plan credentials may be shared by endpoints; subscription requests never
  fall back automatically, and a usage pause requires explicit user recovery
- provider-confirmed item boundaries commit immutable response items into IR;
  open tool drafts are never executable, while a committed tool item survives
  a later length terminal and is executed once through Core's normal tool effect
  path after the response iterator closes
- streaming and single-shot responses pass through the same codec-owned JSON-frame iterator
- failed provider attempts retain bounded private response frames, exception
  chains and the owner's pre-submission R snapshot, when available; successful
  attempts and cancellation do not write failure captures
- LLM failure replies contain the public cause and applicable recovery guidance.
  Raw exception text, provider error envelopes and attempt metadata stay private;
  known error-envelope text never streams as assistant output
- decoder state and provider stream events remain private to LLM; only validated
  response IR crosses into Core, L1 and Channel
- closed L1 turns preserve reasoning parts and replay envelopes until explicit compaction
- transport admission and accepted output are separate owner-side lifecycle events
- L1 is the replay authority; frozen projections can be rebuilt without native loss
- projection send receipts contain no response payload. Native projection
  material derives from the accepted message's replay, never from independent
  pre-hook capture; tool ids, names and arguments must agree with accepted IR

The [LLM contract](../../../docs/pal_llm_contract.md#thinking-selection-validation-and-wire-encoding)
defines accepted configuration vocabulary, per-shape `off` behavior, manual
budget validation, and configuration/restart boundaries. LLM owns compact's
visible summary limit and total output allowance; see the
[compact contract](../../../docs/pal_memory_contract.md#compact).

Model-facing inspection uses one indirect `inspect_llm()` call without arguments.
It returns all loaded enabled endpoint capabilities with `(active)` on the selected
label, current thinking and process usage summaries. Configuration and human
status controls keep their existing ownership; see the LLM contract for accounting
scope, unavailable-state handling and activation boundaries.

`snapshot_turn_settings()` returns an immutable `LLMTurnSettings` for the current
thinking choices and cache policies. Core and Bunshin consume the same snapshot;
refresh errors propagate instead of silently selecting empty settings.
`resolve_output_budget()` owns endpoint limits and the configured default,
including a caller's explicit requested budget.

Cache keep-alive retains one process-local resident wire request after a
successful explicit-cache generation. The bound codec constructs an isolated
bounded-output request without changing effort or marker positions. Provider
usage renews the matching reminder epoch only on a complete read with no
writes; keepalive attempts never advance response lineage, change history, or
contribute to the normal cache ratio. The scheduler remains plugin-owned.

## Endpoint identity cutover

New configurations use `pal llm add --provider PROVIDER --model-id MODEL`.
`--replace` updates that pair. ChatGPT OAuth uses provider `chatgpt`, not
`openai`; upstream namespaces such as `openai/gpt-6.2-sol` remain intact.

For legacy databases, first run `pal llm migrate-ids --dry-run --runtime-root ROOT`.
Each duplicate requires `--keep OLD_ID`; the chosen route's settings win.
The same repeatable `--keep OLD_ID` flag works with `pal setup --upgrade`.
Run the same command without `--dry-run` only after the external host and all
Bunshin workers have stopped. It backs up changed databases and configuration,
updates references, and keeps resident and role recovery journals unchanged.
Interrupted migrations block startup and restore the backup on offline retry.
A pure rename retains verified native replay aliases but starts a new local
cache identity. Incompatible route changes require a source semantic handoff.
