# Pal LLM Contract

The `llm` subsystem is a provider-neutral input/output boundary. Pal owns one
immutable IR; endpoint codecs alone translate between that IR and provider
wire JSON.

## Ownership

`llm` owns:

- `LLMRequestIR`, `LLMMessageIR`, content parts, `LLMResponseIR`, usage, and
  stream updates;
- endpoint selection, retry, fallback, timeout, and output-limit recovery;
- exactly three wire shapes: `openai_completion`, `openai_response`, and
  `anthropic_messages`;
- JSON-frame normalization for streaming and single-shot SDK responses;
- exact-model request hooks;
- built-in provider response-syntax hooks after codec decoding.

It does not own durable conversation policy, tool execution, capability
governance, memory ranking, or channel delivery.

All provider-neutral tool protocol values live in
`pal.shared.tool_protocol`: `ToolDefinitionIR`, `ToolCallIR`, `ToolResultIR`,
the tagged invocation result, and `ToolExecutionResult`. The LLM package only
embeds calls/results as message parts and translates them to provider shapes;
it does not define or privately wrap tool protocol values.

## L1 and turn settlement

L1 stores `LLMMessageIR`, not provider dictionaries. During the active logical
turn it retains text, reasoning parts, tool calls/results, message state, and an
optional exact-endpoint replay envelope. The turn is one atomic protocol unit.

- Every tool result must consume a known, pending call ID exactly once.
- Incomplete JSON tool drafts never become `ToolCallIR` and are never
  executable.
- A structured received tool call without an explicit call ID is ill-formed
  and ignored. A provider text-protocol normalizer may generate an internal ID
  only while constructing the first `ToolCallIR` at that Pal-owned boundary.
- A `length` terminal discards all tool-call intent from that response.
- `settle`, `interrupt`, and `abort` close the turn atomically.
- Closing retires reasoning parts and provider replay data.
- Interrupt/abort also remove unresolved tool calls; late results are rejected.
- Compaction freezes L1 and uses that snapshot as its sole truth source.

Historical prompt projections may shorten old tool-result text to fit an input
budget, but they never mutate L1.

## Endpoint registry

Each `llm_endpoints` row declares:

- stable endpoint, provider-display, and exact model IDs;
- `wire_shape`;
- base URL and credential reference;
- context and output limits;
- an explicit `thinking_levels_blob` enum and a
  `default_thinking_level` contained in that enum;
- tool, streaming, vision, and modality capabilities;
- ascending fallback priority and enabled state.

`/refresh_llm_endpoint` is the explicit reload boundary. It refreshes the
resident Core runtime and, when Minion's host broker runtime is already loaded,
refreshes that independent runtime in the same action. A cold Minion broker
loads the refreshed registry on its first request. Runtime statistics remain
separate because the two runtimes have distinct lifecycle and accounting.

`provider` is credential/display/telemetry identity. It never selects a codec
or changes request semantics. It may select a built-in response-syntax
normalizer when a provider leaks its textual model protocol through a standard
wire shape. The old `api_mode` and `supports_reasoning` columns are migrated
once and then removed.

## Wire codecs and SDKs

Endpoint selection chooses a codec solely by `wire_shape`:

1. `openai_completion` uses the OpenAI SDK chat-completions API.
2. `openai_response` uses the OpenAI SDK Responses API.
3. `anthropic_messages` uses the Anthropic SDK Messages API.

Both streaming and single-shot transports expose one input iterator of JSON
frames to the selected codec. The codec consumes that iterator and lazily
yields `LLMResponseUpdate`; its decoder state and wire events are private. Raw
SDK objects, provider chunks, and decoder events never enter Core, Channel, or
L1.

Tool schemas are represented once as `ToolDefinitionIR.input_schema`. Codecs
render that schema under the provider's required wire field. Tool execution
always returns to local `Execution`.

## Streaming and output recovery

Codecs accumulate partial text, reasoning, usage, and private tool drafts.
Only a successful terminal frame can promote a complete tool draft. EOF without
a terminal state is an error.

Core projects semantic response updates to `ChannelStreamUpdate` only when a
channel supports incremental display. This is a channel delivery contract, not
an LLM wire or codec event.

When a response ends at an output limit, the shared runtime can continue it in
place. Partial text/reasoning is merged into one assistant message, the original
message ID is retained, and only complete tool calls from the final successful
continuation are exposed. Recovery is bounded by endpoint/configured attempts.

## Thinking levels

Pal has a closed `ThinkingLevel` enum. Every endpoint stores the subset it
supports and its default. Preflight validates a requested level against that
endpoint before encoding; codecs map the validated value to the wire shape.
Unsupported values do not silently degrade.

## Exact-model hooks

Optional hooks live at:

```text
<runtime_root>/llm/models/**/*.py
```

Each exports one `MODEL_HOOK` for one exact `model_id`. A hook may insert
model-specific developer instructions and may replace only the immutable
message or tool-definition tuples. The generation policy, endpoint, provider,
credential, shape, routing metadata, and every other request field are
read-only to hooks. Hooks perform no I/O. Duplicate model IDs fail loading.

## Provider response hooks

Provider response hooks run after the selected wire codec and before any
response update reaches Core, L1, or Channel. They may only transform response
updates into the same immutable IR. They cannot change requests, endpoint
routing, credentials, generation policy, or codec selection.

The built-in `deepseek` hook recognizes DeepSeek's textual DSML tool protocol.
It is an incremental decorator over the codec-owned update iterator: ordinary
content streams immediately, while a small cross-chunk prefix gate retains any
possible DSML tag. Native structured tool calls pass through unchanged and
take precedence over textual mirrors. A complete DSML response with no native
call is parsed into reasoning/text/tool-call parts and assigned internal call
IDs at this Pal-owned adapter boundary. Raw DSML and echoed historical
tool-projection markers are discarded. Malformed, unsuccessful, filtered, or
unterminated DSML fails the provider attempt and follows normal bounded
retry/fallback handling. A length-truncated DSML block stays hidden while
output recovery joins its continuation, then the complete response passes
through the same hook instance again.

## Invariants

- IR is the only internal LLM contract.
- L1 is the only active-turn and compaction truth source.
- Provider data is confined to an active replay envelope and retired on close.
- Every executable tool call has complete parsed object arguments.
- Tool calls from truncated or unterminated responses are never executable.
- Endpoint schema and thinking enums are locally validated before a request.
- The persisted endpoint row is the sole source of supported thinking values;
  preflight evaluates the fully hooked request.
- A missing or rejected credential fails that endpoint as one unit and moves to
  the next endpoint; credentials are never borrowed across endpoints.
- `finish_reason=error`, including output-recovery errors, is a failed provider
  attempt and never updates successful health or usage state.
- Request/model quirks are exact-model hooks. Provider-wide branching is
  limited to response-syntax normalization and cannot alter behavior policy.
