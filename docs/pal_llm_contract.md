# Pal LLM Contract

The `llm` subsystem is a provider-neutral input/output boundary. Pal owns one
immutable IR. Endpoint codecs encode requests and split response envelopes;
bound protocol hooks adapt response blocks before common semantic decoding.

## Ownership

`llm` owns:

- `LLMRequestIR`, `LLMMessageIR`, content parts, `LLMResponseIR`, usage, and
  stream updates;
- explicit endpoint selection, retry, timeout, and output-limit recovery;
- exactly three wire shapes: `openai_completion`, `openai_response`, and
  `anthropic_messages`;
- JSON-frame normalization for streaming and single-shot SDK responses;
- exact-model request hooks;
- provider defaults and endpoint response hooks between envelope splitting and
  common semantic decoding.

It does not own durable conversation policy, tool execution, capability
governance, memory ranking, or channel delivery.

All provider-neutral tool protocol values live in
`pal.shared.tool_protocol`: `ToolDefinitionIR`, `ToolCallIR`, `ToolResultIR`,
the tagged invocation result, and `ToolExecutionResult`. The LLM package only
embeds calls/results as message parts and translates them to provider shapes;
it does not define or privately wrap tool protocol values.
Pre-semantic response blocks carry undecoded wire values, are not executable
tool protocol objects, and remain inside the LLM boundary.

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
- A `length` terminal discards uncommitted tool drafts; already committed items survive.
- `settle`, `interrupt`, and `abort` close the turn atomically.
- Closing retains reasoning parts and provider replay data unchanged.
- Interrupt/abort also remove unresolved tool calls; late results are rejected.
- Compaction freezes an L snapshot as its source; R remains appendable.

Historical prompt projections retain accepted result text and native continuation
values. Context reduction happens through explicit left-segment compaction, not
reasoning stripping or result truncation. Object key ordering may differ; string
values, array order, absent fields and explicit nulls must round-trip unchanged.

After preflight and any conditional left compaction, a request captures the
closed R input prefix. Successful response admission freezes exactly that
captured input into L, independently of output
completion or cache hits. Failed admission leaves the cut unchanged. Output
and subsequently arriving results remain in R. Manual compact may first archive
closed turns; automatic compact never promotes the pending active tail. Custom
invokers without admission notifications conservatively leave input in R.

Replay envelopes belong to L1, including optional original `source_payload` for
provider-specific protocol normalization. Projection caches are disposable and are not persisted separately. Restart
restores canonical L1 and builds a fresh projection; there is no independent
projection snapshot/restore API.
Model switching never automatically invokes compact; incompatible opaque replay
requires compact on the previous model or reset.

## Endpoint registry

Each `llm_endpoints` row declares:

- stable endpoint, provider-display, and exact model IDs;
- `wire_shape`;
- `transport`: `http` or `websocket`, independent of authentication;
- base URL and credential reference;
- context and output limits;
- an explicit `thinking_levels_blob` enum and a
  `default_thinking_level` contained in that enum;
- tool, streaming, vision, and modality capabilities, including optional
  rejected generation fields in
  `capabilities_blob.unsupported_request_parameters`;
- ascending display priority and enabled state.

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

Endpoint loading chooses a codec solely by `wire_shape`:

1. `openai_completion` encodes and decodes chat-completions JSON.
2. `openai_response` encodes and decodes Responses JSON/events.
3. `anthropic_messages` encodes and decodes Messages JSON/events.

The validated configuration compiles into an immutable `LLMEndpoint` that binds
the codec, authentication provider and transport once. The authentication
component resolves renewable credentials and applies access-profile failure
policy. The configured transport owns network resource lifetimes. HTTP SDK
clients live in `http_transport`; persistent sockets live in
`websocket_transport`. Both return JSON frames to the same codec pipeline.
Configuration replacement retires the old composition: admitted requests may
finish, later admissions are rejected, and owned resources close after drain.
Credentials and connections can renew without changing the component binding.

