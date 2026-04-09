# execution

Owns:
- capability forest
- compiled capability/search index
- O(1) bound action dispatch index
- tool registry
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
- search is fuzzy and alias-based, but execution is strict and uses
  `canonical_path + target_id`
- instance-level actions are hydrated at runtime and compiled into exact bound
  actions
