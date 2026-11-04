# execution

Owns:
- capability forest
- immutable, generation-scoped tool registry
- `ToolGuidance.search_terms`: internal reviewed action/object synonyms. Legacy
  `search_objects` remains supported. `search_enum_fields` explicitly selects
  operation enum properties; unrelated format/value enums are not indexed.
  Within an explicit owner domain, affirmative purpose and authored auxiliary
  terms/intents rank candidates without action/object gates. Without a domain,
  exact aliases and conservative keyword matches precede purpose fallback,
  which requires at least two positive term matches. Negative-use prose is not
  positive retrieval evidence. Vocabulary survives worker
  serialization, stays out of model-facing contracts/results, and contributes
  to the immutable registry fingerprint.
- O(1) bound action dispatch index
- plugin registration surface
- the only side-effect execution boundary

Does not own:
- conversation state
- durable truth
- control decisions
- channel transport

Exposes:
- `CapabilityDescriptor`
- `CapabilityCall`
- `CapabilityResult`
- `ExecutionRuntime`

Notes:
- `Execution` is the physical owner of the unified Capability Forest
- capability lifecycle and Manager dispatch use `canonical_path + target_id`
  internally; canonical paths are never an LLM invocation surface
- each attach or detach compiles forest, bindings, aliases, search records, and
  provider contracts into one `ToolRegistryGeneration`, then atomically swaps
  one pointer
- lifecycle protection lasts until a synchronous handler's worker exits.
  Cancellation waits for that exit before propagating; repeated cancellation
  cannot release the fence early, and cancelled queued handlers do not start
- every LLM-facing tool has one generation-wide unique alias; direct tools and
  explicit `native_function` opt-ins expose complete schemas in the tool window.
  Other indirect tools are discovered with `search_tools`, inspected with
  `read_tool` when contract information is missing, and invoked through `call_tool`
- Pal-owned tools bind strict Pydantic v2 input/output models, `ToolGuidance`,
  machine execution semantics, examples, and the handler in one immutable
  registry record. Provider descriptions and search documents are compiled
  from guidance; capability authors do not maintain parallel prose fields
- input serialization preserves declared defaults inside nested lists and
  objects, explicit nulls, and false/zero values; only unset null sentinels
  are omitted
- guidance may name likely next tools. Compilation renders the actual direct,
  native-function or indirect route for the current surface. Unknown hints on
  non-detachable first-party tools fail compilation unless explicitly permitted.
  Missing optional or role-scoped hints are omitted from model descriptions;
  their authored relations remain available for future registry generations
- invocation returns a discriminated `complete`, `rejected`, or
  `failed` result; effect outcome and retry direction are explicit
- each tool/provider owns its failure presentation at its execution exit:
  a concise cause, an observed effect receipt and result-specific recovery.
  Execution preserves that text and those facts; it does not append private
  structured diagnostics to a provider's message. Unexpected dispatch faults
  use an Execution-owned fallback with unknown effect and reconciliation first.
  Core delivers the resulting tool text without deriving business recovery
- control replies use `CapabilityResult.user_text`: the owner message plus its
  effect and recovery when needed, without private structured diagnostics
- capability and workflow result adapters retain effect receipts, recovery
  guidance, context attachments and snapshot references across wrapper boundaries
- diagnostic fields are redacted before JSON serialization. Already encoded
  diagnostics respect escaped quotes and newlines, preserving the cause after
  a credential while keeping the original host evidence separate
- complete output is validated before budgeting. Large output is saved as an
  immutable UTF-8 file with a bounded head/tail preview and its local path. Use
  `rg` or `read_file` to inspect the copy; business-query pagination is unchanged
- errors, status, retry decisions and recovery guidance do not consume the
  normal-output budget. Delivery does not summarize or truncate owner-authored
  errors, even when long; per-item receipts and recovery constraints stay intact.
  Owners keep stack traces in logs or explicit diagnostic snapshots. Execution's
  output validator captures its own rejected output at the validation boundary
- output-validation failures retain the provider's reported result and explicit
  effect receipt in `operation_result` and `effect_receipt` metadata, together
  with recovery guidance. These remain visible without reading the diagnostic
  snapshot and do not consume the normal-output budget; validation does not
  establish or reverse the provider's reported effects
- a completed operation may carry `delivery_error` metadata for a secondary
  output or guidance fault. This diagnostic is shown outside the normal-output
  budget without re-summarizing the owner's message; it does not change the operation's
  status or effect and does not authorize repeating completed side effects
- MCP `tools/call` and `prompts/get` use separate result envelopes. Prompt
  retrieval preserves messages and supplies a read receipt instead of applying
  a tool's `structuredContent` output contract
- Alias translation applies to Pal routing prose and internal schema annotations.
  Schema literals, defaults, examples, references and external MCP descriptions
  retain their original values for discovery, validation and invocation
- The internal path-deletion adapter (not a public model tool) removes the final filesystem entry. Symbolic links are unlinked
  without following their targets, including dangling links and directory links.
  SHA-256 checks apply only to regular files; snapshot guards check the entry
  being removed and continue to protect snapshot storage
- `read_file` preserves CRLF and CR between numbered lines and tells the model
  their JSON escapes. `edit_file` continues to match exact authorized bytes;
  callers remove display labels and preserve the delivered line endings
- full exception chains belong in redacted service logs or diagnostic snapshots.
  Owners use `exception_reason` at their error exit, and may use
  `render_diagnostic_record_for_llm` for health/lifecycle records. A failed
  diagnostic snapshot must still produce a brief error; it never authorizes
  dumping a traceback into a conversation. Successful business output retains
  its existing lossless storage-failure behavior
- tool-result delivery metadata is stored on the L1 `ToolResultIR`. A delivered
  read result remains verbatim in prompt history and owns its file grant until
  compaction retires that result. Snapshot files are owned by explicit L1 references, pending delivery and
  in-flight request pins; there is no elapsed-turn TTL
- Core commits L1 tool-result delivery and Execution file authority as one
  rollback boundary. If either side rejects a late or malformed delivery, the
  other side is rolled back and its uncommitted snapshot is retired
- Execution exposes one runtime-state port for logical input clocks, output
  references, file-read snapshots, and grants. Core alone coordinates whole-runtime
  snapshot/restore/reset
- instance-level actions are hydrated at runtime and compiled into exact bound
  actions
