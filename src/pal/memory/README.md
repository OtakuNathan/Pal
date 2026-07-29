# memory

Owns:
- `L1/L2/L3` memory semantics
- runtime-only `L1` and `L2`
- future durable `L3` repository boundary
- memory pack projection
- shared recall, L2 heat, and prompt projection for every agent host
- injected compaction policy as the only host-specific memory seam

Does not own:
- channel state
- execution policy
- worker lifecycle
- service lifecycle

Exposes:
- `MemoryQuery`
- `MemoryPack`
- `MemoryService`
- `MemoryCompactionPolicy`
- `L3RepositoryPort`

Interaction rule:
- Pal and Minion instantiate the same `MemoryService` behavior and
  `MemoryPromptFragmentProvider`
- Minion may inject a task-continuity compaction policy; recall, L2 retirement,
  L3 selection, and prompt rendering remain shared
