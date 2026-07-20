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
- Pal-owned tools bind strict Pydantic v2 input/output models, static guidance,
  machine execution semantics, examples, search text, and the handler in one
  immutable `Tool` value
- invocation returns a discriminated `complete`, `paged`, `rejected`, or
  `failed` result; effect outcome and retry direction are explicit
- complete output is validated before paging, and paged results expose only an
  opaque result handle plus exact `read_tool_result` affordances
- instance-level actions are hydrated at runtime and compiled into exact bound
  actions
