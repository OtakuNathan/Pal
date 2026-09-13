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
- tool, streaming, vision, and modality capabilities, including optional
  rejected generation fields in
  `capabilities_blob.unsupported_request_parameters`;
- ascending fallback priority and enabled state.

`/refresh_llm_endpoint` is the explicit reload boundary. It refreshes the
resident Core runtime and, when Bunshin's host broker runtime is already loaded,
refreshes that independent runtime in the same action. A cold Bunshin broker
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

An endpoint may declare optional generation parameters that its exact model
rejects:

```json
{"unsupported_request_parameters": ["temperature", "top_p"]}
```

The shape codec omits those fields without changing Pal's provider-neutral
generation policy. `pal llm add gpt-6-astra` installs the official OpenAI
Responses profile: `openai_response`, reasoning levels `low` through `max`,
vision/tool/stream support, and the unsupported sampling-parameter declaration.
The command does not activate the endpoint unless explicitly requested.
The OpenRouter model id `openai/gpt-6-astra` selects the equivalent
`openai_response` profile at `https://openrouter.ai/api/v1`. Its advertised
context remains 1,050,000 tokens, but operators may configure a 272,000-token
limit to stay below OpenRouter's higher long-context pricing tier.

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

## Thinking selection, validation, and wire encoding

`GenerationPolicyIR.thinking_selection` defaults to `configured`: an explicit
request level wins, otherwise the endpoint's persisted setting/default applies.
`lowest_supported` resolves against each actual endpoint during both preflight
and generation, using `off < minimal < low < medium < high < xhigh < max`.
It overrides inherited levels and clears manual budgets without writing settings.

### Configuration and errors

An endpoint declares supported levels and one default from that list. There is
no configurable effort mapping table. Pal normalizes whitespace/case, but does
not translate one level into another or fall back to `medium` during encoding.

| Wire shape | Accepted Pal configuration vocabulary | Non-off wire field |
| --- | --- | --- |
| `openai_completion` | `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` | `reasoning_effort` |
| `openai_response` | `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` | `reasoning.effort` |
| `anthropic_messages` | `off`, `low`, `medium`, `high`, `xhigh`, `max` | `output_config.effort` |

Every non-off value is transmitted unchanged. This table describes Pal's
accepted vocabulary, not a guarantee that every remote model supports every
listed level. Configure only the subset supported by the actual endpoint.

`pal llm add`, `--replace`, setup, and repository writes share validation.
An invalid or empty level list reports the shape's accepted choices; an invalid
default reports the endpoint's declared choices. `/think` and explicit runtime
request levels are likewise checked against that endpoint's list. For example:

```text
invalid thinking level: 'ultra'; available for openai_response: off, minimal, low, medium, high, xhigh, max
```

For an existing endpoint known to support `low`, `medium`, and `high`:

```sh
pal llm add my-endpoint --replace --thinking-levels low,medium,high --default-thinking-level low --runtime-root /path/to/runtime
```

Invalid thinking declarations are rejected before endpoint, credential, or active
selection writes. Setup validates all collected thinking declarations before
seeding, and interactive prompts ask again rather than silently dropping unknown
levels or substituting a default. Existing legal declarations are preserved;
historical invalid declarations require explicit correction, not runtime aliases.
After an endpoint configuration edit, use `/refresh_llm_endpoint` to load it.
Changing resident codec/runtime Python code requires a full external host restart.

### Thinking switches and manual budgets

`off` is Pal's thinking switch, not an effort keyword. Anthropic explicitly sends
`thinking: {"type": "disabled"}` and no effort. The OpenAI codecs omit their
reasoning field for `off`; omission does not itself guarantee that a remote
service with reasoning enabled by default will disable it.

Anthropic non-off requests without a manual budget send `thinking.type=adaptive`
and the exact effort. Explicit `thinking_budget_tokens` requests send
`thinking.type=enabled`, the exact budget, and the exact effort. The budget must
be an integer (not a bool), at least 1024 and below the effective output cap after
endpoint limiting. `off` with a manual budget is invalid. Invalid combinations
fail preparation rather than being clamped or discarded. Other shapes reject
explicit manual budgets. Pal does not enable the interleaved-thinking beta
budget exception. This budget is a request IR field, not a `pal llm add` option.

Compact uses `lowest_supported`, clears inherited manual budgets, and never
converts an effort level into a synthetic token budget. Core chooses its total
output allowance separately; see [the compact contract](pal_memory_contract.md#compact).

Effort is not a hard token cap. Compatible services can ignore thinking budgets
(DeepSeek documents this); total output is constrained by `max_tokens`.
Changing thinking/effort may invalidate provider prompt caching even when the
replayed messages are unchanged. Cache usage must be measured from responses.

References: [Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort),
[Anthropic manual thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking),
[DeepSeek Anthropic compatibility](https://api-docs.deepseek.com/guides/anthropic_api/).