HTTP supports all three shapes. WebSocket currently supports `openai_response`
with API credentials or SIWC; configuration rejects unsupported combinations.
The WebSocket address is derived from the endpoint base URL, while the SIWC
profile continues to restrict credentials to the official destination.
`pal llm add ... --transport http|websocket` and the setup wizard persist this
selection. Omitted transport on API configuration defaults to HTTP. The SIWC
wizard defaults to WebSocket and allows explicit HTTP. Existing rows gain an
explicit transport during schema upgrade: HTTP for API, WebSocket for SIWC.
Transport is part of endpoint serialization/fingerprinting, including Bunshin
snapshots; workers cannot override the credential owner's selection.

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

## Responses reasoning continuation

`openai_response` sends `store=false` and
`include=["reasoning.encrypted_content"]`: L1 owns conversation state and keeps
the returned reasoning items in each assistant's `ReplayEnvelope`. The include
is retained for compatible endpoints using the older opt-in behavior; current
OpenAI stateless responses return encrypted reasoning by default. This does
not change the Completion or Anthropic request formats.

`capabilities_blob.reasoning_context` may explicitly select `auto`,
`current_turn`, or `all_turns` for a Responses endpoint known to support it.
Omitting this setting keeps the model's default; Pal does not guess support
from a model name. The resolved value lives in `GenerationPolicyIR` and is
preserved during warm compact replay. Invalid values and use on other shapes
are rejected. Existing `unsupported_request_parameters` can omit `store` or
`include` for incompatible gateways. An explicitly requested
`reasoning.context` declared unsupported is rejected, not silently ignored.

The returned effective `reasoning.context` is recorded separately in
`LLMResponseIR`, assistant metadata persisted with L1, and attempt diagnostics.
It is not injected into prompt text. A missing observation stays unknown;
Pal never substitutes the requested mode as proof of the effective mode.
Older serialized responses without the field remain readable.

