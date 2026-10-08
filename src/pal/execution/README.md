# execution

Owns:
- capability forest
- immutable, generation-scoped tool registry
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
- every LLM-facing tool has one generation-wide unique alias; direct tools are
  provider tools, while indirect tools are discovered with `search_tools` and
  `read_tool` and invoked only through `call_tool`
- Pal-owned tools bind strict Pydantic v2 input/output models, `ToolGuidance`,
  machine execution semantics, examples, and the handler in one immutable
  registry record. Provider descriptions and search documents are compiled
  from guidance; capability authors do not maintain parallel prose fields
- guidance may name likely next tools. Compilation renders the exact direct or
  `read_tool`/`call_tool` route for the current surface. Unknown first-party
  aliases fail compilation, while detachable and scoped projections render an
  unavailable/rediscovery fallback
- invocation returns a discriminated `complete`, `rejected`, or
  `failed` result; effect outcome and retry direction are explicit
- failures must deliver their original cause, exception chain and diagnostic
  details to the model. Recovery or transport errors add evidence; they must
  not replace the original result or erase its effect/retry semantics. The
  shared tool policy explains these fields in normal and failure prompts
- complete output is validated before budgeting. Large output is saved as an
  immutable UTF-8 file with a bounded head/tail preview and its local path. Use
  `rg` or `read_file` to inspect the copy; business-query pagination is unchanged
- MCP `tools/call` and `prompts/get` use separate result envelopes. Prompt
  retrieval preserves messages and supplies a read receipt instead of applying
  a tool's `structuredContent` output contract
- Alias translation applies to Pal routing prose and internal schema annotations.
  Schema literals, defaults, examples, references and external MCP descriptions
  retain their original values for discovery, validation and invocation
- `delete_path` removes the final filesystem entry. Symbolic links are unlinked
  without following their targets, including dangling links and directory links.
  SHA-256 checks apply only to regular files; snapshot guards check the entry
  being removed and continue to protect snapshot storage
- `read_file` preserves CRLF and CR between numbered lines and tells the model
  their JSON escapes. `edit_file` continues to match exact authorized bytes;
  callers remove display labels and preserve the delivered line endings
- if a failure's full text cannot be saved, deliver it inline with the storage
  error and an explicit budget exception. A preview without a complete snapshot
  must not become the only remaining account of why the tool failed
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
