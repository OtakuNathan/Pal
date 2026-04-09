# memory

Owns:
- `L1/L2/L3` memory semantics
- runtime-only `L1` and `L2`
- future durable `L3` repository boundary
- memory pack projection

Does not own:
- channel state
- execution policy
- worker lifecycle
- service lifecycle

Exposes:
- `MemoryQuery`
- `MemoryPack`
- `MemoryService`
- `L3RepositoryPort`