Reference: [OpenAI reasoning continuation](https://developers.openai.com/api/docs/guides/reasoning#preserve-reasoning-across-calls).

## Streaming and output recovery

Codecs accumulate partial text, reasoning, usage, and private tool drafts.
Only a successful terminal frame can promote a complete tool draft. EOF without
a terminal state is an error. Null delta placeholders do not erase already
accumulated content or tool identities. Chat `reasoning_details` chunks retain
their received order, including signatures and opaque values.

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

## Provider and endpoint response hooks

Inbound processing is:

`transport frames -> wire-shape envelope splitting -> provider hook -> endpoint hook -> common semantic decoding -> IR`

Envelope splitting identifies text, reasoning, tool input and item/response
closure. It retains complete tool input as an unparsed JSON value/string in a
non-executable `ResponseBlock`. Both hook layers consume and return immutable
`ResponseBlockUpdate` streams. The common decoder then validates tool JSON and
constructs the shared `ToolCallIR`. Partial tool drafts stay inside the codec
until their provider-confirmed closure; they cannot become executable items.
`codec.decode()` remains a convenience for split plus common decoding without
protocol hooks. Production endpoint inference executes the bound composition.

Provider defaults run first for every endpoint with that provider identity.
An exact-endpoint hook then handles deployment/proxy-specific differences.
Both are bound into the immutable endpoint composition at assembly time;
refresh creates new bindings while in-flight requests retain their old hooks.
Hook functions receive request context but cannot change routing, credentials,
generation policy, request messages, or codec selection. Their iterator state
belongs to one attempt and must not be kept in shared module globals.

Runtime configuration uses trusted Python modules under the selected runtime:

- `llm/providers/*.py` exports `PROVIDER_ID` and `normalize_blocks(context, updates)`.
- `llm/endpoints/*.py` exports `ENDPOINT_ID` and `normalize_blocks(context, updates)`.

Provider identities are case-insensitive; endpoint identities match exactly.
Files beginning with `_` are ignored. A configured provider hook replaces that
provider's built-in default; an endpoint hook composes after the inherited
provider hook. Duplicate identities within a directory, invalid exports and
import errors fail loading. Startup and `refresh_llm_endpoints` load both
directories; deleting a configured hook restores its inherited default on
refresh. Invalid hook reloads leave the previous response-hook registry in use.
These response hooks are separate from the existing exact-model outgoing
message/tool hooks under `llm/models`.
Sandboxed Bunshin workers receive read-only mounts of all three hook
directories and use the same loader and bound processing pipeline as Pal.

The built-in `deepseek` hook recognizes DeepSeek's textual DSML tool protocol
only for endpoints whose provider identity is `deepseek`. Other providers,
including Zhipu/GLM, do not scan reasoning or body text for DSML or textual
tool markers. Their tool calls come only from native structured tool items.

Codecs classify block types before normalization, including streamed deltas.
Reasoning remains reasoning, even when it quotes a complete DSML envelope.
The DeepSeek compatibility hook is an incremental decorator over the
response-block iterator: ordinary content streams immediately, while a
cross-chunk prefix gate retains a possible top-level `tool_calls` envelope at
a body line boundary. Inline mentions, Markdown fences, quotes and indented
code remain body text. An unquoted, top-level DSML envelope is treated as the
DeepSeek compatibility protocol; callers should fence literal envelope examples.
Native structured tool calls pass through unchanged and
take precedence over textual mirrors. A complete DSML response with no native
call is split into text/tool blocks and assigned internal call
IDs at this Pal-owned adapter boundary. DSML inside an existing native tool
input is translated into a standard argument object while retaining the
provider's call ID. Body content around a recovered
envelope is kept separate from executable tool items. Malformed, unsuccessful, filtered, or
unterminated DSML fails the provider attempt and follows normal bounded
same-endpoint retry handling. A length-truncated DSML block stays hidden while
output recovery joins its continuation through the same bound protocol
pipeline. Merged IR is not sent through endpoint hooks a second time. Native
source material is captured before either hook; normalized tool input
can be materialized after common decoding while preserving original source
payloads and opaque reasoning/signatures. An adapter must explicitly mark
`protocol_tool_inventory_adapted` when synthesizing tool blocks from body text;
generic replay binding uses that receipt rather than provider-specific rules.

Accepted L1 messages, including their replay envelopes, are the sole response
authority. Projection send receipts prove which request was sent and carry no
response payload. After finalization, Core derives native projection material
from the accepted message's replay and verifies its tool IDs, names and
arguments against semantic IR. Original pre-hook captures and
`replay.source_payload` remain evidence; they never supply an independent
response to freeze. Opaque reasoning/signatures remain unchanged within accepted
replay. Degraded continuation refuses projection freezing rather than replacing
the accepted message or silently dropping native fields.

Runtime depends on `LLMEndpointInvokerPort`, whose outputs are already adapted,
validated semantic IR. `ManagedEndpointInvokerPort` additionally supplies
endpoint lifecycle, hook configuration, attempt accounting and typed projection
and submission receipts. These capabilities are structural contracts;
Runtime does not branch on a concrete invoker class. Custom raw implementations
plug into the frame transport or block pipeline before common decoding. Custom
semantic invokers own acceptance of their IR. Neither their output nor merged
continuation IR is converted back into protocol blocks by Runtime.

Failed attempts save private diagnostic JSON under
`<PAL_LOG_ROOT>/llm-decode-failures/` (default `/tmp/pal/llm-decode-failures/`).
This covers codec, response-hook and transport exceptions and terminal error
responses. Captures include observed SDK frames from the start of that attempt,
the exception chain, endpoint/attempt identities, and Core's immutable
pre-submission R snapshot with turn, round and L/R cut identity when available.
The snapshot precedes submission because successful submission advances the
live cut. Failed output is diagnostic evidence and never becomes accepted L1.
Full requests, authentication headers and credential configuration are omitted.
Response frames and R each have a 2 MiB retention bound; dropped/truncated data
is explicitly marked. At most 20 captures are kept, with directory mode 0700
and file mode 0600. Success and cancellation do not create failure captures.

## Invariants

- IR is the only internal LLM contract.
- L1 is the only active-turn and compaction truth source.
- Settled L1 preserves replay envelopes and reasoning until explicit compaction.
- Every executable tool call has complete parsed object arguments.
- Uncommitted tool drafts from truncated or unterminated responses are never
  executable. Provider-committed tool items survive a non-error `length` terminal
  and reach the normal agent loop; syntactically complete JSON alone is not a
  provider commit. See `spec/llm/README.md` for the turn/item model mapping.
- Endpoint schema and thinking enums are locally validated before a request.
- The persisted endpoint row is the sole source of supported thinking values;
  preflight evaluates the fully hooked request.
- A missing or rejected credential fails that endpoint as one unit; credentials
  are never borrowed across endpoints. Automatic endpoint fallback is unsupported
  for all providers. Legacy fallback settings and request overrides are ignored.
- `finish_reason=error`, including output-recovery errors, is a failed provider
  attempt and never updates successful health or usage state.
- Request/model quirks are exact-model hooks. Provider-wide branching is
  limited to response-syntax normalization and cannot alter behavior policy.

## Model switching and reasoning replay

The active endpoint must be explicitly configured. A missing, disabled, or deleted
selection is an error; the runtime never chooses the first available endpoint.
Deleting the active endpoint clears the selection. All retries, including a
stale-spec refresh, stay on the selected endpoint.

Replay compatibility is computed from loaded endpoint metadata and existing
`ReplayEnvelope` bindings; L1 gains no new provenance fields. The initial built-in
allowlist is the documented OpenAI API family `gpt-5.6-sol`, `gpt-5.6-terra`, and
`gpt-5.6-luna`, using `openai_response`, the official API URL, API-key auth, and
the same nonempty credential reference. Provider labels, model prefixes,
subscription endpoints, and OpenAI-compatible gateways do not establish compatibility.
See [OpenAI's reasoning reuse contract](https://developers.openai.com/api/docs/guides/reasoning#preserve-reasoning-across-calls).

Every opaque historical binding must be accepted. Compatible replay retains its
original source identity and native bytes; the new endpoint rebuilds its own
projection and cache. Projection binding and receipt checks remain strict. Empty
history or history without incompatible reasoning does not require Compact unless
it exceeds the target budget. Other switches require confirmed Compact on the old
endpoint through the idle-only [control flow](pal_control_plane.md).

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

## Cache evidence and attempt settlement

### Provider-specific history preservation

Historical reasoning is stored independently of whether a provider uses it.
Provider request hooks apply the following controls after cold encoding or
projection assembly and cache normalization, before final request auditing and
transport; shape codecs do not select provider history policy.
Endpoints can opt into their native preservation protocol through
`capabilities_blob.preserved_thinking`:

- `"glm"` requires `openai_completion` and sends
  `thinking.clear_thinking=false` through the SDK's `extra_body`. Z.AI's standard
  API requires this opt-in; Coding Plan already enables preserved thinking by
  default. This setting preserves history; it does not change thinking effort
  or force the current request to generate reasoning.
- `"anthropic"` requires `anthropic_messages` and sends
  `context_management.edits=[{"type":"clear_thinking_20251015","keep":"all"}]`
  with the `anthropic-beta: context-management-2025-06-27` header.

Omission leaves the provider's defaults unchanged. Enable only on endpoints
supporting the selected protocol; shape compatibility alone is insufficient.
Wrong shape/protocol combinations and explicit unsupported-parameter conflicts
fail preparation. No existing runtime configuration is modified automatically.
Reload endpoint configuration after setting the capability; resident codec
changes require an external host restart.

Anthropic `citations_delta` is merged by the shape codec into the native text
block's ordered `citations` array in `ReplayEnvelope`. Complete citation objects,
including unknown extension fields, survive L1 settlement, persistence and
same-binding reconstruction. They are not flattened into prose or interpreted
by the generic IR. Malformed citation objects fail decoding rather than silently
disappearing. This does not claim support for arbitrary unknown delta protocols.

References: [Z.AI thinking](https://docs.z.ai/guides/capabilities/thinking-mode),
[Anthropic context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing),
[Anthropic citations](https://platform.claude.com/docs/en/build-with-claude/citations).

### Attempt evidence

Cache marker submission does not establish confirmed cache coverage. Usage carries
field presence, raw numeric evidence, and protocol-specific input accounting;
missing cost is distinct from zero. Every started provider attempt settles once,
including failures and output recovery, separately from logical request success.
Cache profile selection is frozen at turn start. See
[prompt-cache v2 offline repair](archive/prompt_cache/prompt_cache_v2_offline_repair.md) for selection
precedence, diagnostics, conservative compaction behavior, and offline validation.

Endpoint cache capabilities are bound once per shape context before policy
execution. Provider/model compatibility presets preserve existing defaults;
an explicit `capabilities_blob.prompt_cache.protocol` takes precedence over
every preset. Unknown endpoints, including DeepSeek without a declared cache
protocol, use `general`: implicit provider caching with no local wire controls
or assumed explicit-cache support. Generic cache policy consumes these bound
capabilities, not provider/URL/model heuristics.

For example, an endpoint whose cache behavior is not yet known can declare:

```json
{"prompt_cache": {"protocol": "general", "mode": "implicit", "reminder_ttl_seconds": 1800}}
```

Use `pal llm add <endpoint> --replace --cache-protocol general --cache-mode
implicit --cache-reminder-ttl-seconds 1800 --runtime-root <dir>` to update an
existing endpoint through the CLI. Omitted cache fields are preserved, and
protocol/shape mismatches are rejected before the endpoint is written.

`protocol` also accepts the existing cache dialect names, such as
`openai_chat_explicit`, `openai_responses_explicit`,
`openrouter_openai_explicit`, `anthropic_explicit`, and
`openrouter_anthropic_explicit`. The declared protocol must match `wire_shape`;
it is independent of the provider label and model alias. `mode` continues to
select implicit, explicit or supported hybrid behavior; declaring an explicit
capability alone does not opt into sending explicit cache controls.
`protocol: general` also prevents an exact-model hook from installing a cache
profile implicitly. `enabled: false` disables caching controls and reminders.

`reminder_ttl_seconds` is an optional positive integer scheduling estimate. An
explicit wire cache TTL takes precedence when it expires sooner. This parameter
never establishes confirmed cache coverage or authorizes a hot-only compaction;
without an override, existing provider estimates remain available, while an
explicit `general` policy uses the general estimate. These additions do not
rewrite runtime endpoint configuration.

## ChatGPT plan access (SIWC)

ChatGPT plan usage is an explicit endpoint capability:
`auth_kind=oauth`, `wire_shape=openai_response`,
`base_url=https://api.openai.com/v1`, and
`capabilities_blob.access_profile=openai_chatgpt`. Provider names do not select
request semantics. Legacy Codex OAuth material is not converted or accepted;
configure a new registration in `pal wizard --llm --runtime-root <dir>`.

The LLM-owned public OAuth client uses dynamic registration, PKCE, OIDC identity
validation and a short-lived loopback listener. Issued client IDs stay paired
with verified identities, even when emails match. A stable host ID belongs to
the runtime. The encrypted secret store retains account registrations, rotating
tokens and quota pauses with owner-only files and locked atomic updates.
Refresh occurs on demand within two minutes of expiry (respecting the returned
earliest-refresh time). The host and Bunshin manager retain credential ownership;
remote runners receive raw inference frames and safe failure metadata only.

Subscription inference follows the endpoint's configured transport, including
aggregate generation, compaction and preflight. WebSocket uses
`wss://api.openai.com/v1/responses`; explicit HTTP uses streamed Responses
requests. `DirectEndpointTransport` composes the validated codec, access profile
and selected transport independently. Both surfaces consume the same
credential-resolved request envelope and return JSON frames to the existing
codec, evidence, usage and Bunshin paths.
OAuth registration, token refresh and model discovery remain HTTP operations.
The WebSocket dependency is version 15 or later so the SIWC path honors system
HTTP proxy selection and bypass settings. SOCKS proxies additionally require
the library's optional `python-socks` dependency.

Each socket sends sequential `response.create` messages and reads through the
terminal event. Requests send full input history and `store=false`, omit
unsupported parameters including `max_output_tokens` and `temperature`, project
system messages to developer messages, and group Pal functions under the `pal`
namespace. WebSocket messages omit HTTP's `stream` flag. No
`previous_response_id` or lane state is required: L1 remains the conversation
authority. Local token budgets still govern Pal's context planning. Only
`response.completed` completes a subscription inference; interrupted/incomplete
streams and failed responses cannot authorize pending tool execution or replay
acceptance. Ordinary API endpoint behavior remains independent.

Sequential requests reuse an authenticated socket. Concurrent requests acquire
exclusive fd leases over separate sockets; only one idle socket per binding is
retained after they settle. Credential/model changes, endpoint deactivation and
connections older than 55 minutes retire affected sockets without closing them
under an active worker. Interrupted consumers, cancellation and failed terminal
events retire their socket. A physically uncertain close quarantines the bound
resource graph. An idle socket already known to be closed is replaced before
submission. If send or receive fails after a write begins, the result is
`request_outcome_unknown`, which is not automatically retried or rerouted.
Pre-submission connection failures retain the existing bounded retry policy.

Connection reuse makes cache affinity possible; it does not prove that a prefix
was cached. Report only provider usage counters. Loading this resident transport
implementation requires a host restart; endpoint refresh does not load Python.

All requests stay on the selected endpoint, including subscription requests.
No setting or request override can enable automatic fallback. A usage-limit error, whether HTTP
or in-stream, pauses all endpoints sharing that registration. Pauses survive
restart and credential refresh. `/model <subscription_endpoint>` explicitly
clears the pause; internal activation and `/refresh_llm_endpoint` do not. There
is no background quota polling and no inferred reset time. Already submitted
concurrent requests may still finish; new requests are rejected locally.

User feedback distinguishes quota, reauthorization and transient failures, and
links to https://chatgpt.com/settings/usage. Users may manually select another
configured endpoint with `/model <endpoint_id>`. Temporary failures retain
credentials; terminal refresh failures clear tokens while retaining the account
registration. HTTP error status/code/parameter/request ID and stream error codes
remain structured through the Bunshin proxy and LLM failure metadata.

References:
- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [Responses WebSocket mode](https://developers.openai.com/api/docs/guides/websocket-mode)

## Model-facing LLM inspection

`inspect_llm()` is the single indirect, parameterless LLM inspection tool. It
always returns configured/effective endpoint IDs, a complete list of loaded
enabled endpoints, current thinking, and process accounting summaries. Each
endpoint includes public model/provider/transport, capability and limit fields;
only the selected endpoint's display label carries `(active)`. Endpoint IDs stay
unchanged. No separate active-model capability object repeats that information.

This is passive resident state, not a connectivity or credential probe. If the
configured selection is missing, its ID remains visible, the effective ID is
null, and no row is labelled active. An empty loaded endpoint list does not prove
there are no disabled or persisted configurations; `pal llm list --all` inspects
configuration. Selection and thinking changes remain user-controlled through
`/model`; inspection neither refreshes configuration nor activates a connection.

Usage is read once per inspection and projected into process totals plus each
listed endpoint's summary. It is not a current-turn report or an account balance.
Reporting coverage, unknown-cost counts and cost completeness remain visible;
missing endpoint accounting is `not_recorded`, not a zero-cost measurement.
Process totals may include previously loaded endpoints absent from the current
list. Latest-request accounting fields, raw cache policy and internal bookkeeping
are omitted. Existing human `/status` and internal status payloads retain their
established detail. Authentication material and credential references are not
part of the model-facing endpoint projection.

Thinking and usage sections fail independently: known endpoint configuration is
retained, unavailable sections are identified, and model-visible error summaries
remove traceback frames and remain within 600 characters. Original owner
diagnostics are not overwritten. Thinking status is consumed as its dictionary
contract, without attempting to access `.__dict__`.

`list_llm_endpoints`, `inspect_active_llm` and `inspect_llm_endpoint` are withdrawn
from model discovery and invocation. Internal readers and the human status
control handler remain available; no model-facing selector/view parameters or
configuration write tools are introduced. Source implementation awaits the
combined regression batch and does not activate a running Pal host.
