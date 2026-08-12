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
- invocation returns a discriminated `complete`, `paged`, `rejected`, or
  `failed` result; effect outcome and retry direction are explicit
- complete output is validated before paging, and paged results expose only an
  opaque result handle plus exact `read_tool_result` affordances
- tool-result delivery metadata is stored on the L1 `ToolResultIR`, so file
  visibility can be rebuilt from the conversational truth rather than a second
  transcript. Pager payloads stay in the logical execution runtime; handles,
  full-result replay, file snapshots, and mutation grants retire together
- Core commits L1 tool-result delivery and Execution file authority as one
  rollback boundary. If either side rejects a late or malformed delivery, the
  other side is rolled back and its uncommitted pager payload is retired
- Execution exposes one runtime-state port for logical input clocks, pager
  handles, file snapshots, and grants. Core alone coordinates whole-runtime
  snapshot/restore/reset
- instance-level actions are hydrated at runtime and compiled into exact bound
  actions
